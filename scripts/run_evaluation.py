"""Reproducible evaluation pipeline.

Runs every app.evaluation.* module and writes ONE fixed-name JSON artifact
plus ONE fixed-name Markdown summary (overwritten each run, not
accumulated — asks for "clear commands... not huge
committed artifacts"). This is the single command that reproduces every
number cited in docs/SCIENTIFIC_VALIDATION.md.

Two kinds of checks run here:
  - PURE, deterministic modules (app.evaluation.robustness, dq_impact,
    explainability) — no database needed, fixed seed=42, byte-identical
    results on every machine and every run.
  - DB-BACKED modules (prioritization, copilot_eval, adversarial) — run
    against the REAL application (FastAPI TestClient wrapping the actual
    app, same as scripts/e2e_acceptance_test.py) and a real database
    session. Each of these creates its OWN freshly-named organization
    (unique per run) so repeat runs never collide with each other or with
    the seeded demo org's data.

Usage (mirrors scripts/e2e_acceptance_test.py):

    cd backend
    source .venv/bin/activate
    python ../scripts/run_evaluation.py

Exits non-zero if any DB-backed check's outcome is not one of its expected
"passing" values — the same convention scripts/ci_check.sh uses for its
own gates. Pure-module scenario findings are descriptive (already asserted
by tests/test_evaluation_*.py, which is where correctness is actually
gated) rather than pass/fail here.
"""
import json
import os
import sys
from datetime import datetime, timezone

# Run from backend/ (see the Usage note above) so `app` imports the same
# way it does for pytest/uvicorn.
sys.path.insert(0, os.getcwd())

from fastapi.testclient import TestClient  # noqa: E402

from app.db.session import SessionLocal  # noqa: E402
from app.evaluation import adversarial, copilot_eval, dq_impact, explainability, prioritization, robustness  # noqa: E402
from app.main import app  # noqa: E402
from app.services import auth_service  # noqa: E402

RESULTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "evaluation_results")
JSON_PATH = os.path.join(RESULTS_DIR, "latest_evaluation_run.json")
MARKDOWN_PATH = os.path.join(RESULTS_DIR, "latest_evaluation_run.md")

_DB_CHECK_PASS_OUTCOMES = {
    "reproducible", "context_only_confirmed", "weak_signals_outrank_strong", "strong_signal_still_ranks_higher",
    "agreement_bonus_applied_correctly", "empty_result_handled_safely", "age_not_scored_confirmed",
    "PASS", "safe_failure", "safe_and_documented", "safe", "provenance_preserved", "disagreement_preserved",
}


def _run_db_backed_sections(run_started_at: str) -> tuple[dict, list[str]]:
    """Registers one fresh, uniquely-named organization for this run (so
    repeat runs never collide) and runs the three DB-backed evaluation
    modules against it through the real HTTP/service stack."""
    client = TestClient(app)
    db = SessionLocal()
    failures: list[str] = []
    try:
        org_name = f"Evaluation Run {run_started_at}"
        org, admin = auth_service.register_organization(
            db, organization_name=org_name, admin_email=f"eval-{run_started_at.replace(':', '-')}@evaluation.local",
            admin_password="EvalRunPass123!", admin_full_name="Evaluation Runner",
        )
        headers = {"Authorization": f"Bearer {auth_service.issue_token(admin)}"}

        prioritization_result = prioritization.run_all(db)
        copilot_result = copilot_eval.run_all(client, headers, db, organization_id=org.id)
        adversarial_result = adversarial.run_all(client, headers, db)

        for section_name, result in (("prioritization", prioritization_result), ("copilot_groundedness", copilot_result), ("adversarial_and_failure_cases", adversarial_result)):
            for check in result["checks" if "checks" in result else "cases"]:
                outcome = check.get("outcome")
                if outcome not in _DB_CHECK_PASS_OUTCOMES:
                    failures.append(f"{section_name}.{check.get('check') or check.get('case')}: {outcome}")

        return {
            "prioritization": prioritization_result,
            "copilot_groundedness": copilot_result,
            "adversarial_and_failure_cases": adversarial_result,
        }, failures
    finally:
        db.close()


def main() -> int:
    run_started_at = datetime.now(timezone.utc).isoformat()
    print(f"=== Evaluation pipeline — started {run_started_at} ===\n")

    print("[1/6] app.evaluation.robustness (pure, seed=42)...")
    robustness_result = robustness.run_all()

    print("[2/6] app.evaluation.dq_impact (pure, seed-based)...")
    dq_impact_result = dq_impact.run_all()

    print("[3/6] app.evaluation.explainability (pure, seed-based)...")
    explainability_result = explainability.run_all()

    print("[4/6] app.evaluation.prioritization (DB-backed)...")
    print("[5/6] app.evaluation.copilot_eval (DB-backed, real HTTP stack)...")
    print("[6/6] app.evaluation.adversarial (DB-backed, real HTTP stack)...")
    db_backed_results, failures = _run_db_backed_sections(run_started_at)

    run_finished_at = datetime.now(timezone.utc).isoformat()

    report = {
        "run_started_at": run_started_at,
        "run_finished_at": run_finished_at,
        "provenance": {
            "seed": 42,
            "note": (
                "Pure modules (robustness, dq_impact, explainability) are byte-identical across machines and runs "
                "given this fixed seed. DB-backed modules (prioritization, copilot_eval, adversarial) run against "
                "a freshly-created, uniquely-named organization each run — their outcomes should be stable, but "
                "their raw ids/timestamps are not, by design."
            ),
        },
        "sections": {
            "anomaly_detection_robustness": robustness_result,
            "data_quality_impact": dq_impact_result,
            "explainability": explainability_result,
            **db_backed_results,
        },
    }

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(JSON_PATH, "w") as f:
        json.dump(report, f, indent=2, default=str)

    _write_markdown_summary(report, MARKDOWN_PATH)

    print(f"\nWrote {JSON_PATH}")
    print(f"Wrote {MARKDOWN_PATH}")

    if failures:
        print(f"\n=== {len(failures)} CHECK(S) DID NOT PASS ===")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\n=== All DB-backed checks passed; pure-module results written. ===")
    return 0


def _write_markdown_summary(report: dict, path: str) -> None:
    lines = [
        "# Evaluation run summary",
        "",
        f"- Started: {report['run_started_at']}",
        f"- Finished: {report['run_finished_at']}",
        f"- Seed: {report['provenance']['seed']}",
        "",
        "Full machine-readable results: `evaluation_results/latest_evaluation_run.json`. "
        "Regenerate with `python scripts/run_evaluation.py` from `backend/` — see docs/SCIENTIFIC_VALIDATION.md "
        "for how each section maps to a report claim.",
        "",
    ]
    for section_name, section in report["sections"].items():
        lines.append(f"## {section_name}")
        lines.append("")
        lines.append(f"- Evaluability: {section.get('evaluability', 'n/a')}")
        if section.get("note"):
            lines.append(f"- Note: {section['note']}")
        items = section.get("scenarios") or section.get("analyses") or section.get("checks") or section.get("cases") or []
        lines.append(f"- Items: {len(items)}")
        for item in items:
            name = item.get("scenario") or item.get("analysis") or item.get("check") or item.get("case") or "?"
            outcome = item.get("outcome")
            outcome_str = f" — outcome: {outcome}" if outcome is not None else ""
            lines.append(f"  - **{name}**{outcome_str}: {item.get('finding', '')}")
        lines.append("")

    with open(path, "w") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
