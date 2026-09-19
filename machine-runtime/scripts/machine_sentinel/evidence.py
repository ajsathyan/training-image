from __future__ import annotations

import re
from typing import Any

from .observation_contract import INTERPRETATION_VERSION, interpret_machine_log_line

RULESET_VERSION = 1
MAX_RAW_EVIDENCE_CHARS = 4_000
SECRET_PATTERNS = (
    re.compile(r"\bhf_[A-Za-z0-9_]+\b"),
    re.compile(r"\brp_[A-Za-z0-9_]+\b"),
    re.compile(r"(?i)\b(authorization|api[_ -]?key|token|secret)\s*[:=]\s*\S+"),
)
LEGACY_JOIN_RE = re.compile(
    r"\bnode\s+name\s*:\s*((?P<role>tail|body|head)(?:[-_\s]?[A-Za-z0-9._-]+)?)",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    redacted = str(text)
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def bounded_evidence(line: str) -> str:
    clean = redact(str(line)).strip("\r\n")
    return clean[-MAX_RAW_EVIDENCE_CHARS:]


def _bounded_redacted(line: str) -> str:
    return line.strip("\r\n")[-MAX_RAW_EVIDENCE_CHARS:]


def classify_agora_evidence(
    text: str, *, captured_at: str = "1970-01-01T00:00:00Z"
) -> dict[str, Any]:
    """Classify the newest meaningful complete line using the shared contract."""

    lines = [line.strip() for line in redact(text).splitlines() if line.strip()]
    newest_interpretation = None
    for index, line in enumerate(reversed(lines)):
        interpreted = interpret_machine_log_line(line, captured_at=captured_at)
        if index == 0:
            newest_interpretation = interpreted
        if interpreted["recognized"]:
            return _compatibility_evidence(interpreted, line)
        legacy = LEGACY_JOIN_RE.search(line)
        if legacy:
            return {
                **_base(line),
                "recognized": True,
                "kind": "legacy_join",
                "activity": "joined",
                "reportedAt": None,
                "phase": "joining",
                "recovery": None,
                "attention": "normal_progress",
                "progress": "node_marker",
                "joinState": "joined",
                "role": legacy.group("role").lower(),
                "nodeName": legacy.group(1),
                "errorCategory": None,
                "rawError": None,
            }
    line = lines[-1] if lines else ""
    interpreted = newest_interpretation or interpret_machine_log_line(
        line, captured_at=captured_at
    )
    return {
        **_base(line),
        **interpreted,
        "joinState": "unknown",
        "errorCategory": None,
        "rawError": None,
    }


def _base(line: str) -> dict[str, Any]:
    return {
        "rulesetVersion": RULESET_VERSION,
        "evidence": _bounded_redacted(line) if line else None,
        "role": None,
        "nodeName": None,
        "batchCount": None,
        "windowSeconds": None,
        "positive": None,
    }


def _compatibility_evidence(interpreted: dict[str, Any], line: str) -> dict[str, Any]:
    kind = interpreted["kind"]
    join_state = (
        "joined"
        if kind in {"join", "batch_summary"}
        else "failed"
        if kind == "fatal_error"
        else "joining"
    )
    raw_error = _bounded_redacted(line) if kind == "fatal_error" else None
    return {
        **_base(line),
        **interpreted,
        "joinState": join_state,
        "errorCategory": interpreted.get("errorCategory"),
        "rawError": raw_error,
    }


def canonical_observation(
    evidence: dict[str, Any],
    *,
    captured_at: str,
    source: dict[str, Any],
    identity: dict[str, Any],
    training_run_id: str | None,
    training_session_id: str | None = None,
    join_session: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map shared interpretation into the canonical passive observation model."""

    kind = evidence.get("kind")
    associated_join = join_session if isinstance(join_session, dict) else None
    phase = evidence.get("phase")
    attention = evidence.get("attention")
    progress = evidence.get("progress")
    meaning = None
    if kind == "batch_summary" and not associated_join:
        phase = None
        attention = None
        meaning = (
            "unattributed_positive_batch"
            if evidence.get("positive")
            else "unattributed_zero_batch"
        )
    elif kind == "batch_summary":
        meaning = (
            "positive_processed_batches_window_not_validation_or_reward"
            if evidence.get("positive")
            else "zero_processed_batches_window"
        )
    elif evidence.get("activity") == "training_progress":
        meaning = "progress_not_validation_or_payment"

    return {
        "interpretationVersion": INTERPRETATION_VERSION,
        "provider": identity.get("provider"),
        "accountScope": identity.get("accountScope"),
        "resourceId": identity.get("providerResourceId"),
        "machineId": identity.get("machineId"),
        "machineGenerationId": identity.get("machineGenerationId"),
        "assignmentGeneration": identity.get("slotGeneration"),
        "assignmentOperationId": identity.get("assignmentOperationId"),
        "runId": training_run_id,
        "joinId": associated_join.get("joinId") if associated_join else None,
        "kind": (
            "own_log_join"
            if kind == "join"
            else "own_log_batch_summary"
            if kind == "batch_summary"
            else "machine_sentinel_log"
        ),
        "source": "machine_sentinel",
        "sourceDetail": source.get("name"),
        "reportedAt": evidence.get("reportedAt"),
        "capturedAt": captured_at,
        "phase": phase,
        "recovery": evidence.get("recovery"),
        "freshness": "fresh",
        "attention": attention,
        "progress": progress,
        "evidence": {
            "activity": evidence.get("activity"),
            "kind": kind,
            "joinState": evidence.get("joinState"),
            "role": evidence.get("role"),
            "nodeName": evidence.get("nodeName"),
            "batchCount": evidence.get("batchCount"),
            "windowSeconds": evidence.get("windowSeconds"),
            "positive": evidence.get("positive"),
            "errorCategory": evidence.get("errorCategory"),
            "raw": evidence.get("evidence"),
            "meaning": meaning,
            "trainingSessionId": training_session_id,
            "joinSession": associated_join,
            "joinMarkerAt": (
                associated_join.get("joinedAt")
                if kind == "join" and associated_join
                else None
            ),
            "source": {
                key: source.get(key)
                for key in (
                    "name",
                    "device",
                    "inode",
                    "rotationGeneration",
                    "byteStart",
                    "byteEnd",
                )
            },
            "timeBasis": "source" if evidence.get("reportedAt") else "capture",
        },
    }
