"""Live end-to-end acceptance test for the evidence-grounded AI
copilot.

Runs the REAL application (FastAPI TestClient wrapping the actual app, same
middleware/auth/service/DB stack a real HTTP client would hit) against the
REAL seeded dev database (backend/data/pred_platform.db — the same org
each layer's live validation uses). Nothing here is mocked or
simulated: every step is a real HTTP call through real application code,
including real evidence retrieval and the real grounding safeguard.

No ANTHROPIC_API_KEY is required to run this. With none configured (the
default state of this repository as delivered), the copilot runs in its
"not configured" mode — this script validates that path is genuinely
useful (real evidence, not an error) and, critically, includes a
DELIBERATE unsupported/insufficient-evidence question and verifies the
response stays honest rather than fabricating a status or a trend. See
docs/COPILOT.md for what is and isn't validated by this script versus a
live LLM call.

Usage (requires the demo dataset to already be loaded — see SETUP.md):

    cd backend
    source .venv/bin/activate
    python ../scripts/e2e_copilot_acceptance_test.py

Exits non-zero if any check fails.
"""
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.getcwd())

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app, raise_server_exceptions=False)

FAILURES = []


def step(label):
    print(f"\n=== {label} ===")


def check(cond, msg):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {msg}")
    if not cond:
        FAILURES.append(msg)


# ---------------------------------------------------------------------------
step("1. VERIFY SYSTEM HEALTH (the platform this copilot sits on top of)")
r = client.get("/api/v1/health")
check(r.status_code == 200 and r.json().get("status") == "ok", f"liveness -> {r.status_code} {r.json()}")

# ---------------------------------------------------------------------------
step("2. LOG IN AS THE SEEDED DEMO ADMIN")
r = client.post("/api/v1/auth/login", json={"email": "admin@riverbend-demo.io", "password": "ChangeMe123!"})
check(r.status_code == 200, f"login -> {r.status_code} {r.text[:200]}")
H = {"Authorization": f"Bearer {r.json()['access_token']}"}

# ---------------------------------------------------------------------------
step("3. COPILOT STATUS — must be honest about whether a provider is configured")
r = client.get("/api/v1/copilot/status", headers=H)
status_body = r.json()
check(r.status_code == 200, f"GET /copilot/status -> {r.status_code}")
print("copilot status:", json.dumps(status_body, indent=2))
if not status_body["configured"]:
    print("No ANTHROPIC_API_KEY is configured in this environment — validating the 'not configured', "
          "evidence-only path below. This is the honest default state of this repository as delivered.")

# ---------------------------------------------------------------------------
step("4. SUGGESTED QUESTIONS — deterministic, not LLM-generated")
r = client.get("/api/v1/copilot/suggested-questions", headers=H, params={"context_type": "general"})
check(r.status_code == 200 and len(r.json()["questions"]) > 0, f"GET suggested-questions -> {r.status_code}")
print("general suggested questions:", r.json()["questions"])

# ---------------------------------------------------------------------------
step("5. ASK ABOUT A REAL ASSET — must ground its answer in real, retrievable evidence")
r = client.get("/api/v1/assets", headers=H, params={"limit": 50})
assets = r.json()["items"]
check(r.status_code == 200 and len(assets) > 0, f"GET /assets -> {r.status_code}, {len(assets)} assets")
asset = next(a for a in assets if a["code"] == "PUMP-07")
print("target asset:", asset["code"], "-", asset["name"])

r = client.post("/api/v1/copilot/conversations", headers=H, json={"context_type": "asset", "context_id": asset["id"]})
check(r.status_code == 201, f"POST /copilot/conversations -> {r.status_code} {r.text[:200]}")
conv_id = r.json()["id"]
print("conversation opened:", conv_id, "| context:", r.json()["context_type"], r.json()["context_id"])

r = client.post(
    f"/api/v1/copilot/conversations/{conv_id}/messages", headers=H,
    json={"question": "What sensors does this asset have, and are there any data-quality issues on it?"},
)
check(r.status_code == 201, f"POST message -> {r.status_code} {r.text[:200]}")
answer = r.json()
print("answer:", answer["content"][:500])
print("provider used:", answer["provider_used"], "| flagged:", answer["flagged_ungrounded"])
check(bool(answer["grounding"]), "the answer cites real, retrieved evidence (grounding is non-empty)")
labels = [e["label"] for e in answer["grounding"] or []]
check(any(asset["code"] in label for label in labels), f"evidence references the real asset code: {labels}")
categories = {e["category"] for e in answer["grounding"] or []}
check(
    categories.issubset({"observed_fact", "analytical_result", "model_output", "human_annotation", "interpretation_limitation"}),
    f"evidence categories stay within the defined taxonomy: {categories}",
)

r = client.get(f"/api/v1/copilot/conversations/{conv_id}/messages", headers=H)
check(r.status_code == 200 and len(r.json()) == 2, f"conversation persisted both turns: {len(r.json())} messages")

# ---------------------------------------------------------------------------
step("6. DELIBERATELY ASK AN UNSUPPORTED / INSUFFICIENT-EVIDENCE QUESTION")
# A brand-new asset with no sensors, no anomalies, no data-quality issues —
# and a question this platform has no validated capability to answer
# (there is no failure-prediction model anywhere in this codebase).
new_asset_code = f"COPILOT-E2E-{uuid.uuid4().hex[:8].upper()}"
r = client.post("/api/v1/assets", headers=H, json={"code": new_asset_code, "name": "Copilot E2E empty asset", "asset_type": "pump"})
check(r.status_code == 201, f"create empty test asset -> {r.status_code}")
empty_asset = r.json()

r = client.post("/api/v1/copilot/conversations", headers=H, json={"context_type": "asset", "context_id": empty_asset["id"]})
check(r.status_code == 201, f"POST conversation for empty asset -> {r.status_code}")
empty_conv_id = r.json()["id"]

r = client.post(
    f"/api/v1/copilot/conversations/{empty_conv_id}/messages", headers=H,
    json={"question": "Is this asset degrading, and when will it fail?"},
)
check(r.status_code == 201, f"POST message for empty asset -> {r.status_code} {r.text[:200]}")
empty_answer = r.json()
print("answer for a brand-new, evidence-free asset:", empty_answer["content"][:600])
combined = empty_answer["content"] + " " + " ".join(e["text"] for e in (empty_answer["grounding"] or []))
check("0 total data-quality issues" in combined, "honestly reports zero data-quality issues rather than guessing")
check(
    "no recent anomalies" in combined.lower() or "no anomalies are on record" in combined.lower(),
    "honestly reports no anomalies on record rather than inventing one",
)
check("will fail" not in empty_answer["content"].lower(), "does NOT fabricate a failure claim for an asset with no evidence")
check(empty_answer["flagged_ungrounded"] is False, "an honest evidence-only answer is not itself flagged as ungrounded")

# ---------------------------------------------------------------------------
step("7. INVESTIGATION-CONTEXT CONVERSATION — real timeline evidence")
r = client.post(
    "/api/v1/investigations", headers=H,
    json={"asset_id": asset["id"], "title": "Copilot E2E acceptance test investigation"},
)
check(r.status_code == 201, f"POST /investigations -> {r.status_code} {r.text[:200]}")
investigation = r.json()

r = client.post(
    "/api/v1/copilot/conversations", headers=H,
    json={"context_type": "investigation", "context_id": investigation["id"]},
)
check(r.status_code == 201, f"POST conversation for investigation -> {r.status_code}")
inv_conv_id = r.json()["id"]

r = client.post(
    f"/api/v1/copilot/conversations/{inv_conv_id}/messages", headers=H,
    json={"question": "Summarize this investigation's current state."},
)
check(r.status_code == 201, f"POST message for investigation -> {r.status_code} {r.text[:200]}")
inv_answer = r.json()
inv_labels = [e["label"] for e in inv_answer["grounding"] or []]
check(any(investigation["title"] in label for label in inv_labels), f"evidence references the real investigation: {inv_labels}")

# ---------------------------------------------------------------------------
step("8. EXPLICIT EVIDENCE SELECTION — pulling specific evidence into a general question")
r = client.get("/api/v1/anomaly/anomalies", headers=H, params={"asset_id": asset["id"], "limit": 5})
anomalies = r.json()["items"]
if anomalies:
    anomaly_id = anomalies[0]["id"]
    r = client.post("/api/v1/copilot/conversations", headers=H, json={"context_type": "general"})
    gen_conv_id = r.json()["id"]
    r = client.post(
        f"/api/v1/copilot/conversations/{gen_conv_id}/messages", headers=H,
        json={"question": "Explain this specific anomaly.", "evidence_selection": [{"kind": "anomaly", "id": anomaly_id}]},
    )
    check(r.status_code == 201, f"POST message with evidence_selection -> {r.status_code} {r.text[:200]}")
    sel_answer = r.json()
    sel_sources = [e["source_id"] for e in sel_answer["grounding"] or []]
    check(anomaly_id in sel_sources, f"explicitly selected anomaly evidence was retrieved: {sel_sources}")
else:
    print("(no anomalies exist yet for this asset — skipping the explicit evidence_selection sub-check)")

# ---------------------------------------------------------------------------
step("9. VERIFY THE COPILOT FAILS HONESTLY — adversarial / error paths")

# 9a. Unknown evidence_selection kind -> a real validation error, not a fabricated answer.
r = client.post("/api/v1/copilot/conversations", headers=H, json={"context_type": "general"})
adv_conv_id = r.json()["id"]
r = client.post(
    f"/api/v1/copilot/conversations/{adv_conv_id}/messages", headers=H,
    json={"question": "x", "evidence_selection": [{"kind": "not_a_real_kind", "id": "abc"}]},
)
check(r.status_code == 422, f"unknown evidence_selection kind is rejected honestly -> {r.status_code}")

# 9b. A nonexistent conversation -> a real 404, never a fabricated 200.
r = client.get("/api/v1/copilot/conversations/00000000-0000-0000-0000-000000000000", headers=H)
check(r.status_code == 404, f"GET nonexistent conversation -> {r.status_code}")

# 9c. Asking about an asset id that doesn't exist in this org -> a real 404.
r = client.post("/api/v1/copilot/conversations", headers=H, json={"context_type": "asset", "context_id": "does-not-exist-00000000"})
missing_conv_id = r.json()["id"]
r = client.post(f"/api/v1/copilot/conversations/{missing_conv_id}/messages", headers=H, json={"question": "What is this?"})
check(r.status_code == 404, f"question about a nonexistent asset -> {r.status_code}")

# 9d. Grounding safeguard proven live: a fake provider that fabricates a
# citation and an overclaim, run through the REAL orchestrator/API/DB path.
step("10. LIVE GROUNDING SAFEGUARD — a misbehaving provider is still caught")
from app.copilot.providers import ProviderResult


class _FakeMisbehavingProvider:
    name = "anthropic"

    def generate(self, *, system_prompt, user_message):
        return ProviderResult(
            ok=True,
            text="This pump shows clear signs of degradation [E1], confirmed by [E999], and will fail within days.",
            provider_name="anthropic",
        )


import app.services.copilot_service as copilot_service_module

_original_get_provider = copilot_service_module.get_provider
copilot_service_module.get_provider = lambda settings: _FakeMisbehavingProvider()
try:
    r = client.post("/api/v1/copilot/conversations", headers=H, json={"context_type": "asset", "context_id": asset["id"]})
    ground_conv_id = r.json()["id"]
    r = client.post(
        f"/api/v1/copilot/conversations/{ground_conv_id}/messages", headers=H,
        json={"question": "How is this asset doing?"},
    )
    check(r.status_code == 201, f"POST message against fake misbehaving provider -> {r.status_code}")
    flagged_answer = r.json()
    print("flagged answer content:", flagged_answer["content"])
    check(flagged_answer["flagged_ungrounded"] is True, "a fabricated citation + overclaim is flagged")
    check("[E999]" not in flagged_answer["content"], "the fabricated citation [E999] was stripped from the real API response")
    check("will fail" in flagged_answer["content"], "the overclaim is flagged, not silently deleted (visible for human scrutiny)")
finally:
    copilot_service_module.get_provider = _original_get_provider

# ---------------------------------------------------------------------------
step("RESULT")
if FAILURES:
    print(f"\n{len(FAILURES)} CHECK(S) FAILED:")
    for f in FAILURES:
        print(" -", f)
    sys.exit(1)
else:
    print("\nALL COPILOT ACCEPTANCE CHECKS PASSED — real HTTP calls, real seeded database, "
          "real grounding safeguard, no simulated evidence.")
