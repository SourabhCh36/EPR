"""
Physical Validation Engine.

Treats a recycler's reporting period as a physical system and checks a
certificate claim against three invariants:

  1. Mass balance     M_in (+ opening stock) = M_granules + M_rejects + dInventory
  2. Energy bound     Q * SEC_min <= E_consumed <= Q * SEC_max
  3. Machine capacity Q_claimed <= sum(rated kg/hr) * operating hours

Design
------
* Each invariant is an independent `InvariantCheck` plugin that reads an
  immutable `ValidationContext` and returns a `CheckResult`.
* An `AnomalyScorer` turns failed checks into a single 0..1 confidence score.
* `ValidationEngine` only orchestrates: load data -> build context -> run checks
  -> score -> assemble report. To scale detection later (ML models, cross-
  facility benchmarks, runtime inference from meter load curves) add new
  `InvariantCheck` classes and pass them in; nothing else changes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional, Protocol, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import EnergyTelemetry, Machinery, PWPFacility, StreamType, WeightTransaction
from schemas import ClaimSubmission

PASS = "PASS"
FAIL = "FAIL"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EngineConfig:
    """Measurement tolerances. These absorb instrument error, not fraud."""

    mass_tolerance: float = 0.02      # weighbridge accuracy / moisture & dust loss (2%)
    energy_tolerance: float = 0.05    # smart-meter class accuracy (5%)
    capacity_tolerance: float = 0.01  # nameplate rounding (1%)
    # Yield above this (granules / available scrap) is physically implausible
    # for post-consumer plastic (contamination, moisture, labels). Advisory only.
    implausible_yield: float = 0.97
    # Failed-check severity floor: any hard failure is at least this suspicious.
    severity_floor: float = 0.5


# --------------------------------------------------------------------------- #
# Context & results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ValidationContext:
    """Everything the checks need, pre-aggregated. Checks never touch the DB."""

    facility: PWPFacility
    machines: Sequence[Machinery]
    window_start: datetime
    window_end: datetime
    window_hours: float
    scrap_in_kg: float
    granule_out_kg: float          # as weighed
    reject_out_kg: float
    energy_kwh: float
    energy_reading_count: int
    weight_txn_count: int
    claimed_output_kg: float       # claim if submitted, else weighed granule output
    claim_submitted: bool
    opening_inventory_kg: float = 0.0
    declared_operating_hours: Optional[float] = None


@dataclass
class CheckResult:
    name: str
    status: str                    # PASS | FAIL | INSUFFICIENT_DATA
    severity: float = 0.0          # 0..1, only meaningful when status == FAIL
    failure_code: Optional[str] = None
    message: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class InvariantCheck(Protocol):
    name: str

    def evaluate(self, ctx: ValidationContext, cfg: EngineConfig) -> CheckResult: ...


def _severity(excess_ratio: float, cfg: EngineConfig, weight: float = 1.0) -> float:
    """
    Map relative violation size (0 = exactly on the limit) to a severity in
    [severity_floor, 1]. Saturating curve: a 10% overshoot is suspicious, a
    6x overshoot is near-certain fraud.
    """
    floor = cfg.severity_floor
    s = floor + (1.0 - floor) * (1.0 - math.exp(-max(excess_ratio, 0.0)))
    return min(1.0, s * weight)


# --------------------------------------------------------------------------- #
# Invariant 1: mass balance
# --------------------------------------------------------------------------- #
class MassBalanceCheck:
    """
    M_input = M_granules + M_rejects + dInventory

    Outputs can never exceed available feedstock (period intake + declared
    opening stock). The implied inventory change must therefore be >= 0,
    within weighbridge tolerance. If the claim is larger than what was actually
    weighed out, the claim is what we test.
    """

    name = "mass_balance"

    def evaluate(self, ctx: ValidationContext, cfg: EngineConfig) -> CheckResult:
        if ctx.weight_txn_count == 0:
            return CheckResult(
                self.name, INSUFFICIENT_DATA, message="No weighbridge transactions in window."
            )

        available = ctx.scrap_in_kg + ctx.opening_inventory_kg
        granules = max(ctx.granule_out_kg, ctx.claimed_output_kg)
        outputs = granules + ctx.reject_out_kg
        delta_inventory = available - outputs  # the balancing term in the equation
        allowed_deficit = cfg.mass_tolerance * available

        metrics = {
            "m_input_kg": round(ctx.scrap_in_kg, 2),
            "opening_inventory_kg": round(ctx.opening_inventory_kg, 2),
            "m_granules_kg": round(granules, 2),
            "m_rejects_kg": round(ctx.reject_out_kg, 2),
            "implied_delta_inventory_kg": round(delta_inventory, 2),
            "allowed_deficit_kg": round(allowed_deficit, 2),
            "yield_ratio": round(granules / available, 4) if available > 0 else None,
        }
        warnings: list[str] = []
        if available > 0 and granules / available > cfg.implausible_yield:
            warnings.append(
                f"Granule yield {granules / available:.1%} is implausibly high for post-consumer scrap."
            )

        if delta_inventory < -allowed_deficit:
            leak_kg = -delta_inventory
            excess = (leak_kg - allowed_deficit) / available if available > 0 else 1.0
            # Mass appearing from nowhere is a hard physical violation; scale generously.
            return CheckResult(
                self.name,
                FAIL,
                severity=_severity(excess * 5, cfg),
                failure_code="MASS_LEAKAGE",
                message=(
                    f"Outputs ({outputs:,.0f} kg) exceed available scrap ({available:,.0f} kg) "
                    f"by {leak_kg:,.0f} kg - mass created from nothing."
                ),
                metrics=metrics,
                warnings=warnings,
            )
        return CheckResult(self.name, PASS, message="Outputs do not exceed intake.",
                           metrics=metrics, warnings=warnings)


# --------------------------------------------------------------------------- #
# Invariant 2: energy bound
# --------------------------------------------------------------------------- #
class EnergyBoundCheck:
    """
    Q * SEC_min <= E_consumed <= Q * SEC_max

    Fleet SEC envelope = (min over machines of min SEC, max over machines of
    max SEC). This is deliberately the widest defensible envelope: it gives the
    recycler the benefit of the doubt, so a failure is a physical impossibility
    rather than a modelling disagreement.
    """

    name = "energy_bound"

    def evaluate(self, ctx: ValidationContext, cfg: EngineConfig) -> CheckResult:
        if not ctx.machines:
            return CheckResult(self.name, INSUFFICIENT_DATA,
                               message="No machinery registered; SEC bounds unknown.")
        if ctx.energy_reading_count == 0:
            return CheckResult(self.name, INSUFFICIENT_DATA,
                               message="No smart-meter readings in window.")

        sec_min = min(m.min_kwh_per_kg for m in ctx.machines)
        sec_max = max(m.max_kwh_per_kg for m in ctx.machines)
        q = ctx.claimed_output_kg
        e = ctx.energy_kwh

        e_min = q * sec_min * (1 - cfg.energy_tolerance)
        e_max = q * sec_max * (1 + cfg.energy_tolerance)
        # Largest output the metered energy could possibly support.
        supported_q = e / (sec_min * (1 - cfg.energy_tolerance))

        metrics = {
            "q_processed_kg": round(q, 2),
            "e_consumed_kwh": round(e, 2),
            "sec_min_kwh_per_kg": sec_min,
            "sec_max_kwh_per_kg": sec_max,
            "e_min_required_kwh": round(e_min, 2),
            "e_max_allowed_kwh": round(e_max, 2),
            "max_output_supported_by_energy_kg": round(supported_q, 2),
            "claim_inflation_factor": round(q / supported_q, 2) if supported_q > 0 else None,
            "implied_sec_kwh_per_kg": round(e / q, 4) if q > 0 else None,
        }

        if e < e_min:
            inflation = q / supported_q if supported_q > 0 else float("inf")
            excess = (inflation - 1.0) if math.isfinite(inflation) else 10.0
            return CheckResult(
                self.name,
                FAIL,
                severity=_severity(excess, cfg),
                failure_code="THERMODYNAMIC_IMPOSSIBILITY",
                message=(
                    f"Claimed {q:,.0f} kg needs at least {e_min:,.0f} kWh but only {e:,.0f} kWh "
                    f"was metered; energy supports at most {supported_q:,.0f} kg "
                    f"(claim is {inflation:.1f}x too large)."
                ),
                metrics=metrics,
            )
        if e > e_max:
            excess = (e - e_max) / e_max if e_max > 0 else 1.0
            # Excess energy is weaker evidence (other loads, under-reported output), so down-weight.
            return CheckResult(
                self.name,
                FAIL,
                severity=_severity(excess, cfg, weight=0.8),
                failure_code="UNEXPLAINED_ENERGY_CONSUMPTION",
                message=(
                    f"Metered {e:,.0f} kWh exceeds the {e_max:,.0f} kWh ceiling for {q:,.0f} kg "
                    f"- either output is under-reported or unrelated load is on the meter."
                ),
                metrics=metrics,
            )
        return CheckResult(self.name, PASS, message="Energy lies within the SEC envelope.",
                           metrics=metrics)


# --------------------------------------------------------------------------- #
# Invariant 3: machine capacity
# --------------------------------------------------------------------------- #
class MachineCapacityCheck:
    """
    Q_claimed <= Machine_Rating_kg_hr * Operating_Hours

    Operating hours can never exceed wall-clock hours in the window, even when
    the recycler declares more.
    """

    name = "machine_capacity"

    def evaluate(self, ctx: ValidationContext, cfg: EngineConfig) -> CheckResult:
        if not ctx.machines:
            return CheckResult(self.name, INSUFFICIENT_DATA, message="No machinery registered.")

        rated_total = sum(m.rated_kg_per_hour for m in ctx.machines)
        hours = ctx.window_hours
        warnings: list[str] = []
        if ctx.declared_operating_hours is not None:
            if ctx.declared_operating_hours > ctx.window_hours * (1 + cfg.capacity_tolerance):
                warnings.append(
                    f"Declared operating hours ({ctx.declared_operating_hours:,.0f}) exceed "
                    f"hours in the window ({ctx.window_hours:,.0f}); capped."
                )
            hours = min(ctx.declared_operating_hours, ctx.window_hours)

        max_output = rated_total * hours
        limit = max_output * (1 + cfg.capacity_tolerance)
        q = ctx.claimed_output_kg

        metrics = {
            "q_claimed_kg": round(q, 2),
            "rated_kg_per_hour_total": round(rated_total, 2),
            "operating_hours_used": round(hours, 2),
            "max_physical_output_kg": round(max_output, 2),
            "utilisation": round(q / max_output, 4) if max_output > 0 else None,
        }

        # Advisory: pro-rated licence limit (not one of the three hard invariants).
        licensed_kg = ctx.facility.licensed_capacity_tpa * 1000 * (ctx.window_hours / (24 * 365))
        if q > licensed_kg:
            warnings.append(
                f"Claim exceeds pro-rated licensed capacity ({licensed_kg:,.0f} kg for this window)."
            )
            metrics["pro_rated_licensed_kg"] = round(licensed_kg, 2)

        if q > limit:
            excess = (q - limit) / limit if limit > 0 else 1.0
            return CheckResult(
                self.name,
                FAIL,
                severity=_severity(excess, cfg),
                failure_code="CAPACITY_EXCEEDED",
                message=(
                    f"Claimed {q:,.0f} kg exceeds the {max_output:,.0f} kg the installed machinery "
                    f"can produce in {hours:,.0f} h at nameplate rating "
                    f"({q / max_output:.1f}x capacity)."
                ),
                metrics=metrics,
                warnings=warnings,
            )
        return CheckResult(self.name, PASS, message="Claim is within nameplate capacity.",
                           metrics=metrics, warnings=warnings)


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
class AnomalyScorer:
    """
    Combine failed-check severities into one Anomaly Confidence Score in [0, 1].

    Independent-evidence (noisy-OR) combination: score = 1 - prod(1 - s_i).
    Two independent physical violations are more damning than one. Replace this
    class to plug in a learned model without touching the engine.
    """

    BANDS = ((0.9, "CRITICAL"), (0.7, "HIGH"), (0.5, "ELEVATED"), (0.0, "LOW"))

    def score(self, results: Sequence[CheckResult]) -> tuple[float, str]:
        p_ok = 1.0
        for r in results:
            if r.status == FAIL:
                p_ok *= 1.0 - r.severity
        score = round(1.0 - p_ok, 4)
        band = next(label for threshold, label in self.BANDS if score >= threshold)
        return score, band


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class FacilityNotFound(Exception):
    pass


class ValidationEngine:
    def __init__(
        self,
        db: Session,
        checks: Optional[Sequence[InvariantCheck]] = None,
        scorer: Optional[AnomalyScorer] = None,
        config: Optional[EngineConfig] = None,
    ) -> None:
        self.db = db
        self.config = config or EngineConfig()
        self.checks: Sequence[InvariantCheck] = checks or (
            MassBalanceCheck(),
            EnergyBoundCheck(),
            MachineCapacityCheck(),
        )
        self.scorer = scorer or AnomalyScorer()

    # -- data loading ------------------------------------------------------- #
    def _sum_weights(self, facility_id: int, start: datetime, end: datetime) -> tuple[dict, int]:
        """Net weight per stream in one grouped query, window is [start, end)."""
        rows = self.db.execute(
            select(
                WeightTransaction.stream_type,
                func.coalesce(func.sum(WeightTransaction.net_weight_kg), 0.0),
                func.count(WeightTransaction.txn_id),
            )
            .where(
                WeightTransaction.facility_id == facility_id,
                WeightTransaction.timestamp >= start,
                WeightTransaction.timestamp < end,
            )
            .group_by(WeightTransaction.stream_type)
        ).all()
        totals = {stream: 0.0 for stream in StreamType}
        count = 0
        for stream, total, n in rows:
            totals[stream] = float(total)
            count += n
        return totals, count

    def _sum_energy(self, facility_id: int, start: datetime, end: datetime) -> tuple[float, int]:
        total, n = self.db.execute(
            select(
                func.coalesce(func.sum(EnergyTelemetry.kwh_consumed), 0.0),
                func.count(EnergyTelemetry.reading_id),
            ).where(
                EnergyTelemetry.facility_id == facility_id,
                EnergyTelemetry.timestamp >= start,
                EnergyTelemetry.timestamp < end,
            )
        ).one()
        return float(total), int(n)

    def build_context(
        self,
        facility_id: int,
        start: datetime,
        end: datetime,
        claim: Optional[ClaimSubmission] = None,
    ) -> ValidationContext:
        facility = self.db.get(PWPFacility, facility_id)
        if facility is None:
            raise FacilityNotFound(f"Facility {facility_id} not found")
        machines = self.db.scalars(
            select(Machinery).where(Machinery.facility_id == facility_id)
        ).all()

        weights, txn_count = self._sum_weights(facility_id, start, end)
        energy_kwh, reading_count = self._sum_energy(facility_id, start, end)
        granules = weights[StreamType.GRANULE_OUT]

        return ValidationContext(
            facility=facility,
            machines=machines,
            window_start=start,
            window_end=end,
            window_hours=(end - start).total_seconds() / 3600.0,
            scrap_in_kg=weights[StreamType.SCRAP_IN],
            granule_out_kg=granules,
            reject_out_kg=weights[StreamType.REJECT_OUT],
            energy_kwh=energy_kwh,
            energy_reading_count=reading_count,
            weight_txn_count=txn_count,
            claimed_output_kg=claim.claimed_output_kg if claim else granules,
            claim_submitted=claim is not None,
            opening_inventory_kg=claim.opening_inventory_kg if claim else 0.0,
            declared_operating_hours=claim.declared_operating_hours if claim else None,
        )

    # -- public API --------------------------------------------------------- #
    def verify(
        self,
        facility_id: int,
        start: datetime,
        end: datetime,
        claim: Optional[ClaimSubmission] = None,
    ) -> dict[str, Any]:
        """
        Verify one facility over [start, end). With no `claim`, the weighed
        granule output is treated as the claimed output (audit mode).
        """
        if end <= start:
            raise ValueError("end must be after start")

        ctx = self.build_context(facility_id, start, end, claim)
        results = [check.evaluate(ctx, self.config) for check in self.checks]

        failed = [r for r in results if r.status == FAIL]
        insufficient = [r for r in results if r.status == INSUFFICIENT_DATA]
        verdict = FAIL if failed else (INSUFFICIENT_DATA if insufficient else PASS)
        score, band = self.scorer.score(results)

        return {
            "facility_id": ctx.facility.facility_id,
            "facility_name": ctx.facility.name,
            "period": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "hours": round(ctx.window_hours, 2),
            },
            "mode": "claim" if ctx.claim_submitted else "audit",
            "verdict": verdict,
            "anomaly_confidence_score": score,
            "risk_band": band,
            "failure_reasons": [
                {
                    "check": r.name,
                    "code": r.failure_code,
                    "severity": round(r.severity, 4),
                    "message": r.message,
                }
                for r in failed
            ],
            "data_gaps": [{"check": r.name, "message": r.message} for r in insufficient],
            "checks": {
                r.name: {
                    "status": r.status,
                    "message": r.message,
                    "metrics": r.metrics,
                    "warnings": r.warnings,
                }
                for r in results
            },
            "aggregates": {
                "scrap_in_kg": round(ctx.scrap_in_kg, 2),
                "granule_out_kg": round(ctx.granule_out_kg, 2),
                "reject_out_kg": round(ctx.reject_out_kg, 2),
                "energy_kwh": round(ctx.energy_kwh, 2),
                "claimed_output_kg": round(ctx.claimed_output_kg, 2),
                "weight_transactions": ctx.weight_txn_count,
                "energy_readings": ctx.energy_reading_count,
            },
        }
