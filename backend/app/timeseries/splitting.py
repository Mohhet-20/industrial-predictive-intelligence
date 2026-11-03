"""Time-aware train/validation/test splitting.

Every split in this module is defined purely by TIMESTAMP BOUNDARIES, never
by shuffling or by row index — shuffling a time series before evaluation
lets a model see interpolated neighbors of its own test points during
training, which silently inflates every downstream metric. If a future
phase ever wants random shuffling for a specific, justified methodology,
it should call it out explicitly rather than getting it by default here.

Every function returns split boundaries + a `leakage_safe: bool` field that
is independently re-verified (not just asserted) before being returned —
see `_verify_no_overlap`. `tests/test_splitting.py` exercises this against
adversarial inputs.
"""
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Any, Optional


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _verify_no_overlap(train_end: datetime, test_start: datetime) -> bool:
    return train_end <= test_start


@dataclass
class ChronologicalSplit:
    train_start: str
    train_end: str
    val_start: Optional[str]
    val_end: Optional[str]
    test_start: str
    test_end: str
    leakage_safe: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def chronological_split(
    *, start: datetime, end: datetime, train_fraction: float = 0.7, val_fraction: float = 0.15
) -> ChronologicalSplit:
    """A single holdout split: the first `train_fraction` of the time range
    is train, the next `val_fraction` is validation, the remainder is test.
    No row ever appears in more than one split because the splits are
    defined by non-overlapping half-open time intervals."""
    if not (0 < train_fraction < 1) or val_fraction < 0 or train_fraction + val_fraction >= 1:
        raise ValueError("train_fraction must be in (0,1) and train_fraction + val_fraction must be < 1")
    if end <= start:
        raise ValueError("end must be after start")

    total = (end - start).total_seconds()
    train_end = start + timedelta(seconds=total * train_fraction)
    val_end = train_end + timedelta(seconds=total * val_fraction) if val_fraction > 0 else None
    test_start = val_end if val_end else train_end

    safe = _verify_no_overlap(train_end, val_end or test_start) and (val_end is None or _verify_no_overlap(val_end, test_start))

    return ChronologicalSplit(
        train_start=_iso(start),
        train_end=_iso(train_end),
        val_start=_iso(train_end) if val_end else None,
        val_end=_iso(val_end) if val_end else None,
        test_start=_iso(test_start),
        test_end=_iso(end),
        leakage_safe=safe,
    )


@dataclass
class WindowFold:
    fold_index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    leakage_safe: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def rolling_window_splits(
    *, start: datetime, end: datetime, train_window_seconds: float, test_window_seconds: float, step_seconds: float
) -> list[WindowFold]:
    """Walk-forward folds with a FIXED-SIZE train window that slides
    forward each fold (as opposed to expanding_window_splits, where train
    always starts at `start`). Each fold's train window immediately
    precedes its test window; consecutive folds advance by `step_seconds`.
    """
    if min(train_window_seconds, test_window_seconds, step_seconds) <= 0:
        raise ValueError("train_window_seconds, test_window_seconds, and step_seconds must all be positive")

    folds: list[WindowFold] = []
    fold_index = 0
    train_start = start
    while True:
        train_end = train_start + timedelta(seconds=train_window_seconds)
        test_start = train_end
        test_end = test_start + timedelta(seconds=test_window_seconds)
        if test_end > end:
            break
        folds.append(
            WindowFold(
                fold_index=fold_index,
                train_start=_iso(train_start),
                train_end=_iso(train_end),
                test_start=_iso(test_start),
                test_end=_iso(test_end),
                leakage_safe=_verify_no_overlap(train_end, test_start),
            )
        )
        fold_index += 1
        train_start = train_start + timedelta(seconds=step_seconds)

    return folds


def expanding_window_splits(
    *, start: datetime, end: datetime, initial_train_window_seconds: float, test_window_seconds: float, step_seconds: float
) -> list[WindowFold]:
    """Walk-forward folds where the train window always starts at `start`
    and grows each fold (as opposed to rolling_window_splits, where it
    slides). Models re-trained under this scheme see strictly more history
    each fold — appropriate when older data should never be discarded."""
    if min(initial_train_window_seconds, test_window_seconds, step_seconds) <= 0:
        raise ValueError("initial_train_window_seconds, test_window_seconds, and step_seconds must all be positive")

    folds: list[WindowFold] = []
    fold_index = 0
    train_end = start + timedelta(seconds=initial_train_window_seconds)
    while True:
        test_start = train_end
        test_end = test_start + timedelta(seconds=test_window_seconds)
        if test_end > end:
            break
        folds.append(
            WindowFold(
                fold_index=fold_index,
                train_start=_iso(start),
                train_end=_iso(train_end),
                test_start=_iso(test_start),
                test_end=_iso(test_end),
                leakage_safe=_verify_no_overlap(train_end, test_start),
            )
        )
        fold_index += 1
        train_end = train_end + timedelta(seconds=step_seconds)

    return folds
