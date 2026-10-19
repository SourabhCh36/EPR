"""
Pydantic (v2) schemas for ingestion and claim submission.
"""
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models import StreamType

# Weighbridge indicators round to ~1 kg; allow that much slack on reported net weight.
NET_WEIGHT_TOLERANCE_KG = 1.0


def to_naive_utc(dt: datetime) -> datetime:
    """Normalise any datetime to naive UTC (the storage convention)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# --------------------------------------------------------------------------- #
# Facilities & machinery
# --------------------------------------------------------------------------- #
class MachineryCreate(BaseModel):
    machine_type: str = Field(min_length=1, max_length=100)
    rated_kg_per_hour: float = Field(gt=0, description="Nameplate throughput, kg/hr")
    min_kwh_per_kg: float = Field(gt=0, description="Lower SEC bound, kWh per kg processed")
    max_kwh_per_kg: float = Field(gt=0, description="Upper SEC bound, kWh per kg processed")

    @model_validator(mode="after")
    def _sec_bounds_ordered(self) -> "MachineryCreate":
        if self.max_kwh_per_kg < self.min_kwh_per_kg:
            raise ValueError("max_kwh_per_kg must be >= min_kwh_per_kg")
        return self


class MachineryRead(MachineryCreate):
    model_config = ConfigDict(from_attributes=True)
    machine_id: int
    facility_id: int


class FacilityCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    shed_area_sqm: float = Field(gt=0)
    licensed_capacity_tpa: float = Field(gt=0, description="Licensed tonnes per annum")
    polymer_types: list[str] = Field(default_factory=list)


class FacilityRead(FacilityCreate):
    model_config = ConfigDict(from_attributes=True)
    facility_id: int


# --------------------------------------------------------------------------- #
# Telemetry ingestion
# --------------------------------------------------------------------------- #
class WeightTransactionCreate(BaseModel):
    facility_id: int
    timestamp: datetime
    stream_type: StreamType
    gross_weight_kg: float = Field(ge=0)
    tare_weight_kg: float = Field(ge=0)
    # Optional: if the weighbridge reports a net weight we cross-check it;
    # otherwise it is derived as gross - tare.
    net_weight_kg: Optional[float] = Field(default=None, ge=0)

    @field_validator("timestamp")
    @classmethod
    def _normalise_ts(cls, v: datetime) -> datetime:
        return to_naive_utc(v)

    @model_validator(mode="after")
    def _check_and_derive_net(self) -> "WeightTransactionCreate":
        if self.gross_weight_kg < self.tare_weight_kg:
            raise ValueError("gross_weight_kg must be >= tare_weight_kg")
        derived = self.gross_weight_kg - self.tare_weight_kg
        if self.net_weight_kg is None:
            self.net_weight_kg = derived
        elif abs(self.net_weight_kg - derived) > NET_WEIGHT_TOLERANCE_KG:
            raise ValueError(
                f"net_weight_kg ({self.net_weight_kg}) does not equal gross - tare ({derived})"
            )
        return self


class WeightTransactionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    txn_id: int
    facility_id: int
    timestamp: datetime
    stream_type: StreamType
    gross_weight_kg: float
    tare_weight_kg: float
    net_weight_kg: float


class EnergyReadingCreate(BaseModel):
    facility_id: int
    timestamp: datetime
    kwh_consumed: float = Field(ge=0)

    @field_validator("timestamp")
    @classmethod
    def _normalise_ts(cls, v: datetime) -> datetime:
        return to_naive_utc(v)


class EnergyReadingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    reading_id: int
    facility_id: int
    timestamp: datetime
    kwh_consumed: float


# --------------------------------------------------------------------------- #
# Claim submission
# --------------------------------------------------------------------------- #
class ClaimSubmission(BaseModel):
    """
    A certificate claim to be validated against physical telemetry.

    The window is half-open: [period_start, period_end).
    """

    facility_id: int
    period_start: datetime
    period_end: datetime
    claimed_output_kg: float = Field(gt=0, description="Granule output the certificate claims")
    opening_inventory_kg: float = Field(
        default=0.0, ge=0, description="Declared scrap stock on hand at period start"
    )
    declared_operating_hours: Optional[float] = Field(
        default=None, gt=0, description="Hours of machine operation the recycler declares"
    )

    @field_validator("period_start", "period_end")
    @classmethod
    def _normalise_ts(cls, v: datetime) -> datetime:
        return to_naive_utc(v)

    @model_validator(mode="after")
    def _window_ordered(self) -> "ClaimSubmission":
        if self.period_end <= self.period_start:
            raise ValueError("period_end must be after period_start")
        return self
