"""In-process, real-measurement metrics collection.

No fabricated traffic, no invented latency numbers. This module records
what actually happened in this process — request counts/status/latency —
using plain in-memory counters guarded by a lock (FastAPI sync `def` routes
run on a worker threadpool, so this genuinely needs thread-safety, unlike
most of this codebase's async-only state).

Explicit scope, documented rather than oversold (see §12 of
docs/RELIABILITY_AND_OPERATIONS.md): per-process, in-memory, reset on
restart, not persisted, not aggregated across multiple processes/replicas.
That is an honest description of what an in-process counter can be, not a
placeholder for something bigger — a real metrics backend (Prometheus,
StatsD) is the documented upgrade path once there is more than one process
to aggregate across.
"""
import threading
import time
from collections import deque
from typing import Any, Deque, Dict

_lock = threading.Lock()

# Bounded ring buffer of recent request samples, used to compute a real
# (not fabricated) latency summary without unbounded memory growth.
_MAX_SAMPLES = 2000
_request_samples: Deque[tuple[str, int, float]] = deque(maxlen=_MAX_SAMPLES)  # (path, status_code, duration_ms)
_request_count_by_status_class: Dict[str, int] = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0}
_total_requests = 0
_process_started_at = time.time()


def record_request(path: str, status_code: int, duration_ms: float) -> None:
    status_class = f"{status_code // 100}xx"
    with _lock:
        global _total_requests
        _total_requests += 1
        _request_count_by_status_class[status_class] = _request_count_by_status_class.get(status_class, 0) + 1
        _request_samples.append((path, status_code, duration_ms))


def get_request_metrics() -> Dict[str, Any]:
    with _lock:
        samples = list(_request_samples)
        total = _total_requests
        by_class = dict(_request_count_by_status_class)

    durations = sorted(d for _, _, d in samples)
    n = len(durations)

    def _percentile(p: float) -> float | None:
        if n == 0:
            return None
        idx = min(n - 1, int(round(p * (n - 1))))
        return round(durations[idx], 2)

    return {
        "total_requests_recorded": total,
        "requests_by_status_class": by_class,
        "recent_sample_size": n,
        "recent_sample_capacity": _MAX_SAMPLES,
        "latency_ms": {
            "avg": round(sum(durations) / n, 2) if n else None,
            "p50": _percentile(0.50),
            "p95": _percentile(0.95),
            "max": round(durations[-1], 2) if n else None,
        },
        "note": (
            "In-process counters only: per-worker, in-memory, reset on restart. "
            "Not aggregated across multiple processes. recent_* figures are computed "
            "over the last N requests this process actually served, not a synthetic window."
        ),
    }


def get_process_uptime_seconds() -> float:
    return round(time.time() - _process_started_at, 1)
