"""
FastAPI router: onboarding, telemetry ingestion, and verification.
"""
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from database import get_db
from models import EnergyTelemetry, Machinery, PWPFacility, WeightTransaction
from schemas import (
    ClaimSubmission,
    EnergyReadingCreate,
    EnergyReadingRead,
    FacilityCreate,
    FacilityRead,
    MachineryCreate,
    MachineryRead,
    WeightTransactionCreate,
    WeightTransactionRead,
    to_naive_utc,
)
from validation_engine import FacilityNotFound, ValidationEngine

router = APIRouter()


def _require_facility(db: Session, facility_id: int) -> PWPFacility:
    facility = db.get(PWPFacility, facility_id)
    if facility is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Facility {facility_id} not found")
    return facility


# --------------------------------------------------------------------------- #
# Onboarding (needed to register facilities/machines before telemetry arrives)
# --------------------------------------------------------------------------- #
@router.post("/facilities", response_model=FacilityRead, status_code=status.HTTP_201_CREATED,
             tags=["onboarding"])
def create_facility(payload: FacilityCreate, db: Session = Depends(get_db)):
    facility = PWPFacility(**payload.model_dump())
    db.add(facility)
    db.commit()
    db.refresh(facility)
    return facility


@router.post("/facilities/{facility_id}/machinery", response_model=MachineryRead,
             status_code=status.HTTP_201_CREATED, tags=["onboarding"])
def add_machinery(facility_id: int, payload: MachineryCreate, db: Session = Depends(get_db)):
    _require_facility(db, facility_id)
    machine = Machinery(facility_id=facility_id, **payload.model_dump())
    db.add(machine)
    db.commit()
    db.refresh(machine)
    return machine


# --------------------------------------------------------------------------- #
# Telemetry ingestion
# --------------------------------------------------------------------------- #
@router.post("/telemetry/weight", response_model=WeightTransactionRead,
             status_code=status.HTTP_201_CREATED, tags=["telemetry"])
def ingest_weight(payload: WeightTransactionCreate, db: Session = Depends(get_db)):
    """Ingest one weighbridge ticket. net_weight_kg is derived/validated by the schema."""
    _require_facility(db, payload.facility_id)
    txn = WeightTransaction(**payload.model_dump())
    db.add(txn)
    db.commit()
    db.refresh(txn)
    return txn


@router.post("/telemetry/energy", response_model=EnergyReadingRead,
             status_code=status.HTTP_201_CREATED, tags=["telemetry"])
def ingest_energy(payload: EnergyReadingCreate, db: Session = Depends(get_db)):
    """Ingest one smart-meter reading (kWh consumed since the previous reading)."""
    _require_facility(db, payload.facility_id)
    reading = EnergyTelemetry(**payload.model_dump())
    db.add(reading)
    db.commit()
    db.refresh(reading)
    return reading


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
@router.get("/verify/{facility_id}", tags=["verification"])
def verify_period(
    facility_id: int,
    start_date: datetime = Query(..., description="Window start (inclusive)"),
    end_date: datetime = Query(..., description="Window end (exclusive)"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Audit mode: validate the physically weighed output over [start_date, end_date)."""
    start, end = to_naive_utc(start_date), to_naive_utc(end_date)
    if end <= start:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "end_date must be after start_date")
    try:
        return ValidationEngine(db).verify(facility_id, start, end)
    except FacilityNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


@router.post("/verify/claim", tags=["verification"])
def verify_claim(claim: ClaimSubmission, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Claim mode: validate a certificate claim against the same physical telemetry."""
    try:
        return ValidationEngine(db).verify(
            claim.facility_id, claim.period_start, claim.period_end, claim
        )
    except FacilityNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
