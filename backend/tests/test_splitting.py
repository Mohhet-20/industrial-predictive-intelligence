from datetime import datetime, timedelta, timezone

import pytest

from app.timeseries.splitting import chronological_split, expanding_window_splits, rolling_window_splits

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_chronological_split_produces_non_overlapping_ordered_bounds():
    split = chronological_split(start=BASE, end=BASE + timedelta(days=10), train_fraction=0.7, val_fraction=0.15)
    assert split.train_start == BASE.isoformat()
    assert split.train_end <= split.val_start
    assert split.val_end <= split.test_start
    assert split.test_end == (BASE + timedelta(days=10)).isoformat()
    assert split.leakage_safe is True


def test_chronological_split_without_validation_set():
    split = chronological_split(start=BASE, end=BASE + timedelta(days=10), train_fraction=0.8, val_fraction=0.0)
    assert split.val_start is None and split.val_end is None
    assert split.train_end == split.test_start
    assert split.leakage_safe is True


def test_chronological_split_rejects_invalid_fractions():
    with pytest.raises(ValueError):
        chronological_split(start=BASE, end=BASE + timedelta(days=10), train_fraction=0.9, val_fraction=0.2)  # sums >= 1
    with pytest.raises(ValueError):
        chronological_split(start=BASE, end=BASE + timedelta(days=10), train_fraction=0, val_fraction=0.1)
    with pytest.raises(ValueError):
        chronological_split(start=BASE + timedelta(days=1), end=BASE, train_fraction=0.7, val_fraction=0.1)  # end before start


def test_rolling_window_splits_train_size_stays_fixed_across_folds():
    folds = rolling_window_splits(
        start=BASE, end=BASE + timedelta(days=10), train_window_seconds=86400 * 3, test_window_seconds=86400, step_seconds=86400
    )
    assert len(folds) > 1
    train_durations = {
        (datetime.fromisoformat(f.train_end) - datetime.fromisoformat(f.train_start)).total_seconds() for f in folds
    }
    assert train_durations == {86400 * 3}  # every fold's train window is exactly the same fixed size
    for f in folds:
        assert f.leakage_safe is True
        assert f.train_end <= f.test_start


def test_rolling_window_train_start_advances_each_fold():
    folds = rolling_window_splits(
        start=BASE, end=BASE + timedelta(days=10), train_window_seconds=86400 * 3, test_window_seconds=86400, step_seconds=86400
    )
    starts = [f.train_start for f in folds]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts)  # every fold slides forward, none repeat


def test_expanding_window_splits_train_start_never_moves():
    folds = expanding_window_splits(
        start=BASE, end=BASE + timedelta(days=10), initial_train_window_seconds=86400 * 3, test_window_seconds=86400, step_seconds=86400
    )
    assert len(folds) > 1
    assert all(f.train_start == BASE.isoformat() for f in folds)
    # train_end grows monotonically — expanding, not sliding
    ends = [datetime.fromisoformat(f.train_end) for f in folds]
    assert ends == sorted(ends)
    assert len(set(ends)) == len(ends)
    for f in folds:
        assert f.leakage_safe is True


def test_window_splits_reject_non_positive_parameters():
    with pytest.raises(ValueError):
        rolling_window_splits(start=BASE, end=BASE + timedelta(days=10), train_window_seconds=0, test_window_seconds=86400, step_seconds=86400)
    with pytest.raises(ValueError):
        expanding_window_splits(start=BASE, end=BASE + timedelta(days=10), initial_train_window_seconds=86400, test_window_seconds=-1, step_seconds=86400)


def test_window_too_large_for_range_produces_no_folds_not_an_error():
    folds = rolling_window_splits(start=BASE, end=BASE + timedelta(hours=1), train_window_seconds=86400 * 30, test_window_seconds=86400, step_seconds=86400)
    assert folds == []


def test_splits_are_reproducible_for_identical_input():
    a = rolling_window_splits(start=BASE, end=BASE + timedelta(days=5), train_window_seconds=86400, test_window_seconds=86400, step_seconds=86400)
    b = rolling_window_splits(start=BASE, end=BASE + timedelta(days=5), train_window_seconds=86400, test_window_seconds=86400, step_seconds=86400)
    assert [f.to_dict() for f in a] == [f.to_dict() for f in b]
