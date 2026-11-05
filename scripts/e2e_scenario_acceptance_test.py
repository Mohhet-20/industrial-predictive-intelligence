"""Live end-to-end acceptance test for Scenario Simulation
What-If Analysis.

THIS IS SCENARIO ANALYSIS, NOT A PHYSICAL DIGITAL TWIN — see
docs/SCENARIO_SIMULATION.md. This script exercises the REAL application
(FastAPI TestClient wrapping the actual app — same middleware/auth/service/
DB stack a real HTTP client would hit) against the REAL seeded dev database
(backend/data/pred_platform.db, the same org every other e2e script in this
project uses). Nothing here is mocked: every step is a real HTTP call
through real application code, against real historical sensor data, running
the real robust z-score / condition-evidence / degradation / change-
detection pipeline against a perturbed copy of it.

Usage (requires the demo dataset to already be loaded — see SETUP.md):

    cd backend
    source .venv/bin/activate
    python ../scripts/e2e_scenario_acceptance_test.py

Exits non-zero if any check fails.
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.getcwd())

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, raise_server_exceptions=False)

# TestClient's FastAPI startup event (which calls
# app.jobs.handlers.register_job_handlers()) only fires when the client is
# used as a context manager (`with TestClient(app) as client:`). This script
# calls the client directly, module-style, like every other e2e script in
# this project — so it registers job handlers explicitly here rather than
# relying on a lifespan event that would otherwise silently never run,
# which would surface as "No handler registered for job type..." only once
# a scenario run happens to cross into the background-job size threshold.
from app.jobs.handlers import register_job_handlers  # noqa: E402

register_job_handlers()

FAILURES = []


def step(label):
    print(f"\n=== {label} ===")


def check(cond, msg):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {msg}")
    if not cond:
        FAILURES.append(msg)


def wait_for_run(run_id: str, headers: dict, timeout_s: float = 15.0) -> dict:
    """Polls GET /scenarios/runs/{id} until it leaves QUEUED/RUNNING —
    scenario runs above MAX_SCENARIO_ROWS_SYNC execute as a real background
    job (app.jobs.runner.job_runner), the same async pattern
    app.services.anomaly_service.create_detection_run uses."""
    deadline = time.monotonic() + timeout_s
    last = {}
    while time.monotonic() < deadline:
        r = client.get(f"/api/v1/scenarios/runs/{run_id}", headers=headers)
        if r.status_code != 200:
            return {"status": "error", "error_message": f"GET run -> {r.status_code} {r.text[:200]}"}
        last = r.json()
        if last["status"] not in ("queued", "running"):
            return last
        time.sleep(0.2)
    return last


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
step("1. VERIFY SYSTEM HEALTH")
r = client.get("/api/v1/health")
check(r.status_code == 200 and r.json().get("status") == "ok", f"liveness -> {r.status_code} {r.json()}")

# ---------------------------------------------------------------------------
step("2. LOG IN AS THE SEEDED DEMO ADMIN")
r = client.post("/api/v1/auth/login", json={"email": "admin@riverbend-demo.io", "password": "ChangeMe123!"})
check(r.status_code == 200, f"login -> {r.status_code} {r.text[:200]}")
H = {"Authorization": f"Bearer {r.json()['access_token']}"}

r = client.post("/api/v1/auth/login", json={"email": "viewer@riverbend-demo.io", "password": "ChangeMe123!"})
VIEWER_H = {"Authorization": f"Bearer {r.json()['access_token']}"} if r.status_code == 200 else None
if VIEWER_H is None:
    print("(no seeded viewer account — the read-only-role check in step 9 will be skipped)")

# ---------------------------------------------------------------------------
step("3. FIND A REAL SENSOR WITH ENOUGH HISTORY (PUMP-07 / bearing_temp_de)")
r = client.get("/api/v1/assets", headers=H, params={"limit": 50})
assets = r.json()["items"]
check(r.status_code == 200 and len(assets) > 0, f"GET /assets -> {r.status_code}, {len(assets)} assets")
asset = next(a for a in assets if a["code"] == "PUMP-07")
r = client.get(f"/api/v1/assets/{asset['id']}/sensors", headers=H, params={"limit": 50})
sensors = r.json()["items"]
sensor = next(s for s in sensors if s["code"] == "bearing_temp_de")
print("target sensor:", sensor["code"], "-", sensor["name"], f"({sensor['unit']})")

r = client.get(f"/api/v1/stats/sensors/{sensor['id']}", headers=H)
check(r.status_code == 200, f"GET sensor summary stats -> {r.status_code} {r.text[:200]}")
stats = r.json()
check(stats["count"] > 200, f"enough real history to build a scenario: {stats['count']} observations")
first_ts = datetime.fromisoformat(stats["first_timestamp"].replace("Z", "+00:00"))
last_ts = datetime.fromisoformat(stats["last_timestamp"].replace("Z", "+00:00"))
span = last_ts - first_ts
print(f"observed range: {first_ts.isoformat()} .. {last_ts.isoformat()} ({span.days} days, std={stats['std_dev']})")

# Carve out a baseline window (the last third of the observed history) and an
# independent reference window immediately before it (the third before
# that) — mirrors app.services.scenario_service._auto_reference_window's own
# convention, computed here explicitly so this script owns its assumptions.
baseline_end = last_ts
baseline_start = last_ts - span / 3
reference_end = baseline_start
reference_start = baseline_start - span / 3
modification_start = baseline_start + (baseline_end - baseline_start) / 2
duration_seconds = (baseline_end - baseline_start).total_seconds() / 4
magnitude = round(min(3.0 * (stats["std_dev"] or 1.0), 5.0), 2)
print(f"baseline window: {baseline_start.isoformat()} .. {baseline_end.isoformat()}")
print(f"reference window: {reference_start.isoformat()} .. {reference_end.isoformat()}")
print(f"modification: starts {modification_start.isoformat()}, duration {duration_seconds:.0f}s, magnitude {magnitude}")

# ---------------------------------------------------------------------------
step("4. CREATE, VALIDATE, AND RUN A LEVEL_SHIFT SCENARIO")
r = client.post(
    "/api/v1/scenarios", headers=H,
    json={
        "asset_id": asset["id"], "sensor_id": sensor["id"],
        "name": "E2E — what if bearing temp shifts up", "scenario_type": "level_shift",
        "baseline_window_start": iso(baseline_start), "baseline_window_end": iso(baseline_end),
        "reference_window_start": iso(reference_start), "reference_window_end": iso(reference_end),
        "modification_start": iso(modification_start), "modification_duration_seconds": duration_seconds,
        "magnitude": magnitude, "unit": sensor["unit"],
    },
)
check(r.status_code == 201, f"POST /scenarios -> {r.status_code} {r.text[:300]}")
scenario = r.json()
check(scenario["status"] == "draft", f"a new scenario starts DRAFT: {scenario['status']}")

r = client.post(f"/api/v1/scenarios/{scenario['id']}/validate", headers=H)
check(r.status_code == 200, f"POST validate -> {r.status_code} {r.text[:300]}")
scenario = r.json()
check(scenario["status"] == "validated", f"a scenario with real, sufficient data validates cleanly: {scenario}")
check(scenario["validation_result"]["ok"] is True, "validation_result.ok is True")

# Snapshot the real measurements in the baseline window BEFORE running —
# the concrete, executable non-destructive-guarantee check.
r = client.get(
    "/api/v1/measurements", headers=H,
    params={"sensor_ids": sensor["id"], "start_time": iso(baseline_start), "end_time": iso(baseline_end), "limit": 5000},
)
check(r.status_code == 200, f"GET /measurements (before run) -> {r.status_code}")
values_before = [(m["timestamp"], m["value"]) for m in r.json()["items"]]

r = client.post(f"/api/v1/scenarios/{scenario['id']}/runs", headers=H, json={"include_forecast_comparison": True})
check(r.status_code == 201, f"POST /scenarios/{{id}}/runs -> {r.status_code} {r.text[:300]}")
run = r.json()
if run["status"] in ("queued", "running"):
    print(f"run submitted as a background job (background_job_id={run.get('background_job_id')}) — polling for completion...")
    run = wait_for_run(run["id"], H)
check(run["status"] == "succeeded", f"run status: {run['status']} — {run.get('error_message')}")

r = client.get(
    "/api/v1/measurements", headers=H,
    params={"sensor_ids": sensor["id"], "start_time": iso(baseline_start), "end_time": iso(baseline_end), "limit": 5000},
)
values_after = [(m["timestamp"], m["value"]) for m in r.json()["items"]]
check(values_before == values_after, f"NON-DESTRUCTIVE GUARANTEE: real measurements are byte-identical before/after the run ({len(values_before)} rows compared)")

results = run["results"]
check(results["run_type"] == "single", f"run_type: {results['run_type']}")
check(results["baseline"]["label"] == "OBSERVED (real sensor data)", f"baseline label: {results['baseline']['label']}")
check(results["simulated"]["label"] == "SIMULATED SCENARIO RESULT", f"simulated label: {results['simulated']['label']}")
mean_diff = results["comparison"]["value_summary_diff"]["mean"]["absolute_diff"]
check(mean_diff is not None and mean_diff > 0, f"simulated mean is measurably higher than baseline mean: absolute_diff={mean_diff}")
check(len(results["limitations"]) >= 4, f"mandatory limitations are attached: {len(results['limitations'])} items")
check(any("not a physical simulation" in lim for lim in results["limitations"]), "the core 'not a physical simulation' disclaimer is present verbatim")
check(results["provenance"]["scenario_type"] == "level_shift", f"provenance records the scenario type: {results['provenance'].get('scenario_type')}")
print("mean shift observed by the real pipeline:", mean_diff, sensor["unit"])
print("simulated degradation stage:", (results["comparison"]["condition_intelligence_diff"] or {}).get("simulated_stage"))

# ---------------------------------------------------------------------------
step("5. SENSITIVITY SWEEP — same scenario, several magnitudes, no fabricated trend line")
r = client.post(f"/api/v1/scenarios/{scenario['id']}/sensitivity", headers=H, json={"magnitudes": [magnitude / 2, magnitude, magnitude * 2]})
check(r.status_code == 201, f"POST sensitivity -> {r.status_code} {r.text[:300]}")
sweep_run = r.json()
check(sweep_run["status"] == "succeeded", f"sweep status: {sweep_run['status']}")
sweep_results = sweep_run["results"]
check(sweep_results["run_type"] == "sensitivity_sweep", f"run_type: {sweep_results['run_type']}")
check(len(sweep_results["sensitivity"]["points"]) == 3, f"one point per tested magnitude: {len(sweep_results['sensitivity']['points'])}")
check("no trend line is fit" in sweep_results["sensitivity"]["note"].lower(), "sweep note explicitly disclaims fitting a trend")

r = client.post(f"/api/v1/scenarios/{scenario['id']}/sensitivity", headers=H, json={"magnitudes": [float(i) for i in range(1, 10)]})
check(r.status_code == 422, f"a sweep beyond the 8-point bound is rejected -> {r.status_code}")

# ---------------------------------------------------------------------------
step("6. MULTI-RUN COMPARISON — real per-run results side by side, no combined score")
r = client.post("/api/v1/scenarios/compare", headers=H, json={"scenario_run_ids": [run["id"], sweep_run["id"]]})
check(r.status_code == 200, f"POST /scenarios/compare -> {r.status_code} {r.text[:300]}")
compare_data = r.json()["data"]
check(len(compare_data["runs"]) == 2, f"both runs present: {len(compare_data['runs'])}")
check("no combined" in compare_data["note"].lower() or "no fabricated" in compare_data["note"].lower(), "compare response states no combined score is computed")

# ---------------------------------------------------------------------------
step("7. REJECTION, NOT SILENT CLAMPING — an absurd magnitude fails validation honestly")
r = client.post(
    "/api/v1/scenarios", headers=H,
    json={
        "asset_id": asset["id"], "sensor_id": sensor["id"],
        "name": "E2E — deliberately absurd magnitude", "scenario_type": "level_shift",
        "baseline_window_start": iso(baseline_start), "baseline_window_end": iso(baseline_end),
        "reference_window_start": iso(reference_start), "reference_window_end": iso(reference_end),
        "modification_start": iso(modification_start), "modification_duration_seconds": duration_seconds,
        "magnitude": 100000.0, "unit": sensor["unit"],
    },
)
check(r.status_code == 201, f"create absurd scenario -> {r.status_code}")
absurd = r.json()
r = client.post(f"/api/v1/scenarios/{absurd['id']}/validate", headers=H)
check(r.status_code == 200, f"validate absurd scenario -> {r.status_code}")
absurd = r.json()
check(absurd["status"] == "draft", f"an invalid scenario stays DRAFT, not VALIDATED: {absurd['status']}")
check(absurd["validation_result"]["ok"] is False, "validation_result.ok is False for an absurd magnitude")
check(len(absurd["validation_result"]["errors"]) > 0, f"a specific rejection reason is recorded: {absurd['validation_result']['errors']}")

r = client.post(f"/api/v1/scenarios/{absurd['id']}/runs", headers=H, json={})
check(r.status_code == 409, f"running an unvalidated scenario is refused (never silently run anyway) -> {r.status_code}")

# ---------------------------------------------------------------------------
step("8. ARCHIVE — 'delete' archives, never hard-deletes; an archived scenario can't be re-run")
r = client.delete(f"/api/v1/scenarios/{absurd['id']}", headers=H)
check(r.status_code == 200, f"DELETE (archive) -> {r.status_code}")
archived = r.json()
check(archived["status"] == "archived", f"status after archive: {archived['status']}")
r = client.get(f"/api/v1/scenarios/{absurd['id']}", headers=H)
check(r.status_code == 200, "the archived scenario is still fully readable (never hard-deleted)")
r = client.post(f"/api/v1/scenarios/{absurd['id']}/validate", headers=H)
check(r.status_code == 409, f"an archived scenario can't be re-validated -> {r.status_code}")

# ---------------------------------------------------------------------------
step("9. AUTHORIZATION — a viewer can read scenarios but cannot create or run them")
if VIEWER_H:
    r = client.get(f"/api/v1/scenarios/{scenario['id']}", headers=VIEWER_H)
    check(r.status_code == 200, f"viewer can read a scenario -> {r.status_code}")
    r = client.post(
        "/api/v1/scenarios", headers=VIEWER_H,
        json={
            "asset_id": asset["id"], "sensor_id": sensor["id"], "name": "viewer should not be able to do this",
            "scenario_type": "level_shift", "baseline_window_start": iso(baseline_start), "baseline_window_end": iso(baseline_end),
            "modification_start": iso(modification_start), "magnitude": 1.0,
        },
    )
    check(r.status_code == 403, f"viewer cannot create a scenario -> {r.status_code}")
r = client.get(f"/api/v1/scenarios/{scenario['id']}")
check(r.status_code in (401, 403), f"unauthenticated request is refused -> {r.status_code}")

# ---------------------------------------------------------------------------
step("10. MULTI_SENSOR SCENARIO — independent per-sensor scenarios, no fabricated cross-sensor link")
other_sensor = next((s for s in sensors if s["code"] == "vibration_rms"), None)
if other_sensor:
    r = client.post(
        "/api/v1/scenarios", headers=H,
        json={
            "asset_id": asset["id"], "sensor_id": sensor["id"], "sensor_scope": [other_sensor["id"]],
            "name": "E2E — independent multi-sensor scenario", "scenario_type": "multi_sensor",
            "baseline_window_start": iso(baseline_start), "baseline_window_end": iso(baseline_end),
            "reference_window_start": iso(reference_start), "reference_window_end": iso(reference_end),
            "parameters": {
                "per_sensor": [
                    {"sensor_id": sensor["id"], "scenario_type": "level_shift", "modification_start": iso(modification_start), "modification_duration_seconds": duration_seconds, "magnitude": magnitude, "unit": sensor["unit"]},
                    {"sensor_id": other_sensor["id"], "scenario_type": "drift", "modification_start": iso(modification_start), "magnitude": 0.5, "unit": other_sensor["unit"]},
                ]
            },
        },
    )
    check(r.status_code == 201, f"create multi_sensor scenario -> {r.status_code} {r.text[:300]}")
    ms_scenario = r.json()
    r = client.post(f"/api/v1/scenarios/{ms_scenario['id']}/validate", headers=H)
    ms_validated = r.json()
    check(r.status_code == 200 and ms_validated["status"] == "validated", f"multi_sensor validation -> {ms_validated.get('status')} {ms_validated.get('validation_result')}")
    if ms_validated.get("status") == "validated":
        r = client.post(f"/api/v1/scenarios/{ms_scenario['id']}/runs", headers=H, json={})
        check(r.status_code == 201, f"POST multi_sensor run -> {r.status_code} {r.text[:300]}")
        ms_run = r.json()
        if r.status_code == 201 and ms_run["status"] in ("queued", "running"):
            ms_run = wait_for_run(ms_run["id"], H)
        check(ms_run.get("status") == "succeeded", f"multi_sensor run -> {ms_run.get('status')} — {ms_run.get('error_message')}")
        ms_results = ms_run.get("results") or {}
        check(ms_results.get("run_type") == "multi_sensor", f"run_type: {ms_results.get('run_type')}")
        check(sensor["id"] in ms_results.get("per_sensor", {}) and other_sensor["id"] in ms_results.get("per_sensor", {}), "both sensors have independent results")
        check(any("independently" in lim for lim in ms_results.get("limitations", [])), "limitations explicitly state each sensor ran independently")
    else:
        print("(multi_sensor scenario failed validation — see reported errors above; skipping the run sub-checks)")
else:
    print("(no vibration_rms sensor found — skipping the multi_sensor sub-check)")

# ---------------------------------------------------------------------------
step("11. INVESTIGATION EVIDENCE LINK — attach a real scenario run without altering it")
r = client.post("/api/v1/investigations", headers=H, json={"asset_id": asset["id"], "title": "E2E scenario investigation", "primary_sensor_id": sensor["id"]})
check(r.status_code == 201, f"POST /investigations -> {r.status_code}")
investigation = r.json()
r = client.post(
    f"/api/v1/investigations/{investigation['id']}/evidence", headers=H,
    json={"evidence_type": "scenario_result", "evidence_id": run["id"], "relation_note": "e2e what-if comparison"},
)
check(r.status_code == 201, f"attach scenario evidence -> {r.status_code} {r.text[:300]}")
r = client.get(f"/api/v1/scenarios/runs/{run['id']}", headers=H)
check(r.json()["results"] == run["results"], "attaching evidence did not alter the scenario run's results")

# ---------------------------------------------------------------------------
step("12. COPILOT GROUNDING — a scenario-context conversation always labels results SIMULATED")
r = client.post("/api/v1/copilot/conversations", headers=H, json={"context_type": "scenario", "context_id": scenario["id"]})
check(r.status_code == 201, f"POST scenario-context conversation -> {r.status_code}")
conv = r.json()
r = client.post(f"/api/v1/copilot/conversations/{conv['id']}/messages", headers=H, json={"question": "What did this scenario show?"})
check(r.status_code == 201, f"POST message -> {r.status_code} {r.text[:300]}")
message = r.json()
grounding_text = " ".join(item.get("text", "") for item in (message.get("grounding") or []))
check("simulated" in grounding_text.lower(), "copilot grounding text labels the scenario result simulated")
check("hypothetical" in grounding_text.lower(), "copilot grounding text calls the scenario hypothetical")

r = client.post("/api/v1/copilot/conversations", headers=H, json={"context_type": "investigation", "context_id": investigation["id"]})
inv_conv = r.json()
r = client.post(f"/api/v1/copilot/conversations/{inv_conv['id']}/messages", headers=H, json={"question": "Summarize the evidence on this investigation."})
inv_grounding_text = " ".join(item.get("text", "") for item in (r.json().get("grounding") or []))
check("not a real observation" in inv_grounding_text, "a scenario linked into an investigation is still labeled 'not a real observation' by the copilot")

# ---------------------------------------------------------------------------
step("RESULT")
if FAILURES:
    print(f"\n{len(FAILURES)} CHECK(S) FAILED:")
    for f in FAILURES:
        print(" -", f)
    sys.exit(1)
else:
    print("\nALL SCENARIO SIMULATION ACCEPTANCE CHECKS PASSED — real HTTP calls, real seeded database, "
          "real historical data perturbed non-destructively, real analytical pipeline reused, every "
          "result labeled SIMULATED end to end.")
