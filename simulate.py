"""
End-to-end simulation using FastAPI's TestClient (in-memory SQLite).

Scenario A - physically viable recycler  -> expect PASS
Scenario B - fraudulent recycler         -> expect FAIL
    Claims ~500 t of granules while the meter only shows energy for ~50 t,
    and while the single extruder can physically make only ~360 t in 30 days.

Run:  python simulate.py
"""
import json
import os

# Must be set before the app imports database.py. SQLite keeps the sim self-contained.
os.environ["DATABASE_URL"] = "sqlite://"

from datetime import datetime, timedelta  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402

PERIOD_START = datetime(2026, 9, 1)
PERIOD_END = datetime(2026, 10, 1)  # 30 days = 720 h, exclusive end
DAYS = 30

# Extruder: 500 kg/h nameplate, 0.30-0.70 kWh/kg specific energy.
MACHINE = {
    "machine_type": "Twin-screw extruder",
    "rated_kg_per_hour": 500,
    "min_kwh_per_kg": 0.30,
    "max_kwh_per_kg": 0.70,
}


def create_facility(client: TestClient, name: str) -> int:
    fac = client.post("/facilities", json={
        "name": name, "shed_area_sqm": 1200, "licensed_capacity_tpa": 6000,
        "polymer_types": ["HDPE", "LDPE", "PP"],
    })
    assert fac.status_code == 201, fac.text
    fid = fac.json()["facility_id"]
    m = client.post(f"/facilities/{fid}/machinery", json=MACHINE)
    assert m.status_code == 201, m.text
    return fid


def inject_period(client, fid, scrap_in_kg, granules_kg, rejects_kg, energy_kwh):
    """Spread monthly totals evenly across daily weighbridge tickets and meter readings."""
    for day in range(DAYS):
        ts = (PERIOD_START + timedelta(days=day, hours=10)).isoformat()
        for stream, total in (("SCRAP_IN", scrap_in_kg), ("GRANULE_OUT", granules_kg),
                              ("REJECT_OUT", rejects_kg)):
            net = total / DAYS
            tare = 8000.0  # empty truck
            r = client.post("/telemetry/weight", json={
                "facility_id": fid, "timestamp": ts, "stream_type": stream,
                "gross_weight_kg": net + tare, "tare_weight_kg": tare,
            })
            assert r.status_code == 201, r.text
        r = client.post("/telemetry/energy", json={
            "facility_id": fid, "timestamp": ts, "kwh_consumed": energy_kwh / DAYS,
        })
        assert r.status_code == 201, r.text


def verify(client, fid):
    r = client.get(f"/verify/{fid}", params={
        "start_date": PERIOD_START.isoformat(), "end_date": PERIOD_END.isoformat(),
    })
    assert r.status_code == 200, r.text
    return r.json()


def main():
    with TestClient(app) as client:  # context manager runs lifespan -> creates tables
        # ---- Scenario A: viable ------------------------------------------- #
        good = create_facility(client, "Honest Polymers Pvt Ltd")
        # 200 t in, 170 t granules (85% yield), 20 t rejects, 10 t left in stock.
        # 170 t * 0.45 kWh/kg (mid-range SEC) = 76,500 kWh. Utilisation ~47% of nameplate.
        inject_period(client, good, scrap_in_kg=200_000, granules_kg=170_000,
                      rejects_kg=20_000, energy_kwh=76_500)
        report_a = verify(client, good)
        print("=== Scenario A: viable recycler ===")
        print(json.dumps(report_a, indent=2))
        assert report_a["verdict"] == "PASS", report_a["failure_reasons"]
        assert report_a["anomaly_confidence_score"] == 0.0

        # ---- Scenario B: fraud -------------------------------------------- #
        bad = create_facility(client, "Paper Tiger Recyclers")
        # Claims 500 t of granules from 520 t of "scrap" (mass balance looks fine on paper),
        # but meter shows 22,500 kWh = energy for only ~50 t at 0.45 kWh/kg.
        inject_period(client, bad, scrap_in_kg=520_000, granules_kg=500_000,
                      rejects_kg=15_000, energy_kwh=22_500)
        report_b = verify(client, bad)
        print("\n=== Scenario B: fraudulent recycler ===")
        print(json.dumps(report_b, indent=2))
        assert report_b["verdict"] == "FAIL"
        codes = {f["code"] for f in report_b["failure_reasons"]}
        assert "THERMODYNAMIC_IMPOSSIBILITY" in codes
        assert "CAPACITY_EXCEEDED" in codes
        assert report_b["anomaly_confidence_score"] > 0.9

        # ---- Bonus: claim mode with mass leakage --------------------------- #
        r = client.post("/verify/claim", json={
            "facility_id": good,
            "period_start": PERIOD_START.isoformat(),
            "period_end": PERIOD_END.isoformat(),
            "claimed_output_kg": 300_000,  # more than the 200 t that ever entered
        })
        assert r.status_code == 200, r.text
        report_c = r.json()
        print("\n=== Bonus: inflated claim on the honest facility ===")
        print(json.dumps({k: report_c[k] for k in
                          ("verdict", "anomaly_confidence_score", "risk_band", "failure_reasons")},
                         indent=2))
        assert report_c["verdict"] == "FAIL"
        assert "MASS_LEAKAGE" in {f["code"] for f in report_c["failure_reasons"]}

    print("\nAll simulation assertions passed.")


if __name__ == "__main__":
    main()
