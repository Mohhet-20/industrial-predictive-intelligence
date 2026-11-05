"""Unit tests for app.feedback.matching — the temporal-integrity choke
point. No database involved; every function here is pure."""
import datetime as dt
import inspect

import pytest

from app.feedback.matching import (
    MAX_HORIZON_SECONDS,
    MIN_HORIZON_SECONDS,
    compute_horizon_window,
    evaluation_run_leakage_check,
    resolve_link_temporal_fields,
    validate_horizon,
)

BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


class TestValidateHorizon:
    def test_valid_horizon_has_no_errors(self):
        assert validate_horizon(BASE, 3600.0) == []

    def test_naive_timestamp_is_rejected(self):
        errors = validate_horizon(dt.datetime(2026, 1, 1), 3600.0)
        assert any("timezone-aware" in e for e in errors)

    def test_missing_prediction_timestamp_is_rejected(self):
        errors = validate_horizon(None, 3600.0)
        assert any("required" in e for e in errors)

    def test_missing_horizon_seconds_is_rejected(self):
        errors = validate_horizon(BASE, None)
        assert any("required" in e for e in errors)

    def test_horizon_below_minimum_is_rejected(self):
        errors = validate_horizon(BASE, 0.1)
        assert any("at least" in e for e in errors)

    def test_horizon_at_minimum_is_valid(self):
        assert validate_horizon(BASE, MIN_HORIZON_SECONDS) == []

    def test_horizon_above_maximum_is_rejected(self):
        errors = validate_horizon(BASE, MAX_HORIZON_SECONDS + 1)
        assert any("must not exceed" in e for e in errors)

    def test_horizon_at_maximum_is_valid(self):
        assert validate_horizon(BASE, MAX_HORIZON_SECONDS) == []


class TestComputeHorizonWindow:
    def test_window_spans_prediction_timestamp_to_horizon_end(self):
        window = compute_horizon_window(BASE, 7200.0)
        assert window.start == BASE
        assert window.end == BASE + dt.timedelta(seconds=7200)


class TestResolveLinkTemporalFields:
    def test_outcome_inside_window_is_within_horizon(self):
        result = resolve_link_temporal_fields(
            prediction_timestamp=BASE, horizon_seconds=86400.0, outcome_timestamp=BASE + dt.timedelta(hours=12)
        )
        assert result.errors == []
        assert result.within_horizon is True

    def test_outcome_exactly_at_window_boundary_is_within_horizon(self):
        result = resolve_link_temporal_fields(
            prediction_timestamp=BASE, horizon_seconds=3600.0, outcome_timestamp=BASE + dt.timedelta(seconds=3600)
        )
        assert result.within_horizon is True

    def test_outcome_after_window_is_not_within_horizon_but_not_an_error(self):
        result = resolve_link_temporal_fields(
            prediction_timestamp=BASE, horizon_seconds=3600.0, outcome_timestamp=BASE + dt.timedelta(hours=5)
        )
        assert result.errors == []
        assert result.within_horizon is False

    def test_outcome_before_prediction_timestamp_is_not_within_horizon(self):
        result = resolve_link_temporal_fields(
            prediction_timestamp=BASE, horizon_seconds=3600.0, outcome_timestamp=BASE - dt.timedelta(hours=1)
        )
        assert result.within_horizon is False

    def test_invalid_horizon_produces_errors_and_no_window(self):
        result = resolve_link_temporal_fields(prediction_timestamp=BASE, horizon_seconds=-1.0, outcome_timestamp=BASE)
        assert result.errors
        assert result.horizon_window is None
        assert result.within_horizon is False

    def test_naive_outcome_timestamp_is_rejected(self):
        result = resolve_link_temporal_fields(
            prediction_timestamp=BASE, horizon_seconds=3600.0, outcome_timestamp=dt.datetime(2026, 1, 1)
        )
        assert any("outcome_timestamp" in e for e in result.errors)

    def test_result_is_deterministic_regardless_of_when_computed(self):
        """Calling this twice, with no clock dependency, must give the
        exact same answer — this is the whole point of the function."""
        kwargs = dict(prediction_timestamp=BASE, horizon_seconds=3600.0, outcome_timestamp=BASE + dt.timedelta(minutes=30))
        first = resolve_link_temporal_fields(**kwargs)
        second = resolve_link_temporal_fields(**kwargs)
        assert first == second

    def test_linked_at_is_never_an_input(self):
        """Structural guarantee: this function cannot accept a `linked_at`
        or `now` parameter, because its signature doesn't have one."""
        params = inspect.signature(resolve_link_temporal_fields).parameters
        assert "linked_at" not in params
        assert "now" not in params


class TestEvaluationRunLeakageCheck:
    def test_no_leakage_when_outcome_and_verification_are_in_the_past(self):
        result = evaluation_run_leakage_check(
            evaluation_period_end=BASE + dt.timedelta(days=10),
            computed_at=BASE + dt.timedelta(days=11),
            outcome_verified_at=BASE + dt.timedelta(days=9),
            outcome_timestamp=BASE + dt.timedelta(days=8),
        )
        assert result is None

    def test_outcome_after_period_end_is_leakage(self):
        result = evaluation_run_leakage_check(
            evaluation_period_end=BASE + dt.timedelta(days=5),
            computed_at=BASE + dt.timedelta(days=10),
            outcome_verified_at=None,
            outcome_timestamp=BASE + dt.timedelta(days=6),
        )
        assert result is not None
        assert "leakage" in result

    def test_verification_after_computed_at_is_leakage(self):
        result = evaluation_run_leakage_check(
            evaluation_period_end=BASE + dt.timedelta(days=10),
            computed_at=BASE + dt.timedelta(days=5),
            outcome_verified_at=BASE + dt.timedelta(days=7),
            outcome_timestamp=BASE + dt.timedelta(days=1),
        )
        assert result is not None
        assert "leakage" in result

    def test_none_outcome_timestamp_and_verified_at_is_safe(self):
        result = evaluation_run_leakage_check(
            evaluation_period_end=BASE, computed_at=BASE, outcome_verified_at=None, outcome_timestamp=None
        )
        assert result is None

    def test_linked_at_is_never_an_input_to_leakage_check(self):
        params = inspect.signature(evaluation_run_leakage_check).parameters
        assert "linked_at" not in params
        assert "now" not in params

    # -----------------------------------------------------------------
    # outcome_reported_at — a backdated outcome_timestamp
    # inside the evaluation period is NOT by itself proof this platform knew
    # about the outcome in time; the report itself (created_at) must also
    # predate computed_at.
    # -----------------------------------------------------------------

    def test_no_leakage_when_report_time_is_also_before_computed_at(self):
        result = evaluation_run_leakage_check(
            evaluation_period_end=BASE + dt.timedelta(days=10),
            computed_at=BASE + dt.timedelta(days=11),
            outcome_verified_at=None,
            outcome_timestamp=BASE + dt.timedelta(days=3),
            outcome_reported_at=BASE + dt.timedelta(days=4),
        )
        assert result is None

    def test_backdated_outcome_timestamp_still_leaks_if_reported_after_computed_at(self):
        """A human reports, on day 12, that something happened on day 3 —
        outcome_timestamp (day 3) alone looks perfectly safe against a
        period ending day 10, but the REPORT didn't exist in this system
        until day 12, after computed_at (day 11). This is exactly the
        leakage shape outcome_timestamp alone cannot catch."""
        result = evaluation_run_leakage_check(
            evaluation_period_end=BASE + dt.timedelta(days=10),
            computed_at=BASE + dt.timedelta(days=11),
            outcome_verified_at=None,
            outcome_timestamp=BASE + dt.timedelta(days=3),
            outcome_reported_at=BASE + dt.timedelta(days=12),
        )
        assert result is not None
        assert "leakage" in result
        assert "outcome_reported_at" in result

    def test_outcome_reported_at_defaults_to_none_and_is_backward_compatible(self):
        """Existing callers that omit outcome_reported_at entirely (e.g.
        historical tests) are unaffected — the new check is simply skipped."""
        result = evaluation_run_leakage_check(
            evaluation_period_end=BASE + dt.timedelta(days=10),
            computed_at=BASE + dt.timedelta(days=11),
            outcome_verified_at=None,
            outcome_timestamp=BASE + dt.timedelta(days=3),
        )
        assert result is None

    def test_report_time_exactly_at_computed_at_is_not_leakage(self):
        result = evaluation_run_leakage_check(
            evaluation_period_end=BASE + dt.timedelta(days=10),
            computed_at=BASE + dt.timedelta(days=11),
            outcome_verified_at=None,
            outcome_timestamp=BASE + dt.timedelta(days=3),
            outcome_reported_at=BASE + dt.timedelta(days=11),
        )
        assert result is None
