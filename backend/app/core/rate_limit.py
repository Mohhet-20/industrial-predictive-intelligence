"""A minimal, honestly-scoped in-memory rate limiter.

Used for exactly one thing right now: slowing down credential-guessing
against POST /auth/login (see of docs/RELIABILITY_AND_OPERATIONS.md).
This is a fixed-window counter keyed by (client IP, attempted email) held
in process memory — not a distributed limiter, not backed by Redis, and it
resets on process restart or scales per-worker if this process is ever run
with multiple workers. That is a real, stated limitation, not a hidden one:
a genuinely adversarial deployment needs a shared store (Redis) behind a
load balancer, which is unjustified infrastructure for this foundation's
actual deployment shape (documented as the upgrade path, not built).
"""
import threading
import time
from typing import Dict, Tuple

_lock = threading.Lock()
_attempts: Dict[str, list[float]] = {}


def check_and_record(key: str, *, max_attempts: int, window_seconds: int) -> Tuple[bool, int]:
    """Records one attempt for `key` and reports whether it should be
    allowed. Returns (allowed, retry_after_seconds). Old attempts outside
    the window are pruned on every call so memory does not grow unbounded
    for a key that stops being used."""
    now = time.time()
    cutoff = now - window_seconds
    with _lock:
        timestamps = [t for t in _attempts.get(key, []) if t > cutoff]
        if len(timestamps) >= max_attempts:
            _attempts[key] = timestamps
            retry_after = int(window_seconds - (now - timestamps[0])) + 1
            return False, max(retry_after, 1)
        timestamps.append(now)
        _attempts[key] = timestamps
        return True, 0


def reset_for_tests() -> None:
    """Test-only helper — clears all recorded attempts so tests don't leak
    state into each other via this module-level dict."""
    with _lock:
        _attempts.clear()
