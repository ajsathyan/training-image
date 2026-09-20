"""Portable interpretation of exact machine-owned Agora log evidence."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any, Mapping

INTERPRETATION_VERSION = "local-observation.v1"

_ISO_UTC_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})\b"
)
_SYSLOG_UTC_RE = re.compile(
    r"\b(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"(?P<day>\d{1,2})\s+(?P<clock>\d{2}:\d{2}:\d{2}(?:\.\d+)?)\b",
    re.I,
)
OWN_EXACT_NODE_RE = re.compile(
    r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\s+"
    r"\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+\[INFO\]\s+"
    r"Node name:\s*((?P<role>head|body|tail)-[0-9]+-[A-Za-z0-9._-]+-[0-9]+)\b"
)
_PROCESSED_BATCHES = re.compile(
    r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\s+"
    r"\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+\[INFO\]\s+"
    r"Processed (?P<count>\d+) batches in last (?P<window>\d+) seconds:$"
)
_AUTH_QUEUE_RE = re.compile(r"\bauthorization queue\b", re.I)
_ADMISSION_RE = re.compile(
    r"\b(?:access granted|authorization (?:granted|successful)|admitted)\b", re.I
)
_STATE_DOWNLOAD_RE = re.compile(r"\b(?:state download|downloaded in)\b", re.I)
_AVERAGED_RE = re.compile(r"\baveraged parameters with\s+\d+\s+peers\b", re.I)
_MAX_NODE_PATTERNS = (
    "maximum number of active nodes",
    "max active nodes",
    "maximum number of nodes connected",
    "max number of nodes connected",
    "too many nodes connected",
    "max nodes connected",
)
_FATAL_CATEGORIES = (
    (
        "authentication",
        ("invalid token", "unauthorized", "forbidden", "peer_id is already used"),
    ),
    ("storage", ("no space left",)),
    ("gpu_memory", ("cuda out of memory",)),
    ("process_exception", ("traceback",)),
    ("port_conflict", ("exit=98",)),
)


def source_utc(line: str, captured_at: str) -> str | None:
    """Resolve a log timestamp without treating capture time as event time."""

    iso_match = _ISO_UTC_RE.search(line)
    if iso_match:
        try:
            value = dt.datetime.fromisoformat(iso_match.group(0).replace("Z", "+00:00"))
        except ValueError:
            return None
        return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    syslog_match = _SYSLOG_UTC_RE.search(line)
    if not syslog_match:
        return None
    try:
        captured = dt.datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        captured = captured.astimezone(dt.timezone.utc)
        month = dt.datetime.strptime(syslog_match.group("month"), "%b").month
        clock = dt.time.fromisoformat(syslog_match.group("clock"))
        candidates = [
            dt.datetime(
                year,
                month,
                int(syslog_match.group("day")),
                clock.hour,
                clock.minute,
                clock.second,
                clock.microsecond,
                tzinfo=dt.timezone.utc,
            )
            for year in (captured.year - 1, captured.year, captured.year + 1)
        ]
    except ValueError:
        return None
    resolved = min(
        candidates, key=lambda value: abs((captured - value).total_seconds())
    )
    if resolved > captured + dt.timedelta(minutes=5):
        return None
    return resolved.isoformat().replace("+00:00", "Z")


def own_server_join_occurrence(
    evidence: str,
    captured_at: str,
    *,
    identity: Mapping[str, Any],
    machine_generation_id: Any,
    assignment_generation: Any,
    assignment_operation_id: Any,
    boot_id: Any,
    node_name: str,
) -> tuple[str, str] | None:
    """Identify one timestamped own-server marker across local collectors."""

    match = OWN_EXACT_NODE_RE.match(evidence.strip())
    if not match or match.group(1) != node_name:
        return None
    observed = source_utc(evidence, captured_at)
    if not observed:
        return None
    try:
        joined_at = dt.datetime.fromisoformat(observed.replace("Z", "+00:00"))
        joined_at = joined_at.astimezone(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
    except ValueError:
        return None
    marker = {
        "identity": dict(identity),
        "machineGenerationId": machine_generation_id,
        "assignmentGeneration": assignment_generation,
        "assignmentOperationId": assignment_operation_id,
        "bootId": boot_id,
        "nodeName": node_name,
        "joinedAt": joined_at,
    }
    material = json.dumps(
        marker, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(material.encode()).hexdigest()[:32], joined_at


def own_server_batch_summary(line: str) -> dict[str, Any] | None:
    """Recognize only the server's own completed processing-window summary."""

    match = _PROCESSED_BATCHES.fullmatch(line.strip())
    if not match:
        return None
    window = int(match.group("window"))
    if window <= 0:
        return None
    count = int(match.group("count"))
    return {"batchCount": count, "windowSeconds": window, "positive": count > 0}


def interpret_machine_log_line(line: str, *, captured_at: str) -> dict[str, Any]:
    """Interpret one complete line without inferring validation or payment."""

    text = str(line).strip("\r\n")
    lowered = text.lower()
    reported_at = source_utc(text, captured_at)
    node = OWN_EXACT_NODE_RE.match(text.strip())
    if node:
        return {
            "recognized": True,
            "kind": "join",
            "activity": "own_node_marker",
            "reportedAt": reported_at,
            "phase": "joining",
            "recovery": None,
            "attention": "normal_progress",
            "progress": "own_node_marker",
            "nodeName": node.group(1),
            "role": node.group("role").lower(),
        }
    summary = own_server_batch_summary(text)
    if summary is not None:
        positive = bool(summary["positive"])
        return {
            "recognized": True,
            "kind": "batch_summary",
            "activity": "batch_summary",
            "reportedAt": reported_at,
            "phase": "contributing" if positive else None,
            "recovery": None,
            "attention": "normal_progress" if positive else None,
            "progress": "positive_batch_report" if positive else "zero_batch_report",
            **summary,
        }
    if _AUTH_QUEUE_RE.search(text):
        return _simple("auth_queue", reported_at, phase="queued", attention="waiting")
    if _ADMISSION_RE.search(text):
        return _simple(
            "admission", reported_at, phase="joining", progress="access_granted"
        )
    if _STATE_DOWNLOAD_RE.search(text):
        return _simple(
            "state_download",
            reported_at,
            phase="syncing",
            progress="state_download",
        )
    if _AVERAGED_RE.search(text):
        return _simple(
            "training_progress",
            reported_at,
            phase="syncing",
            progress="parameters_averaged",
        )
    if any(pattern in lowered for pattern in _MAX_NODE_PATTERNS):
        return _fatal("capacity", "max_nodes_connected", reported_at)
    for category, patterns in _FATAL_CATEGORIES:
        if any(pattern in lowered for pattern in patterns):
            return _fatal(category, "fatal_error", reported_at)
    return {
        "recognized": False,
        "kind": "unknown",
        "activity": "unknown",
        "reportedAt": reported_at,
        "phase": None,
        "recovery": None,
        "attention": None,
        "progress": None,
    }


def _simple(
    activity: str,
    reported_at: str | None,
    *,
    phase: str,
    attention: str = "normal_progress",
    progress: str | None = None,
) -> dict[str, Any]:
    return {
        "recognized": True,
        "kind": activity,
        "activity": activity,
        "reportedAt": reported_at,
        "phase": phase,
        "recovery": None,
        "attention": attention,
        "progress": progress,
    }


def _fatal(category: str, activity: str, reported_at: str | None) -> dict[str, Any]:
    return {
        "recognized": True,
        "kind": "fatal_error",
        "activity": activity,
        "reportedAt": reported_at,
        "phase": None,
        "recovery": "needed",
        "attention": "needs_action",
        "progress": None,
        "errorCategory": category,
    }
