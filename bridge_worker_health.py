"""Secret-free, thread-safe health state for long-running Bridge workers."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkerHealthRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, dict] = {}

    def register(self, worker_id: str, *, stale_after_seconds: int) -> None:
        worker_id = str(worker_id or "").strip()
        if not worker_id:
            raise ValueError("worker_id_required")
        with self._lock:
            self._records.setdefault(
                worker_id,
                {
                    "id": worker_id,
                    "started": False,
                    "in_progress": False,
                    "last_started_at": "",
                    "last_success_at": "",
                    "last_failure_at": "",
                    "consecutive_failures": 0,
                    "last_duration_ms": None,
                    "last_error_type": "",
                    "stale_after_seconds": max(10, int(stale_after_seconds)),
                    "_cycle_started": 0.0,
                },
            )

    def begin(self, worker_id: str) -> None:
        with self._lock:
            record = self._records[worker_id]
            record["started"] = True
            record["in_progress"] = True
            record["last_started_at"] = utc_now()
            record["_cycle_started"] = time.monotonic()

    def success(self, worker_id: str) -> None:
        with self._lock:
            record = self._records[worker_id]
            started = float(record.get("_cycle_started") or time.monotonic())
            record["in_progress"] = False
            record["last_success_at"] = utc_now()
            record["consecutive_failures"] = 0
            record["last_error_type"] = ""
            record["last_duration_ms"] = round((time.monotonic() - started) * 1000, 3)

    def failure(self, worker_id: str, error: BaseException) -> None:
        with self._lock:
            record = self._records[worker_id]
            started = float(record.get("_cycle_started") or time.monotonic())
            record["in_progress"] = False
            record["last_failure_at"] = utc_now()
            record["consecutive_failures"] = int(record["consecutive_failures"]) + 1
            record["last_error_type"] = type(error).__name__[:80]
            record["last_duration_ms"] = round((time.monotonic() - started) * 1000, 3)

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {
                worker_id: {
                    key: value
                    for key, value in record.items()
                    if not key.startswith("_")
                }
                for worker_id, record in self._records.items()
            }


__all__ = ["WorkerHealthRegistry"]
