from __future__ import annotations

import queue
import threading
import time
from typing import Any

ACTIVE_STATES = {"launching", "installing", "joining", "retrying", "unhealthy"}
HISTORY_REPORT_INTERVAL_SECONDS = 60.0


class AsyncTelemetryDispatcher:
    def __init__(self, sink, on_error, on_success=None, *, capacity: int = 64):
        self.sink = sink
        self.on_error = on_error
        self.on_success = on_success
        self.queue = queue.Queue(maxsize=capacity)
        self._condition = threading.Condition()
        self._dropped = 0
        self._worker = threading.Thread(target=self._run, name="machine-sentinel-telemetry", daemon=True)
        self._worker.start()

    def submit(self, payload: dict[str, Any]) -> bool:
        try:
            self.queue.put_nowait(payload)
            return True
        except queue.Full:
            with self._condition:
                self._dropped += 1
            return False

    def flush(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self.queue.unfinished_tasks:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True

    def _run(self) -> None:
        while True:
            payload = self.queue.get()
            try:
                self.sink(payload)
                if self.on_success is not None:
                    self.on_success(payload)
            except Exception as error:
                self._report_error(payload, error)
            finally:
                with self._condition:
                    dropped = self._dropped
                    self._dropped = 0
                if dropped:
                    self._report_error(payload, RuntimeError(f"telemetry queue dropped {dropped} event(s)"))
                self.queue.task_done()
                with self._condition:
                    self._condition.notify_all()

    def _report_error(self, payload: dict[str, Any], error: Exception) -> None:
        try:
            self.on_error(payload, error)
        except Exception:
            pass


def heartbeat_interval_seconds(
    *,
    launch_active: bool | None = None,
    server_minimum: float | None = None,
    # Kept only for source compatibility with older local callers. These
    # values do not affect cadence.
    fleet_size: int | None = None,
    lifecycle_state: str | None = None,
) -> float:
    if launch_active is None:
        launch_active = lifecycle_state in ACTIVE_STATES
    return max(HISTORY_REPORT_INTERVAL_SECONDS, float(server_minimum or 0.0))


def history_report_interval_seconds(*, server_minimum: float | None = None) -> float:
    return max(HISTORY_REPORT_INTERVAL_SECONDS, float(server_minimum or 0.0))


def heartbeat_payload(state: dict[str, Any], *, seq: int, sent_at: str) -> dict[str, Any]:
    identity = state["identity"]
    return {
        "schemaVersion": 1,
        "machineId": identity["machineId"],
        "reservationId": identity["reservationId"],
        "slotGeneration": identity["slotGeneration"],
        "bootId": identity["bootId"],
        "setupRevision": identity["setupRevision"],
        "seq": seq,
        "sentAt": sent_at,
        "setup": dict(state["setup"]),
        "publicMapping": dict(state["publicMapping"]),
        "training": dict(state["training"]),
        "protection": dict(state["protection"]),
        "process": dict(state["process"]),
        "evidence": state.get("latestEvidence"),
        "errors": [dict(value) for value in state.get("errors", {}).values()],
        "commandCursor": state["lastAcknowledgementCursor"],
    }
