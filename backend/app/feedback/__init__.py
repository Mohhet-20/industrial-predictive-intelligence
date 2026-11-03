"""Closed-loop feedback domain logic (Phase 12).

This package holds the pure, deterministic logic that sits underneath
`app.services.feedback_service`. Each module is deliberately narrow and
import-light (no SQLAlchemy, no FastAPI) so it can be unit tested without a
database and so the temporal-integrity and classification rules are easy to
audit in isolation from persistence and HTTP concerns.

Sub-modules:

- `matching` — the temporal-integrity choke point. Computes horizon windows
  and `within_horizon` from frozen inputs only (`prediction_timestamp`,
  `horizon_seconds`, `outcome_timestamp`), never from "now" or `linked_at`,
  and provides the leakage check used before any evaluation run is accepted.
- `label_definitions` — the explicit, versioned, human-readable definitions
  of every verification status, signal assessment, and classification rule
  this platform uses. Exists so "what does VERIFIED mean" or "what counts as
  a false positive here" is a documented, API-exposed answer rather than
  something only readable in code (Phase 12 §18, label governance).
- `metrics` — classification of outcome-signal links into true
  positive/false positive/unresolved buckets, and precision-only metric
  computation gated on a minimum verified-link sample size. Deliberately
  never reports recall or F1 — this platform cannot enumerate unsignaled
  faults, so recall is not computable here without inventing a denominator.
- `error_analysis` — late-detection, duplicate-candidate, and
  false-negative-candidate detection. Everything here is framed as a
  *candidate* for human review, never an automatic root-cause conclusion
  (Phase 12 §9/§15).

Nothing in this package talks to synthetic or scenario ground truth — that
is `app.anomaly.evaluation` / `app.anomaly.synthetic_scenarios`, a completely
separate, parallel implementation. Real and synthetic ground truth must
never be computed by code that could accidentally blend them.
"""
