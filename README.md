# Independent Material Verification Engine

Physics-based verification of plastic recycling claims in India. Replaces trust with three physical invariants: mass balance, energy thermodynamics, and machine capacity.

## Overview

The Extended Producer Responsibility (EPR) system relies on self-reported data, enabling massive fraud (recyclers claiming 38x actual capacity). This engine treats plastic recycling as a state machine governed by physical constraints:

1. **Mass Balance**: `M_input = M_granules + M_rejects + ΔInventory`
2. **Energy Bound**: `Q × SEC_min ≤ E_consumed ≤ Q × SEC_max`
3. **Machine Capacity**: `Q_claimed ≤ Σ(rated_kg/hr) × operating_hours`

Any claim that violates these rules is flagged as fraudulent with a confidence score (0–1).

---

## Project Structure

```
material-verification-engine/
├── README.md                      # This file
├── requirements.txt               # Python dependencies
│
├── database.py                    # SQLAlchemy setup, session factory
├── models.py                      # ORM models (PWPFacility, Machinery, WeightTransaction, EnergyTelemetry)
├── schemas.py                     # Pydantic input/output schemas
│
├── validation_engine.py           # Core physics engine (checks, scoring, verification logic)
├── api.py                         # FastAPI router (6 endpoints)
├── main.py                        # FastAPI app entrypoint
│
└── simulate.py                    # End-to-end test (PASS/FAIL scenarios)
```

---

## Installation

### Prerequisites
- Python 3.9+
- PostgreSQL (production) or SQLite (testing)

### Setup

```bash
# Clone or download the project
cd material-verification-engine

# Install dependencies
pip install -r requirements.txt

# (Optional) Set PostgreSQL connection string
export DATABASE_URL="postgresql+psycopg2://user:password@localhost:5432/material_verification"
```

---

## Usage

### 1. Run the Simulation (Test the Engine)

```bash
python simulate.py
```

**Output:**
- ✅ **Scenario A (PASS)**: Honest recycler with viable data
- ❌ **Scenario B (FAIL)**: Fraudulent recycler (6.3x energy overshoot, 1.4x capacity overshoot)
- ❌ **Bonus (FAIL)**: Inflated claim (mass leakage detected)

---

### 2. Start the API Server

```bash
uvicorn main:app --reload
```

Server runs on `http://localhost:8000`

**Swagger docs:** `http://localhost:8000/docs`

---

## API Endpoints

### Onboarding

#### Create a Facility
```bash
POST /facilities
Content-Type: application/json

{
  "name": "Honest Polymers Pvt Ltd",
  "shed_area_sqm": 1200,
  "licensed_capacity_tpa": 6000,
  "polymer_types": ["HDPE", "LDPE", "PP"]
}
```

**Response:** `facility_id` (e.g., `1`)

#### Add Machinery
```bash
POST /facilities/{facility_id}/machinery
Content-Type: application/json

{
  "machine_type": "Twin-screw extruder",
  "rated_kg_per_hour": 500,
  "min_kwh_per_kg": 0.30,
  "max_kwh_per_kg": 0.70
}
```

---

### Telemetry Ingestion

#### Ingest Weighbridge Ticket
```bash
POST /telemetry/weight
Content-Type: application/json

{
  "facility_id": 1,
  "timestamp": "2026-09-01T10:30:00",
  "stream_type": "SCRAP_IN",
  "gross_weight_kg": 28000,
  "tare_weight_kg": 8000
}
```

**Stream Types:** `SCRAP_IN`, `GRANULE_OUT`, `REJECT_OUT`

#### Ingest Smart Meter Reading
```bash
POST /telemetry/energy
Content-Type: application/json

{
  "facility_id": 1,
  "timestamp": "2026-09-01T10:30:00",
  "kwh_consumed": 2550
}
```

---

### Verification

#### Audit Mode (Check Physical Output Against Telemetry)
```bash
GET /verify/{facility_id}?start_date=2026-09-01T00:00:00&end_date=2026-10-01T00:00:00
```

**Response:**
```json
{
  "facility_id": 1,
  "facility_name": "Honest Polymers Pvt Ltd",
  "period": {
    "start": "2026-09-01T00:00:00",
    "end": "2026-10-01T00:00:00",
    "hours": 720.0
  },
  "mode": "audit",
  "verdict": "PASS",
  "anomaly_confidence_score": 0.0,
  "risk_band": "LOW",
  "failure_reasons": [],
  "data_gaps": [],
  "checks": {
    "mass_balance": { "status": "PASS", "message": "...", "metrics": {...} },
    "energy_bound": { "status": "PASS", "message": "...", "metrics": {...} },
    "machine_capacity": { "status": "PASS", "message": "...", "metrics": {...} }
  },
  "aggregates": {
    "scrap_in_kg": 200000.0,
    "granule_out_kg": 170000.0,
    "reject_out_kg": 20000.0,
    "energy_kwh": 76500.0,
    "claimed_output_kg": 170000.0,
    "weight_transactions": 90,
    "energy_readings": 30
  }
}
```

#### Claim Mode (Validate a Certificate Claim)
```bash
POST /verify/claim
Content-Type: application/json

{
  "facility_id": 1,
  "period_start": "2026-09-01T00:00:00",
  "period_end": "2026-10-01T00:00:00",
  "claimed_output_kg": 500000,
  "opening_inventory_kg": 0,
  "declared_operating_hours": 720
}
```

**Response:** Same structure as audit mode, but with `"mode": "claim"` and claim-specific failure codes.

---

## Response Structure

### Verdict
- **PASS**: All checks passed
- **FAIL**: One or more checks failed
- **INSUFFICIENT_DATA**: Missing telemetry (no machines, no meter readings, etc.)

### Anomaly Confidence Score
- **0.0–0.49**: LOW risk
- **0.50–0.69**: ELEVATED risk
- **0.70–0.89**: HIGH risk
- **0.90–1.0**: CRITICAL risk

### Failure Codes
| Code | Check | Meaning |
|------|-------|---------|
| `MASS_LEAKAGE` | mass_balance | Outputs exceed available scrap (mass created from nothing) |
| `THERMODYNAMIC_IMPOSSIBILITY` | energy_bound | Claimed output needs more energy than metered |
| `UNEXPLAINED_ENERGY_CONSUMPTION` | energy_bound | Metered energy exceeds the envelope for claimed output |
| `CAPACITY_EXCEEDED` | machine_capacity | Claimed output exceeds nameplate rating × hours |

---

## Data Models

### PWPFacility
```python
facility_id: int (PK)
name: str
shed_area_sqm: float
licensed_capacity_tpa: float
polymer_types: list[str]
```

### Machinery
```python
machine_id: int (PK)
facility_id: int (FK)
machine_type: str
rated_kg_per_hour: float
min_kwh_per_kg: float  # SEC lower bound
max_kwh_per_kg: float  # SEC upper bound
```

### WeightTransaction
```python
txn_id: int (PK)
facility_id: int (FK)
timestamp: datetime
stream_type: enum (SCRAP_IN | GRANULE_OUT | REJECT_OUT)
gross_weight_kg: float
tare_weight_kg: float
net_weight_kg: float (derived)
```

**Index:** `(facility_id, timestamp)` for fast windowed aggregations

### EnergyTelemetry
```python
reading_id: int (PK)
facility_id: int (FK)
timestamp: datetime
kwh_consumed: float  # kWh since last reading
```

**Index:** `(facility_id, timestamp)` for fast windowed aggregations

---

## Configuration

### Engine Config (validation_engine.py)

```python
EngineConfig(
    mass_tolerance=0.02,              # 2% weighbridge error margin
    energy_tolerance=0.05,            # 5% smart-meter error margin
    capacity_tolerance=0.01,          # 1% nameplate rounding
    implausible_yield=0.97,           # >97% yield triggers advisory warning
    severity_floor=0.5                # minimum severity for any failed check
)
```

### Database URL

**Development (SQLite):**
```bash
export DATABASE_URL="sqlite://"
```

**Production (PostgreSQL):**
```bash
export DATABASE_URL="postgresql+psycopg2://user:password@localhost:5432/material_verification"
```

---

## Architecture

### Validation Engine Design

**Plugin-based checks:** Each invariant is a separate class implementing the `InvariantCheck` protocol. Add new checks without modifying the engine:

```python
class NewCheck:
    name = "new_check"
    def evaluate(self, ctx: ValidationContext, cfg: EngineConfig) -> CheckResult:
        # Custom logic
        return CheckResult(...)

engine = ValidationEngine(db, checks=[MassBalanceCheck(), EnergyBoundCheck(), NewCheck()])
```

**Immutable context:** All checks receive a frozen `ValidationContext` with pre-aggregated data. Checks never touch the database.

**Noisy-OR scoring:** Independent evidence combination:
```
anomaly_score = 1 - Π(1 - severity_i)
```

Two independent physical violations are more damning than one.

---

## Example Workflow

### 1. Onboard a Facility
```bash
curl -X POST http://localhost:8000/facilities \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Eco Plastics Ltd",
    "shed_area_sqm": 1500,
    "licensed_capacity_tpa": 5000,
    "polymer_types": ["HDPE", "LDPE"]
  }'
# Returns: {"facility_id": 1, ...}
```

### 2. Add Machinery
```bash
curl -X POST http://localhost:8000/facilities/1/machinery \
  -H "Content-Type: application/json" \
  -d '{
    "machine_type": "Twin-screw extruder",
    "rated_kg_per_hour": 400,
    "min_kwh_per_kg": 0.25,
    "max_kwh_per_kg": 0.65
  }'
```

### 3. Ingest Daily Telemetry
```bash
# Weighbridge
for day in {1..30}; do
  curl -X POST http://localhost:8000/telemetry/weight \
    -H "Content-Type: application/json" \
    -d "{
      \"facility_id\": 1,
      \"timestamp\": \"2026-09-$(printf '%02d' $day)T10:00:00\",
      \"stream_type\": \"SCRAP_IN\",
      \"gross_weight_kg\": 28000,
      \"tare_weight_kg\": 8000
    }"
done

# Meter
for day in {1..30}; do
  curl -X POST http://localhost:8000/telemetry/energy \
    -H "Content-Type: application/json" \
    -d "{
      \"facility_id\": 1,
      \"timestamp\": \"2026-09-$(printf '%02d' $day)T10:00:00\",
      \"kwh_consumed\": 2550
    }"
done
```

### 4. Verify the Period
```bash
curl -X GET "http://localhost:8000/verify/1?start_date=2026-09-01T00:00:00&end_date=2026-10-01T00:00:00"
```

---

## Testing

### Run Simulation
```bash
python simulate.py
```

**Expected Output:**
```
=== Scenario A: viable recycler ===
{
  "verdict": "PASS",
  "anomaly_confidence_score": 0.0,
  "risk_band": "LOW",
  ...
}

=== Scenario B: fraudulent recycler ===
{
  "verdict": "FAIL",
  "anomaly_confidence_score": 0.9992,
  "risk_band": "CRITICAL",
  "failure_reasons": [
    { "code": "THERMODYNAMIC_IMPOSSIBILITY", "severity": 0.9976 },
    { "code": "CAPACITY_EXCEEDED", "severity": 0.6564 }
  ]
  ...
}

All simulation assertions passed.
```

---

## Limitations & Future Work

### Current Limitations
- **Operating hours inference**: Uses wall-clock hours or declared hours (capped). Real operation time could be inferred from meter load curves.
- **Single-machine fleet**: Energy envelope uses widest bounds. Multi-machine load balancing not modeled.
- **Bulk ingestion**: API takes one record per request. High-volume facilities need batch endpoints.
- **Schema management**: Tables created at startup. Use Alembic for production migrations.

### Planned Extensions (Phase 3+)
- Machine utilisation curves from meter load analysis
- Cross-facility benchmarking (yield anomalies, energy outliers)
- Historical baseline drift detection
- Real-time streaming verification
- ML-based anomaly scoring (replaces noisy-OR)
- Audit trail & certificate issuance workflow

---

## Production Deployment

### Prerequisites
- PostgreSQL 12+ with PostGIS (optional, for geo-tagging facilities)
- Python 3.11+ with `poetry` or `venv`
- Gunicorn or similar WSGI server
- Nginx reverse proxy (optional)

### Deployment Checklist
- [ ] Set `DATABASE_URL` to production Postgres
- [ ] Run database migrations: `alembic upgrade head`
- [ ] Use Gunicorn: `gunicorn -w 4 -k uvicorn.workers.UvicornWorker main:app`
- [ ] Enable SSL/TLS on reverse proxy
- [ ] Set up logging (CloudWatch, ELK, Datadog)
- [ ] Configure API rate limiting
- [ ] Add health checks: `GET /health`

---

## Support

For issues, questions, or contributions:
- Review `validation_engine.py` for core logic
- Check `schemas.py` for input validation rules
- Extend `api.py` with new endpoints
- Add new checks by implementing `InvariantCheck`

---

**Version:** 0.1.0 (MVP)  
**License:** TBD  
**Last Updated:** 2026-10-18
