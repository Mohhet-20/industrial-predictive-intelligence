"""Scientific evaluation & validation framework (Phase 10).

Every module here MEASURES the actual behavior of real, already-built
Project 5 code (app.anomaly.*, app.condition.*, app.services.*,
app.copilot.*) under conditions chosen to be scientifically informative —
it does not reimplement or approximate that code, and it does not invent
results. Where a component cannot be evaluated honestly (no ground truth,
no real-world data), that is recorded explicitly rather than filled with
a fabricated number — see docs/SCIENTIFIC_VALIDATION.md for the full
report and app.evaluation.report for how these modules' outputs are
aggregated.

Every function in this package is deterministic given a fixed seed
(default 42, matching app.anomaly.isolation_forest_detector.RANDOM_STATE)
so a run of scripts/run_evaluation.py reproduces byte-identical numeric
results across machines and across time.
"""
