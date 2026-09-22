from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

EVENT_SCHEMA = "agora.machine-sentinel-event.v1"
CURSOR_SCHEMA = "agora.machine-sentinel-event-cursor.v1"
MAX_EVENT_BYTES = 16 * 1024


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > 9_007_199_254_740_991:
            raise ValueError("Machine Sentinel canonical integer is outside the safe range")
        return value
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ValueError("Machine Sentinel canonical string contains an unpaired surrogate")
        return value
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Machine Sentinel canonical object keys must be strings")
        return {
            key: _canonical_value(value[key])
            for key in sorted(value, key=lambda item: item.encode("utf-16-be"))
        }
    raise ValueError("Machine Sentinel canonical value has an unsupported type")


def _canonical(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_canonical(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class EventSpool:
    """Append-only machine event history with an exact acknowledged prefix."""

    def __init__(self, path: Path, cursor_path: Path | None = None):
        self.path = path
        self.cursor_path = cursor_path or path.with_suffix(path.suffix + ".cursor.json")
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self._lock = threading.RLock()
        self._event_cache: list[dict[str, Any]] | None = None
        self._source_event_index: dict[str, dict[str, Any]] | None = None

    @contextmanager
    def _file_lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _reset_cache(self) -> None:
        self._event_cache = None
        self._source_event_index = None

    def _events(self) -> list[dict[str, Any]]:
        if self._event_cache is not None:
            return self._event_cache
        if not self.path.exists():
            self._event_cache = []
            self._source_event_index = {}
            return self._event_cache
        raw = self.path.read_bytes()
        lines = raw.splitlines()
        complete_without_newline = bool(raw and not raw.endswith(b"\n"))
        events: list[dict[str, Any]] = []
        for line_number, encoded_line in enumerate(lines, start=1):
            final_incomplete = line_number == len(lines) and not raw.endswith(b"\n")
            try:
                line = encoded_line.decode("utf-8")
            except UnicodeDecodeError as error:
                if final_incomplete:
                    self._truncate_incomplete_tail(len(raw) - len(encoded_line))
                    break
                raise RuntimeError(
                    f"Machine Sentinel event spool is corrupt at line {line_number}"
                ) from error
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                if final_incomplete:
                    self._truncate_incomplete_tail(len(raw) - len(encoded_line))
                    break
                raise RuntimeError(
                    f"Machine Sentinel event spool is corrupt at line {line_number}"
                ) from error
            if (
                not isinstance(event, dict)
                or event.get("schemaVersion") != EVENT_SCHEMA
                or not isinstance(event.get("sequence"), int)
                or event["sequence"] != len(events) + 1
                or not isinstance(event.get("eventId"), str)
            ):
                raise RuntimeError("Machine Sentinel event spool has an invalid sequence")
            material = dict(event)
            event_id = material.pop("eventId")
            if hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest() != event_id:
                raise RuntimeError("Machine Sentinel event spool has an invalid content hash")
            events.append(event)
        if complete_without_newline and len(events) == len(lines):
            # A successful write may reach stable storage before its trailing
            # newline.  The JSON record is complete history; normalize the
            # delimiter before the next append instead of deleting it or
            # concatenating the next object onto the same line.
            descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND)
            try:
                os.write(descriptor, b"\n")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        self._event_cache = events
        self._source_event_index = {
            event["sourceEventId"]: event
            for event in events
            if isinstance(event.get("sourceEventId"), str)
        }
        return self._event_cache

    def _truncate_incomplete_tail(self, length: int) -> None:
        with self.path.open("r+b") as handle:
            handle.truncate(length)
            handle.flush()
            os.fsync(handle.fileno())

    def _cursor(self) -> dict[str, Any]:
        if not self.cursor_path.exists():
            return {
                "schemaVersion": CURSOR_SCHEMA,
                "acknowledgedSequence": 0,
                "acknowledgedEventId": None,
                "remoteCursor": None,
            }
        value = json.loads(self.cursor_path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("schemaVersion") != CURSOR_SCHEMA
            or not isinstance(value.get("acknowledgedSequence"), int)
            or value["acknowledgedSequence"] < 0
            or value.get("remoteCursor") is not None
            and not isinstance(value.get("remoteCursor"), str)
        ):
            raise RuntimeError("Machine Sentinel event cursor is invalid")
        acknowledged = value["acknowledgedSequence"]
        if acknowledged:
            events = self._events()
            if (
                acknowledged > len(events)
                or value.get("acknowledgedEventId")
                != events[acknowledged - 1]["eventId"]
            ):
                raise RuntimeError("Machine Sentinel event cursor does not match the spool")
        return value

    def append(
        self,
        *,
        event_type: str,
        occurred_at: str,
        identity: dict[str, Any],
        training_run_id: str | None,
        training_session_id: str | None = None,
        evidence: dict[str, Any] | None = None,
        interpretation_version: str = "machine-sentinel.v1",
        source_event_id: str | None = None,
    ) -> dict[str, Any]:
        if not event_type or not occurred_at:
            raise ValueError("Machine Sentinel event type and occurrence time are required")
        with self._lock, self._file_lock():
            self._reset_cache()
            events = self._events()
            if source_event_id is not None:
                existing = (self._source_event_index or {}).get(source_event_id)
                if existing is not None:
                    return copy.deepcopy(existing)
            sequence = len(events) + 1
            material = {
                "schemaVersion": EVENT_SCHEMA,
                "sequence": sequence,
                "eventType": event_type,
                "occurredAt": occurred_at,
                "trainingRunId": training_run_id,
                "trainingSessionId": training_session_id,
                "identity": {
                    key: identity.get(key)
                    for key in (
                        "fleetId",
                        "authorityEpoch",
                        "launchId",
                        "reservationId",
                        "slotId",
                        "slotGeneration",
                        "machineGenerationId",
                        "machineId",
                        "provider",
                        "accountScope",
                        "providerResourceId",
                        "bootId",
                        "setupRevision",
                    )
                },
                "interpretationVersion": interpretation_version,
                "evidence": evidence or {},
                "sourceEventId": source_event_id,
            }
            event_id = hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()
            event = {**material, "eventId": event_id}
            encoded = (_canonical(event) + "\n").encode("utf-8")
            if len(encoded) > MAX_EVENT_BYTES:
                raise ValueError("Machine Sentinel event exceeds its bounded size")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            existed = self.path.exists()
            descriptor = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
            )
            try:
                os.fchmod(descriptor, 0o600)
                remaining = memoryview(encoded)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise OSError("Machine Sentinel event spool append made no progress")
                    remaining = remaining[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if not existed:
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            events.append(event)
            if source_event_id is not None:
                assert self._source_event_index is not None
                self._source_event_index[source_event_id] = event
            return copy.deepcopy(event)

    def pending(self, *, limit: int = 100) -> dict[str, Any]:
        with self._lock, self._file_lock():
            self._reset_cache()
            cursor = self._cursor()
            acknowledged = cursor["acknowledgedSequence"]
            events = self._events()
            if acknowledged > len(events):
                raise RuntimeError("Machine Sentinel event cursor exceeds the spool")
            bounded = max(1, min(int(limit), 100))
            return {
                "afterCursor": cursor.get("remoteCursor"),
                "events": copy.deepcopy(events[acknowledged : acknowledged + bounded]),
                "hasMore": len(events) > acknowledged + bounded,
            }

    def acknowledge(
        self,
        event_ids: Iterable[str],
        *,
        remote_cursor: str,
    ) -> dict[str, Any]:
        expected = list(event_ids)
        if not isinstance(remote_cursor, str) or not remote_cursor:
            raise ValueError("Machine Sentinel remote history cursor is invalid")
        with self._lock, self._file_lock():
            self._reset_cache()
            cursor = self._cursor()
            events = self._events()
            start = cursor["acknowledgedSequence"]
            actual = [event["eventId"] for event in events[start : start + len(expected)]]
            if actual != expected:
                positions = {
                    event["eventId"]: index + 1 for index, event in enumerate(events)
                }
                if (
                    expected
                    and all(event_id in positions for event_id in expected)
                    and [positions[event_id] for event_id in expected]
                    == list(range(positions[expected[0]], positions[expected[0]] + len(expected)))
                    and positions[expected[-1]] <= start
                    and cursor.get("remoteCursor") == remote_cursor
                ):
                    return cursor
                raise RuntimeError("Machine Sentinel event acknowledgement is not the exact spool prefix")
            if not expected:
                return cursor
            acknowledged = start + len(expected)
            value = {
                "schemaVersion": CURSOR_SCHEMA,
                "acknowledgedSequence": acknowledged,
                "acknowledgedEventId": expected[-1],
                "remoteCursor": remote_cursor,
            }
            _atomic_json(self.cursor_path, value)
            return value

    def export_page(
        self,
        *,
        expected_identity: dict[str, Any],
        max_events: int = 100,
        max_bytes: int = 256 * 1024,
    ) -> dict[str, Any]:
        """Return one validated, immutable pending prefix without acknowledging it."""

        required = (
            "fleetId",
            "authorityEpoch",
            "launchId",
            "reservationId",
            "slotId",
            "slotGeneration",
            "machineGenerationId",
            "machineId",
            "provider",
            "accountScope",
            "providerResourceId",
            "bootId",
            "setupRevision",
        )
        if any(expected_identity.get(key) in (None, "") for key in required):
            raise ValueError("Machine Sentinel export identity is incomplete")
        event_limit = max(1, min(int(max_events), 1000))
        byte_limit = max(1024, min(int(max_bytes), 4 * 1024 * 1024))
        with self._lock, self._file_lock():
            self._reset_cache()
            cursor = self._cursor()
            events = self._events()
            start = cursor["acknowledgedSequence"]
            selected: list[dict[str, Any]] = []
            encoded_bytes = 0
            for event in events[start : start + event_limit]:
                identity = event.get("identity")
                if not isinstance(identity, dict) or any(
                    identity.get(key) != expected_identity.get(key) for key in required
                ):
                    raise RuntimeError(
                        "Machine Sentinel event lifecycle identity does not match the selected source"
                    )
                encoded = (_canonical(event) + "\n").encode("utf-8")
                if selected and encoded_bytes + len(encoded) > byte_limit:
                    break
                if len(encoded) > byte_limit:
                    raise RuntimeError("Machine Sentinel event exceeds the export byte limit")
                selected.append(copy.deepcopy(event))
                encoded_bytes += len(encoded)
            stat = self.path.stat() if self.path.exists() else None
            end = start + len(selected)
            prefix_ids = [event["eventId"] for event in events[:end]]
            prefix_digest = hashlib.sha256(
                _canonical(prefix_ids).encode("utf-8")
            ).hexdigest()
            return {
                "schemaVersion": "agora.machine-sentinel-spool-page.v1",
                "identity": {key: expected_identity[key] for key in required},
                "startSequence": start + 1,
                "endSequence": end,
                "eventIds": [event["eventId"] for event in selected],
                "events": selected,
                "encodedBytes": encoded_bytes,
                "hasMore": len(events) > end,
                "prefixDigest": prefix_digest,
                "spoolIdentity": {
                    "device": int(stat.st_dev) if stat is not None else None,
                    "inode": int(stat.st_ino) if stat is not None else None,
                },
            }

    def acknowledge_page(self, page: dict[str, Any], *, remote_cursor: str) -> dict[str, Any]:
        """Acknowledge exactly the exported prefix, rejecting spool replacement or drift."""

        if not isinstance(page, dict) or page.get("schemaVersion") != "agora.machine-sentinel-spool-page.v1":
            raise ValueError("Machine Sentinel spool page is invalid")
        event_ids = page.get("eventIds")
        if not isinstance(event_ids, list) or any(not isinstance(value, str) for value in event_ids):
            raise ValueError("Machine Sentinel spool page event IDs are invalid")
        with self._lock, self._file_lock():
            self._reset_cache()
            stat = self.path.stat() if self.path.exists() else None
            actual_spool_identity = {
                "device": int(stat.st_dev) if stat is not None else None,
                "inode": int(stat.st_ino) if stat is not None else None,
            }
            if page.get("spoolIdentity") != actual_spool_identity:
                raise RuntimeError("Machine Sentinel spool was replaced before acknowledgement")
            events = self._events()
            end = page.get("endSequence")
            if not isinstance(end, int) or isinstance(end, bool) or end < 0 or end > len(events):
                raise RuntimeError("Machine Sentinel spool was truncated before acknowledgement")
            prefix_digest = hashlib.sha256(
                _canonical([event["eventId"] for event in events[:end]]).encode("utf-8")
            ).hexdigest()
            if prefix_digest != page.get("prefixDigest"):
                raise RuntimeError("Machine Sentinel spool prefix changed before acknowledgement")
            cursor = self._cursor()
            start = cursor["acknowledgedSequence"]
            actual = [
                event["eventId"] for event in events[start : start + len(event_ids)]
            ]
            if actual != event_ids:
                if (
                    event_ids
                    and end <= start
                    and cursor.get("remoteCursor") == remote_cursor
                ):
                    return cursor
                raise RuntimeError(
                    "Machine Sentinel event acknowledgement is not the exact spool prefix"
                )
            if not event_ids:
                return cursor
            value = {
                "schemaVersion": CURSOR_SCHEMA,
                "acknowledgedSequence": start + len(event_ids),
                "acknowledgedEventId": event_ids[-1],
                "remoteCursor": remote_cursor,
            }
            _atomic_json(self.cursor_path, value)
            return value
