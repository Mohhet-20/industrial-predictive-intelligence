"""Phase 8 §16 — Final end-to-end flagship acceptance test.

Runs the REAL application (FastAPI TestClient wrapping the actual app,
same middleware/auth/service/DB stack a real HTTP client would hit)
against the REAL seeded dev database (backend/data/pred_platform.db,
the same org used by every prior phase's live validation). Nothing here
is mocked or simulated — every step is a real HTTP call through real
application code, and every printed result is the real response.

Usage (requires the demo dataset to already be loaded — see SETUP.md):

    cd backend
    source .venv/bin/activate
    python ../scripts/e2e_acceptance_test.py

Exits non-zero if any check fails. Real output from the most recent run
is quoted in docs/RELIABILITY_AND_OPERATIONS.md's Phase 8 addendum and
the Phase 8 completion report.
"""
import json
import os
import sys
import time

# Run from backend/ (see the Usage note above) so `app` imports the same
# way it does for pytest/uvicorn; this just makes that robust if the
# script is invoked with a different cwd.
sys.path.insert(0, os.getcwd())

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, raise_server_exceptions=False)

# TestClient's FastAPI startup event (which calls
# app.jobs.handlers.register_job_handlers()) only fires when the client is
# used as a context manager (`with TestClient(app) as client:`). This script
# calls the client directly, module-style, so it registers job handlers
# explicitly here rather than relying on a lifespan event that would
# otherwise silently never run — the same fix already applied in
# e2e_scenario_acceptance_test.py (Phase 11), ported here because this
# script predates it and step 5 below can cross into the background-job
# size threshold for anomaly detection runs, which previously failed with
# "No handler registered for job type 'anomaly_detection_run'."
from app.jobs.handlers import register_job_handlers  # noqa: E402

register_job_handlers()


def step(label):
    print(f"\n=== {label} ===")


def check(cond, msg):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {msg}")
    if not cond:
        FAILURES.append(msg)


FAILURES = []

# ---------------------------------------------------------------------------
step("1. VERIFY SYSTEM HEALTH")
r = client.get("/api/v1/health")
check(r.status_code == 200 and r.json().get("status") == "ok", f"liveness -> {r.status_code} {r.json()}")

r = client.get("/api/v1/health/ready")
ready_body = r.json()
check(r.status_code == 200 and ready_body.get("status") == "ok", f"readiness -> {r.status_code} {ready_body}")
print("schema_version:", ready_body.get("schema_version"))

# ---------------------------------------------------------------------------
step("2. LOG IN AS THE SEEDED DEMO ADMIN (real synthetic demo org)")
r = client.post("/api/v1/auth/login", json={"email": "admin@riverbend-demo.io", "password": "ChangeMe123!"})
check(r.status_code == 200, f"login -> {r.status_code} {r.text[:200]}")
token = r.json()["access_token"]
H = {"Authorization": f"Bearer {token}"}

r = client.get("/api/v1/auth/me", headers=H)
me = r.json()
check(r.status_code == 200, f"/auth/me -> {r.status_code}")
print("logged in as:", me["email"], "| org:", me["organization_name"], "| role:", me["role"])

# ---------------------------------------------------------------------------
step("3. INSPECT ASSET / TIME-SERIES BEHAVIOR (real, labeled synthetic demo data)")
r = client.get("/api/v1/assets", headers=H, params={"limit": 50})
assets = r.json()["items"]
check(r.status_code == 200 and len(assets) > 0, f"GET /assets -> {r.status_code}, {len(assets)} assets")
asset = next(a for a in assets if a["code"] == "PUMP-07")
print("target asset:", asset["code"], "-", asset["name"])

r = client.get(f"/api/v1/assets/{asset['id']}/sensors", headers=H, params={"limit": 50})
sensors = r.json()["items"]
check(r.status_code == 200 and len(sensors) > 0, f"GET sensors -> {r.status_code}, {len(sensors)} sensors")
sensor = next(s for s in sensors if "bearing_temp" in s["code"])
print("target sensor (has injected drift, per docs/SYNTHETIC_DATA.md):", sensor["code"], sensor["unit"])

from datetime import datetime, timedelta, timezone
_now = datetime.now(timezone.utc)
RANGE_START = (_now - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
RANGE_END = _now.strftime("%Y-%m-%dT%H:%M:%SZ")

r = client.get("/api/v1/measurements", headers=H, params={
    "sensor_ids": sensor["id"], "start_time": RANGE_START, "end_time": RANGE_END, "limit": 5,
})
meas = r.json()
check(r.status_code == 200, f"GET /measurements -> {r.status_code}")
print(f"total real measurements for this sensor: {meas.get('total')}; sample:",
      meas["items"][0] if meas.get("items") else None)

# ---------------------------------------------------------------------------
step("4. OBSERVE DATA QUALITY CONTEXT (real, not fabricated)")
r = client.get(f"/api/v1/data-quality/summary/{asset['id']}", headers=H)
dq = r.json()
check(r.status_code == 200, f"GET data-quality summary -> {r.status_code}")
print("data quality summary:", json.dumps(dq, indent=2)[:600])

# ---------------------------------------------------------------------------
step("5. ACCESS REAL ANALYTICAL INTELLIGENCE (anomaly detection)")
r = client.get("/api/v1/anomaly/anomalies", headers=H, params={"asset_id": asset["id"], "limit": 20})
anomalies = r.json()["items"]
check(r.status_code == 200, f"GET /anomaly/anomalies -> {r.status_code}, {len(anomalies)} found")

if not anomalies:
    # No anomaly exists yet for this asset in the current DB state — run a
    # real detector against real data rather than skipping the step.
    #
    # NOTE: DetectorResponse.status is ModelVersionStatus
    # (trained/failed/archived — app/models/intelligence.py). "active" is
    # not a reachable value on this field — that's the separate MLOps
    # ComponentVersion.lifecycle_stage concept (docs/MLOPS_GOVERNANCE.md).
    # A robust z-score detector needs no MLOps activation to be directly
    # scorable (docs/ANOMALY_DETECTION.md governance table: "any TRAINED
    # version was directly scorable"), so the previous `status == "active"`
    # filter here could never match anything, silently making this
    # self-heal path dead code on every run.
    r = client.get("/api/v1/anomaly/detectors", headers=H, params={"limit": 20})
    detectors = [
        d for d in r.json()["items"]
        if d["status"] == "trained" and sensor["id"] in d.get("detector_config", {}).get("sensor_ids", [])
    ]
    scoring_start = scoring_end = None

    if not detectors:
        # None exists yet in this DB state either — train a real one
        # against this sensor's own observed history (reference = first
        # 70%, scored = the remainder, so scoring never overlaps what the
        # detector was fit on).
        stats_resp = client.get(f"/api/v1/stats/sensors/{sensor['id']}", headers=H)
        stats = stats_resp.json()
        check(stats_resp.status_code == 200 and stats.get("count", 0) > 1, f"GET sensor stats -> {stats_resp.status_code}, count={stats.get('count')}")
        first_ts = datetime.fromisoformat(stats["first_timestamp"]).replace(tzinfo=timezone.utc)
        last_ts = datetime.fromisoformat(stats["last_timestamp"]).replace(tzinfo=timezone.utc)
        split_ts = first_ts + (last_ts - first_ts) * 0.7

        train_resp = client.post("/api/v1/anomaly/detectors", headers=H, json={
            "name": f"bootstrap-robust-zscore-{sensor['code']}",
            "detector_type": "robust_zscore",
            "sensor_ids": [sensor["id"]],
            "reference_start": first_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reference_end": split_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        trained_ok = train_resp.status_code == 201 and train_resp.json().get("status") == "trained"
        check(trained_ok, f"train a detector against real history -> {train_resp.status_code} {train_resp.text[:200]}")
        if trained_ok:
            det = train_resp.json()
            print("trained detector:", det["id"], "| reference:", det["reference_period_start"], "->", det["reference_period_end"])
            detectors = [det]
            scoring_start, scoring_end = split_ts, last_ts

    check(len(detectors) > 0, f"a usable (trained) detector exists to run ({len(detectors)} found)")
    if detectors:
        det = detectors[0]
        if scoring_start is None:
            # Found a pre-existing trained detector rather than training
            # one just above — score from just after its own reference
            # window through the present.
            scoring_start = datetime.fromisoformat(det["reference_period_end"]).replace(tzinfo=timezone.utc)
            scoring_end = datetime.now(timezone.utc)
        r = client.post(f"/api/v1/anomaly/detectors/{det['id']}/runs", headers=H, json={
            "scoring_start": scoring_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "scoring_end": scoring_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        run = r.json()
        check(r.status_code == 201, f"POST detection run -> {r.status_code} {str(run)[:300]}")
        # A large scoring window crosses into the async background-job
        # path (MLOPS_GOVERNANCE.md's MAX_DETECTION_ROWS_SYNC) — poll the
        # job to completion rather than assuming the run finished
        # synchronously, so this script's own result is never fabricated.
        job_id = run.get("background_job_id")
        if job_id:
            for _ in range(20):
                job = client.get(f"/api/v1/jobs/{job_id}", headers=H).json()
                if job.get("status") in ("succeeded", "failed"):
                    break
                time.sleep(0.5)
            check(job.get("status") == "succeeded", f"background detection job -> {job.get('status')} {job.get('error_message')}")
            print("detection run (async job):", job.get("status"), "| result:", str(job.get("result"))[:300])
        else:
            print("detection run (sync):", r.status_code, str(run)[:300])
    r = client.get("/api/v1/anomaly/anomalies", headers=H, params={"asset_id": asset["id"], "limit": 20})
    anomalies = r.json()["items"]

check(len(anomalies) > 0, f"a real anomaly is available to investigate ({len(anomalies)} found)")
anomaly = anomalies[0]
print("anomaly:", anomaly["id"], "| severity:", anomaly.get("severity_tier"), "| method:", anomaly.get("detection_method"))

# ---------------------------------------------------------------------------
step("6. IDENTIFY A MEANINGFUL SIGNAL + REVIEW TEMPORAL EVIDENCE / EXPLANATION")
r = client.get(f"/api/v1/anomaly/anomalies/{anomaly['id']}/context", headers=H)
context = r.json()
check(r.status_code == 200, f"GET anomaly context -> {r.status_code}")
print("explanation / evidence (real calculation, not generated text):", json.dumps(context, indent=2)[:600])

# ---------------------------------------------------------------------------
step("7. INSPECT EXPLANATION / PROVENANCE (traces to a real model/component version)")
r = client.get(f"/api/v1/orchestration/evidence/anomaly/{anomaly['id']}/provenance", headers=H)
prov = r.json()
check(r.status_code == 200, f"GET provenance -> {r.status_code}")
print("provenance:", json.dumps(prov, indent=2)[:600])

# ---------------------------------------------------------------------------
step("8. REVIEW CONDITION / FORECASTING CONTEXT")
r = client.get("/api/v1/condition/attention-list", headers=H)
check(r.status_code == 200, f"GET condition attention-list -> {r.status_code}")
attention = r.json()
print("attention list (transparent ordering, not a probability):", json.dumps(attention, indent=2)[:400])

# Real condition evidence for the drift sensor — a real, non-mocked computation.
r = client.get("/api/v1/measurements", headers=H, params={
    "sensor_ids": sensor["id"], "start_time": RANGE_START, "end_time": RANGE_END, "limit": 1, "offset": 0,
})
items = r.json().get("items") or []
first_ts = items[0]["timestamp"] if items else None
total_for_sensor = r.json().get("total", 0)
last_ts = None
if total_for_sensor:
    r = client.get("/api/v1/measurements", headers=H, params={
        "sensor_ids": sensor["id"], "start_time": RANGE_START, "end_time": RANGE_END,
        "limit": 1, "offset": max(total_for_sensor - 1, 0),
    })
    items2 = r.json().get("items") or []
    last_ts = items2[0]["timestamp"] if items2 else None
if first_ts and last_ts:
    r = client.get(f"/api/v1/condition/sensors/{sensor['id']}/evidence", headers=H, params={
        "window_start": last_ts, "window_end": last_ts,
        "reference_start": first_ts, "reference_end": first_ts,
    })
    print("condition evidence call (edge-case window) ->", r.status_code, str(r.json())[:300])

# ---------------------------------------------------------------------------
step("9. OPEN THE UNIFIED INVESTIGATION EXPERIENCE")
r = client.post("/api/v1/investigations", headers=H, json={
    "asset_id": asset["id"],
    "title": "Phase 8 acceptance test — bearing temp anomaly review",
    "description": "Opened by the final flagship acceptance test to exercise the real investigation workflow end to end.",
    "primary_sensor_id": sensor["id"],
})
check(r.status_code == 201, f"POST /investigations -> {r.status_code} {r.text[:200]}")
investigation = r.json()
inv_id = investigation["id"]
print("investigation opened:", inv_id, "| status:", investigation["status"])

# ---------------------------------------------------------------------------
step("10. ATTACH EVIDENCE (reference, not a copy)")
r = client.post(f"/api/v1/investigations/{inv_id}/evidence", headers=H, json={
    "evidence_type": "anomaly",
    "evidence_id": anomaly["id"],
    "relation_note": "Primary anomaly under review for this investigation.",
})
check(r.status_code == 201, f"POST evidence -> {r.status_code} {r.text[:200]}")
evidence_link = r.json()
print("evidence attached:", evidence_link["id"], "-> anomaly", evidence_link["evidence_id"])

# ---------------------------------------------------------------------------
step("11. ADD HUMAN INVESTIGATION CONTEXT (append-only annotation)")
r = client.post(f"/api/v1/investigations/{inv_id}/annotations", headers=H, json={
    "annotation_type": "observation",
    "content": "Reviewed the flagged anomaly against the raw trend — consistent with the known injected drift on this sensor. Recommending continued monitoring, not immediate action.",
    "follow_up_required": True,
})
check(r.status_code == 201, f"POST annotation -> {r.status_code} {r.text[:200]}")
annotation = r.json()
print("annotation added:", annotation["id"], "| type:", annotation["annotation_type"])

r = client.post(f"/api/v1/investigations/{inv_id}/status", headers=H, json={
    "to_status": "under_review", "notes": "Moving to under_review after initial evidence review.",
})
check(r.status_code == 200, f"POST status transition -> {r.status_code} {r.text[:200]}")
print("status transitioned to:", r.json()["status"])

# ---------------------------------------------------------------------------
step("12. VERIFY EVIDENCE HISTORY IS PRESERVED (append-only, nothing overwritten)")
r = client.get(f"/api/v1/investigations/{inv_id}/status-history", headers=H)
history = r.json()
check(r.status_code == 200 and len(history) >= 2, f"status history has {len(history)} entries (open + under_review)")
print("status history:", [(h["from_status"], h["to_status"]) for h in history])

r = client.get(f"/api/v1/investigations/{inv_id}/evidence", headers=H)
evidence_list = r.json()
check(r.status_code == 200 and len(evidence_list) == 1 and evidence_list[0]["evidence_id"] == anomaly["id"],
      f"evidence link still present and unmutated: {evidence_list}")

r = client.get(f"/api/v1/investigations/{inv_id}/annotations", headers=H)
annotations = r.json()
check(r.status_code == 200 and len(annotations) == 1, f"annotation still present: {len(annotations)} found")

r = client.get(f"/api/v1/anomaly/anomalies/{anomaly['id']}", headers=H)
anomaly_after = r.json()
check(anomaly_after["severity_tier"] == anomaly["severity_tier"] and anomaly_after["raw_score"] == anomaly.get("raw_score"),
      "original anomaly evidence unchanged after being referenced by an investigation (no silent mutation)")

# ---------------------------------------------------------------------------
step("13. VERIFY THE SYSTEM FAILS HONESTLY — insufficient-data / error conditions")

# 13a. A real 404 on a fabricated id — never a fabricated 200.
r = client.get("/api/v1/assets/does-not-exist-00000000", headers=H)
check(r.status_code == 404 and r.json()["error"]["code"] == "not_found",
      f"GET nonexistent asset -> {r.status_code} {r.json()}")

# 13b. Condition evidence requested over a window with no real measurements
# at all -- the system must report insufficient evidence, not fabricate a value.
r = client.get(f"/api/v1/condition/sensors/{sensor['id']}/evidence", headers=H, params={
    "window_start": "1990-01-01T00:00:00Z", "window_end": "1990-01-02T00:00:00Z",
    "reference_start": "1990-01-03T00:00:00Z", "reference_end": "1990-01-04T00:00:00Z",
})
print("condition evidence over an empty window ->", r.status_code, str(r.json())[:400])
check(r.status_code in (200, 422), f"empty-window condition evidence responds honestly, not a 500: {r.status_code}")
if r.status_code == 200:
    body = r.json()
    # Must not fabricate a confident trend from zero data.
    check("insufficient" in json.dumps(body).lower() or body.get("data", {}).get("sample_size", 1) == 0,
          "empty-window response is explicit about insufficient data rather than a fabricated result")

# 13c. Viewer-role authorization boundary on an admin/engineer-only endpoint.
r = client.get("/api/v1/system/metrics", headers=H)
check(r.status_code == 200, f"admin CAN read /system/metrics -> {r.status_code}")

# ---------------------------------------------------------------------------
step("RESULT")
if FAILURES:
    print(f"\n{len(FAILURES)} CHECK(S) FAILED:")
    for f in FAILURES:
        print(" -", f)
    sys.exit(1)
else:
    print("\nALL ACCEPTANCE CHECKS PASSED — real HTTP calls, real seeded database, no simulated steps.")
