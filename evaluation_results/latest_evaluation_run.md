# Evaluation run summary

- Started: 2026-11-03T15:42:51.397414+00:00
- Finished: 2026-11-03T15:42:56.029685+00:00
- Seed: 42

Full machine-readable results: `evaluation_results/latest_evaluation_run.json`. Regenerate with `python scripts/run_evaluation.py` from `backend/` — see docs/SCIENTIFIC_VALIDATION.md for how each section maps to a report claim.

## anomaly_detection_robustness

- Evaluability: FULLY EVALUABLE WITH AVAILABLE DATA
- Note: All scenarios run against real app.anomaly.* code with deterministic synthetic inputs — no DB required.
- Items: 8
  - **missing_data_gap**: The detector does not crash on a gap and produces no fabricated candidate spanning the missing observations — it simply has no points to score there. Detection quality during a gap is bounded by data-quality checks (app.services.quality_service's data_gap issue type), not by the anomaly detector, which is the correct division of responsibility.
  - **noise_sensitivity_sweep**: Spike recalled at noise std <= 4.0; beyond that the injected signal is no longer a statistical outlier relative to reference variability, so it is correctly not flagged rather than incorrectly flagged. This demonstrates the method's actual sensitivity boundary rather than assuming it.
  - **outlier_robustness_reference**: With 3/200 reference points contaminated by extreme outliers, the robust median stayed within 0.045 of the true center (50.0), while a naive mean would have been off by 6.712 — 60.3x wider spread than the robust estimate. This is a measured demonstration of the documented rationale for median/MAD, not an assumption.
  - **sensor_drift**: This platform's anomaly detectors use a STATIC reference-period baseline (by design — see docs/ANOMALY_DETECTION.md §2) and are not drift-adaptive. Slow, continuous drift is not yet flagged at this drift rate/duration rather than being tracked as a moving 'new normal.' This is a genuine, stated limitation: an operator relying on this detector for a sensor with known continuous drift (e.g. recalibration-due sensors) should expect eventual flags that reflect drift, not necessarily a fault, and should re-baseline periodically. It is recorded here rather than hidden.
  - **short_history_boundary**: The boundary is exact: n=29 raises InsufficientReferenceDataError (an honest, typed error the API surfaces as a real 4xx, not a silently wrong baseline), n=30 and above computes normally. No off-by-one gap between the documented minimum and actual behavior.
  - **sparse_observations**: Baseline statistics are identical regardless of time-density because compute_baseline is purely value-based (it has no concept of elapsed time once observations are selected). This means a sparse-but-30-day reference window is treated exactly as confidently as a dense 2.5-hour one — a real, stated limitation: MIN_FOR_BASELINE guards against too FEW points, not against a reference period that is representative in TIME (e.g. missing a season). Operators choosing reference windows should ensure they span representative operating conditions, not just an observation count; this is not enforced in code.
  - **boundary_threshold**: The documented `>=` (inclusive) semantics hold exactly: a value at precisely the threshold z-score is flagged, one just below is not.
  - **isolation_forest_short_reference** — outcome: honest_error: Isolation Forest scoring refuses to fit on an insufficient reference feature matrix with a typed, honest error rather than fitting on a handful of rows and returning an overconfident model.

## data_quality_impact

- Evaluability: SYNTHETICALLY EVALUABLE
- Note: Uses the real detector-running code with synthetic, seeded gap/anomaly patterns — no real sensor failure data exists to measure this against (see docs/SYNTHETIC_DATA.md).
- Items: 3
  - **gap_adjacent_to_anomaly_impact**: A gap immediately following an anomaly does not remove the anomaly's own onset point (the spike itself is still present — only the RECOVERY period is missing), so recall is unchanged in this scenario. This validates that detection quality here depends on whether the anomalous point itself survives, not merely on whether SOME nearby data is missing — a distinction worth stating explicitly rather than assuming gaps always degrade detection.
  - **pure_gap_false_positive_check**: robust_zscore raised 2 false-positive candidate(s) on a pure data gap with default configuration. isolation_forest could not be evaluated on this scenario: Only 23 of 24 reference grid points had every feature computed (need >= 30). Widen the reference period, shrink the feature windows, or reduce the number of required features..
  - **gap_threshold_boundary_sweep**: The gap threshold is a strict '>' (exclusive), replicated exactly from app.services.quality_service.scan_gaps_for_sensor: a gap exactly equal to 900s is NOT flagged, one second longer is.

## explainability

- Evaluability: FULLY EVALUABLE WITH AVAILABLE DATA
- Note: Each check runs real detector code on synthetic data and independently recomputes the reported explanation numbers from raw inputs via a separate code path, rather than only reviewing the explanation-generation code itself.
- Items: 4
  - **robust_zscore_explanation_traceability** — outcome: traceable: The stored explanation's baseline_median, baseline_robust_std_estimate, and z_score all match an independent from-scratch numpy recomputation from the raw reference/scoring data to within floating-point tolerance.
  - **isolation_forest_explanation_traceability** — outcome: traceable: All 3 reported contributing_features (mean/reference_mean/z_score) matched an independent recomputation from a separate build_feature_matrix call, and the top-3 ranking was correctly sorted by |z-score| descending.
  - **explanation_stability_scenario** — outcome: stable: Running the same detector on identical data twice produced byte-identical explanation dicts.
  - **heuristic_explanation_honesty_check** — outcome: honest: No isolation_forest explanation text in this scenario used causal-attribution language ('caused by', 'root cause', etc.) beyond the deviation-ranking it actually computes.

## prioritization

- Evaluability: FULLY EVALUABLE WITH AVAILABLE DATA
- Note: Runs the real orchestration_service.get_prioritized_signals against real ORM-inserted evidence rows in a test database.
- Items: 6
  - **reproducibility_check** — outcome: reproducible: Two calls to get_prioritized_signals against unchanged DB state returned byte-identical results (ordering, scores, and every contributing factor).
  - **strong_anomaly_poor_data_quality** — outcome: context_only_confirmed: A high_priority anomaly with 3 nearby critical data-quality issues scored exactly 300 (severity_rank only) — poor nearby data quality is surfaced for a human to see, but does NOT change this item's rank. This is a real, worth-stating limitation: a poorly-instrumented sensor's anomaly is not deprioritized OR reweighted for the reader, who must apply that judgment themselves.
  - **multiple_weak_signals** — outcome: strong_signal_still_ranks_higher: 5 OBSERVED_TREND signals on one sensor each scored 140 (stage_rank=1 x 100 + persistence_count=4 x 10 = 140) versus a single VALIDATED_DEGRADATION signal scoring 300 (stage_rank=3 x 100 = 300). The single strong (validated) signal still outranked the repeated weak ones in this scenario.
  - **conflicting_detectors** — outcome: agreement_bonus_applied_correctly: Two different detector types flagging the same sensor both received the +25 cross_detector_agreement bonus, correctly reflecting independent-method agreement — a real, inspectable factor, not a hidden score.
  - **no_available_signals** — outcome: empty_result_handled_safely: With zero open anomalies/degradation signals, get_prioritized_signals returned an empty items list with honest zero counts and an intact methodology description — no crash, no placeholder item.
  - **stale_evidence** — outcome: age_not_scored_confirmed: An anomaly still open after 400 days scored identically (200) to an equivalent anomaly opened 1 hour ago — this ranking has NO staleness/age factor once an item is still 'open' status. This is a genuine, worth-documenting limitation: an old, possibly-forgotten open item is not surfaced any differently than a brand-new one by score alone (period_start only breaks exact ties, and does so toward MORE RECENT items, meaning stale evidence can rank even further down on ties).

## copilot_groundedness

- Evaluability: FULLY EVALUABLE WITH AVAILABLE DATA (mechanism-level; a real LLM's own judgment is NOT evaluated — no API key is configured in this environment, see docs/COPILOT.md)
- Note: Each case exercises the real orchestrator/service/API path. Where a specific model response was needed to trigger a path, a FakeProvider supplied controlled text — this evaluates the platform's retrieval/grounding/safeguard machinery, not a real model's judgment.
- Items: 9
  - **correct_evidence_retrieval** — outcome: PASS: An asset-context question retrieved real evidence rooted in the actual asset row (not empty, not fabricated).
  - **correct_source_attribution** — outcome: PASS: The cited ref [E1] in the answer resolves to the real evidence item it claims to (the asset row actually queried), and the answer was not flagged.
  - **unsupported_question_handling** — outcome: PASS: An out-of-domain question with no provider configured produced the honest 'not configured, here is the evidence' degradation rather than any fabricated answer.
  - **insufficient_evidence_handling** — outcome: PASS: A brand-new asset with no ingested data/anomalies produced no fabricated model_output (anomaly) evidence, and its data-quality summary honestly reported zero issues rather than being omitted or invented.
  - **anomaly_vs_failure_distinction** — outcome: PASS: A properly-hedged anomaly answer passed unflagged while a failure-certainty answer about the identical evidence was correctly flagged — the safeguard distinguishes anomaly-language from failure-language.
  - **synthetic_labeling** — outcome: PASS: A real is_synthetic_scenario=True anomaly's copilot evidence text explicitly labels it as a synthetic evaluation scenario, independent of organization naming.
  - **authz_boundary** — outcome: PASS: A user in a different organization could not retrieve another organization's asset as copilot evidence (404, not leaked data).
  - **provider_unavailable_behavior** — outcome: PASS: With no LLM provider configured, the copilot returned the real retrieved evidence (including the actual asset code) as plain text rather than an error or a fabricated answer.
  - **adversarial_question** — outcome: PASS: An adversarial question paired with a worst-case fabricated-probability response was still caught and flagged by the architectural safeguard (not left to the model's own judgment), and the flagged text was preserved rather than silently rewritten.

## adversarial_and_failure_cases

- Evaluability: FULLY EVALUABLE WITH AVAILABLE DATA
- Note: Covers the 9 named adversarial scenarios: 4 are new checks below, 1 (synthetic-presented-as-real) is covered by app.evaluation.copilot_eval.case_synthetic_labeling, 2 (unsupported failure confirmation) by app.evaluation.copilot_eval's adversarial/anomaly-vs-failure cases, 1 (insufficient history) by app.evaluation.robustness's boundary sweeps, and 1 (gap overlapping anomaly) by app.evaluation.dq_impact + tests/test_evaluation_dq_impact.py's DB-backed propagation test — see docs/SCIENTIFIC_VALIDATION.md for the unified 9-scenario table.
- Items: 5
  - **incompatible_model_artifact** — outcome: safe_failure: A persisted detector artifact with a schema-incompatible baseline_summary (simulating drift/corruption) did not crash the server or fabricate a successful result: the detection run was created, then honestly marked FAILED with a recorded error_message, and the API returned a generic 500. The raw error message stored is a low-level Python TypeError string rather than a curated diagnostic — a real, minor limitation worth noting, distinct from the safe-failure property itself which held.
  - **malformed_timestamp_is_quarantined_per_record** — outcome: safe_and_documented: A genuinely unparseable timestamp string at index 3 of a 6-record batch was quarantined as one indexed RecordError; the other 5 well-formed records were still accepted (200, not a whole-batch 422) — this platform's deliberate list[dict]-not-list[Model] request schema (see app.schemas.ingestion.IngestionRequest's own docstring) holds even for a completely unparseable value, not just semantically-invalid-but-parseable ones.
  - **extreme_value_scoring_does_not_crash** — outcome: safe: A physically-implausible extreme value (1e12) in the scoring window was flagged as an anomaly with a finite (non-NaN, non-Inf) raw_score/normalized_score — no numeric overflow reached the stored evidence.
  - **annotation_vs_model_conflict_preserves_both** — outcome: provenance_preserved: A human review disagreeing with the detector (false_positive + explanatory note) changed only the anomaly's status; every original detection field (raw_score, threshold, severity_tier, explanation) stayed byte-identical, and the human's disagreement is preserved as its own separate, queryable AnomalyInvestigation record — the model's original output and the human's interpretation coexist as distinct evidence rather than one overwriting the other.
  - **disagreeing_detectors_no_forced_consensus** — outcome: disagreement_preserved: robust_zscore (sensitive default threshold) flagged the injected spike while isolation_forest (deliberately configured with a far less sensitive threshold) did not — both detectors' runs completed and are independently queryable with their own real output; nothing in the platform forces a merged or averaged verdict between disagreeing detectors, and orchestration_service's cross_detector_agreement factor correctly reflects only AGREEMENT, never manufacturing false consensus when detectors disagree.
