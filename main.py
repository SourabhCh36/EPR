"""
Application entrypoint.

Run:  uvicorn main:app --reload
Set DATABASE_URL to point at PostgreSQL (see database.py for the default).
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

import models  # noqa: F401  (ensures ORM models are registered on Base.metadata)
from api import router
from database import Base, engine


@asynccontextmanager
async def lifespan(_: FastAPI):
    # MVP convenience. In production, manage schema with Alembic migrations instead.
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="Independent Material Verification Engine",
    description="Physics-based verification of plastic recycling claims "
                "(mass balance, energy bound, machine capacity).",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}
