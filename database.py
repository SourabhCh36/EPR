"""
Database wiring (SQLAlchemy 2.0).

Production target is PostgreSQL. The URL is read from DATABASE_URL so tests and
simulations can swap in SQLite without touching application code.
"""
import os
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://sourabhcahandrakar@localhost:5432/material_verification",
)

if DATABASE_URL.startswith("sqlite"):
    # In-memory SQLite needs one shared connection across threads (used by the simulation).
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
