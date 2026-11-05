"""The temporal-integrity choke point for outcome-signal linking.

Every function in this module is a pure function of its explicit arguments.
None of them accept "now", `linked_at`, or any other call-time clock value —
that is the whole point. `within_horizon` and the leakage check must produce
the exact same answer regardless of when they are computed, because they are
re-derivable facts about frozen historical timestamps, not live judgments.

`prediction_timestamp` and `horizon_seconds` are frozen once, at link-creation
time, by the caller (see `app.services.feedback_service.link_outcome_to_signal`)
and stored on `OutcomeSignalLink`. They are never re-derived later. This
module only ever computes from values the caller already froze and passes in
explicitly.

Three distinct clocks matter here, and conflating any two of them is exactly
what this module exists to prevent:

  - `outcome_timestamp` — when the real-world event itself occurred, as the
    reporting human states it. This can be backdated (someone reports today
    that a failure happened three days ago).
  - `outcome_reported_at` — when that report became known to the platform,
    i.e. `app.models.outcome.Outcome.created_at` at the moment
    `app.services.feedback_service.report_outcome` persisted it. This is
    never backdated — it is a DB-assigned insert time.
  - `outcome_verified_at` — when a human later confirmed or rejected the
    report (`app.models.outcome.OutcomeVerification.created_at`), a third,
    separate, always-later act.

`evaluation_run_leakage_check` checks all three against an evaluation run's
own `evaluation_period_end` / `computed_at`, because a leak through any one
of them is still a leak.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

MIN_HORIZON_SECONDS = 1.0
MAX_HORIZON_SECONDS = 365.0 * 24.0 * 3600.0  # one year — an explicit, named bound, never "whatever the caller sends"


@dataclasses.dataclass(frozen=True)
class HorizonWindow:
    """The closed interval [start, end] during which an outcome is
    considered to fall within a signal's stated prediction horizon."""

    start: dt.datetime
    end: dt.datetime


def validate_horizon(prediction_timestamp: dt.datetime, horizon_seconds: float) -> list[str]:
    """Returns a list of human-readable validation errors (empty = valid).

    Deliberately returns a list rather than raising, so callers (the API
    layer) can surface every problem at once instead of one at a time.
    """
    errors: list[str] = []
    if prediction_timestamp is None:
        errors.append("prediction_timestamp is required")
    elif prediction_timestamp.tzinfo is None:
        errors.append("prediction_timestamp must be timezone-aware")
    if horizon_seconds is None:
        errors.append("horizon_seconds is required")
        return errors
    if horizon_seconds < MIN_HORIZON_SECONDS:
        errors.append(f"horizon_seconds must be at least {MIN_HORIZON_SECONDS} second(s) (got {horizon_seconds})")
    if horizon_seconds > MAX_HORIZON_SECONDS:
        errors.append(
            f"horizon_seconds must not exceed {MAX_HORIZON_SECONDS} seconds (~365 days); an arbitrarily long "
            f"horizon is not permitted — the platform never silently defaults to an unbounded window (got {horizon_seconds})"
        )
    return errors


def compute_horizon_window(prediction_timestamp: dt.datetime, horizon_seconds: float) -> HorizonWindow:
    """Pure arithmetic — assumes `validate_horizon` has already passed."""
    return HorizonWindow(
        start=prediction_timestamp,
        end=prediction_timestamp + dt.timedelta(seconds=horizon_seconds),
    )


@dataclasses.dataclass(frozen=True)
class LinkTemporalResult:
    """The result of resolving a candidate outcome-signal link's temporal
    fields. `errors` non-empty means the link must be rejected by the
    caller before anything is persisted."""

    within_horizon: bool
    horizon_window: HorizonWindow | None
    errors: list[str]


def resolve_link_temporal_fields(
    *,
    prediction_timestamp: dt.datetime,
    horizon_seconds: float,
    outcome_timestamp: dt.datetime,
) -> LinkTemporalResult:
    """Computes whether `outcome_timestamp` falls within the horizon window
    implied by `prediction_timestamp` + `horizon_seconds`.

    No `now`, no `linked_at` — see module docstring. This function's result
    is fully determined by its three arguments and nothing else, which is
    what makes it safe to call at link-creation time and trust forever
    after, regardless of when the link itself was created or re-inspected.
    """
    errors = validate_horizon(prediction_timestamp, horizon_seconds)
    if outcome_timestamp is None:
        errors.append("outcome_timestamp is required")
    elif outcome_timestamp.tzinfo is None:
        errors.append("outcome_timestamp must be timezone-aware")
    if errors:
        return LinkTemporalResult(within_horizon=False, horizon_window=None, errors=errors)

    window = compute_horizon_window(prediction_timestamp, horizon_seconds)
    within = window.start <= outcome_timestamp <= window.end
    return LinkTemporalResult(within_horizon=within, horizon_window=window, errors=[])


def evaluation_run_leakage_check(
    *,
    evaluation_period_end: dt.datetime,
    computed_at: dt.datetime,
    outcome_verified_at: dt.datetime | None,
    outcome_timestamp: dt.datetime | None,
    outcome_reported_at: dt.datetime | None = None,
) -> str | None:
    """Returns a human-readable error string if including this
    outcome/verification in an evaluation run scoped to
    `evaluation_period_end` would constitute backward information leakage.
    Returns None if the inclusion is safe.

    Three leakage shapes are checked, all explicit, named integrity
    failures rather than a vague "something's off" warning. The first two
    are about the real-world event itself; the third is about when the
    PLATFORM learned of it — a distinct clock ("time an
    event occurred" vs. "time it became known to the platform" must be
    distinguishable, because a human can truthfully report, today, that
    something happened last week — the event's own timestamp alone cannot
    tell you when that fact entered this system):

    1. The outcome itself happened after the evaluation period ends — using
       it would let future information grade a past-scoped evaluation.
    2. The outcome was verified after this evaluation run's own
       `computed_at` — meaning, at the instant this run claims to have
       computed its result, that verification did not yet exist to use.
    3. The outcome was REPORTED (became known to the platform —
       app.models.outcome.Outcome.created_at, set the moment
       app.services.feedback_service.report_outcome persists it) after this
       evaluation run's `computed_at`. An outcome can be backdated by its
       own `outcome_timestamp` to well within the evaluation period while
       still not having existed in this system yet at compute time — check
       (1) alone cannot catch that, because it only looks at when the real
       event happened, not when this platform found out about it.
       `outcome_reported_at` is optional (callers that genuinely have no
       report-time to pass, e.g. existing tests, are unaffected) but every
       live caller should supply it.
    """
    if outcome_timestamp is not None and outcome_timestamp > evaluation_period_end:
        return (
            "leakage: outcome_timestamp "
            f"({outcome_timestamp.isoformat()}) is after evaluation_period_end "
            f"({evaluation_period_end.isoformat()}) — this outcome had not yet occurred "
            "as of the period this evaluation run claims to cover"
        )
    if outcome_verified_at is not None and outcome_verified_at > computed_at:
        return (
            "leakage: outcome_verified_at "
            f"({outcome_verified_at.isoformat()}) is after this evaluation run's computed_at "
            f"({computed_at.isoformat()}) — the verification used did not exist yet at compute time"
        )
    if outcome_reported_at is not None and outcome_reported_at > computed_at:
        return (
            "leakage: outcome_reported_at "
            f"({outcome_reported_at.isoformat()}) is after this evaluation run's computed_at "
            f"({computed_at.isoformat()}) — this outcome had not yet been reported to the platform "
            "(regardless of its own outcome_timestamp) as of this run's compute time"
        )
    return None
