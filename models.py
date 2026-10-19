"""
SQLAlchemy ORM models for the Independent Material Verification Engine.

Conventions
-----------
* All timestamps are stored as naive UTC (the API layer normalises on ingest).
* Masses are kilograms, energy is kWh.
* Composite (facility_id, timestamp) indexes keep window aggregations fast.
"""
import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

# JSONB on PostgreSQL, plain JSON elsewhere (e.g. SQLite in tests).
JSONType = JSON().with_variant(JSONB(), "postgresql")


class StreamType(str, enum.Enum):
    """Which material stream a weighbridge ticket belongs to."""

    SCRAP_IN = "SCRAP_IN"
    GRANULE_OUT = "GRANULE_OUT"
    REJECT_OUT = "REJECT_OUT"


class PWPFacility(Base):
    """A Plastic Waste Processor (recycler) facility."""

    __tablename__ = "pwp_facilities"

    facility_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    shed_area_sqm: Mapped[float] = mapped_column(Float, nullable=False)
    licensed_capacity_tpa: Mapped[float] = mapped_column(Float, nullable=False)  # tonnes / year
    polymer_types: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)

    machines: Mapped[list["Machinery"]] = relationship(
        back_populates="facility", cascade="all, delete-orphan"
    )


class Machinery(Base):
    """Nameplate data for one machine (extruder, shredder, washing line ...)."""

    __tablename__ = "machinery"
    __table_args__ = (
        CheckConstraint("rated_kg_per_hour > 0", name="ck_machine_rating_positive"),
        CheckConstraint("min_kwh_per_kg > 0", name="ck_machine_sec_min_positive"),
        CheckConstraint("max_kwh_per_kg >= min_kwh_per_kg", name="ck_machine_sec_order"),
    )

    machine_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    facility_id: Mapped[int] = mapped_column(
        ForeignKey("pwp_facilities.facility_id"), nullable=False, index=True
    )
    machine_type: Mapped[str] = mapped_column(String(100), nullable=False)
    rated_kg_per_hour: Mapped[float] = mapped_column(Float, nullable=False)
    # Specific Energy Consumption bounds (SEC) in kWh per kg of polymer processed.
    min_kwh_per_kg: Mapped[float] = mapped_column(Float, nullable=False)
    max_kwh_per_kg: Mapped[float] = mapped_column(Float, nullable=False)

    facility: Mapped[PWPFacility] = relationship(back_populates="machines")


class WeightTransaction(Base):
    """One weighbridge ticket."""

    __tablename__ = "weight_transactions"
    __table_args__ = (
        CheckConstraint("gross_weight_kg >= tare_weight_kg", name="ck_gross_ge_tare"),
        CheckConstraint("tare_weight_kg >= 0", name="ck_tare_non_negative"),
        Index("ix_weight_facility_ts", "facility_id", "timestamp"),
    )

    txn_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    facility_id: Mapped[int] = mapped_column(
        ForeignKey("pwp_facilities.facility_id"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    stream_type: Mapped[StreamType] = mapped_column(Enum(StreamType), nullable=False)
    gross_weight_kg: Mapped[float] = mapped_column(Float, nullable=False)
    tare_weight_kg: Mapped[float] = mapped_column(Float, nullable=False)
    net_weight_kg: Mapped[float] = mapped_column(Float, nullable=False)


class EnergyTelemetry(Base):
    """One smart-meter reading: energy consumed since the previous reading."""

    __tablename__ = "energy_telemetry"
    __table_args__ = (
        CheckConstraint("kwh_consumed >= 0", name="ck_kwh_non_negative"),
        Index("ix_energy_facility_ts", "facility_id", "timestamp"),
    )

    reading_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    facility_id: Mapped[int] = mapped_column(
        ForeignKey("pwp_facilities.facility_id"), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    kwh_consumed: Mapped[float] = mapped_column(Float, nullable=False)
