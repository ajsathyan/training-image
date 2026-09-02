from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable


def initial_state(
    *,
    reservation_id: str,
    slot_generation: int,
    machine_id: str,
    boot_id: str,
    setup_revision: str,
    authority_epoch: int = 1,
) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "identity": {
            "reservationId": reservation_id,
            "slotGeneration": slot_generation,
            "machineId": machine_id,
            "bootId": boot_id,
            "setupRevision": setup_revision,
            "authorityEpoch": authority_epoch,
        },
        "setup": {
            "state": "pending",
            "readyAt": None,
        },
        "publicMapping": {
            "state": "unknown",
            "internalPort": 49200,
            "externalPort": None,
            "mappingGeneration": None,
            "observedAt": None,
        },
        "training": {
            "state": "not_started",
            "startedAt": None,
            "stoppedAt": None,
            "cancelledAt": None,
            "trainingRunId": None,
            "trainingPlanId": None,
            "configurationRevision": None,
            "serviceReachable": False,
            "serviceObservedAt": None,
        },
        "configuration": {
            "configurationId": None,
            "currentRevision": None,
            "settings": {},
            "appliedAt": None,
            "restartRequested": False,
        },
        "protection": {
            "joined": False,
            "joinedAt": None,
            "warm": False,
            "warmAt": None,
        },
        "process": {
            "tmuxAgora": None,
            "watchdog": None,
            "observedAt": None,
            "tmuxSessionId": None,
            "tmuxWindowId": None,
            "tmuxPaneId": None,
            "tmuxPanePid": None,
            "watchdogInstanceId": None,
            "retryContinuityToken": None,
        },
        "commands": {},
        "commandOrder": [],
        "lastAcknowledgementCursor": 0,
        "lastServerCommandCursor": 0,
        "lastAcceptedIssuedAt": None,
        "latestEvidence": None,
        "errors": {},
        "activeErrorId": None,
        "telemetryFailures": [],
        "telemetryOutbox": {},
    }


class MemoryStateStore:
    def __init__(self, state: dict[str, Any]):
        self._state = copy.deepcopy(state)
        self._lock = threading.RLock()

    def read(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    def transaction(self, reducer: Callable[[dict[str, Any]], Any]) -> Any:
        with self._lock:
            draft = copy.deepcopy(self._state)
            result = reducer(draft)
            self._state = draft
            return copy.deepcopy(result)


class JsonStateStore:
    def __init__(self, path: Path, state: dict[str, Any] | None = None):
        self.path = path
        self._lock = threading.RLock()
        with self._lock:
            if not self.path.exists():
                if state is None:
                    raise ValueError("initial state is required")
                self._write(state)

    def read(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(self.path.read_text(encoding="utf-8"))

    def transaction(self, reducer: Callable[[dict[str, Any]], Any]) -> Any:
        with self._lock:
            draft = self.read()
            result = reducer(draft)
            self._write(draft)
            return copy.deepcopy(result)

    def _write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
            text=True,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(state, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
