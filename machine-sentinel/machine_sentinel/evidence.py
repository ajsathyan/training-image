from __future__ import annotations

import re
from typing import Any

RULESET_VERSION = 1
MAX_RAW_EVIDENCE_CHARS = 4_000
SECRET_PATTERNS = (
    re.compile(r"\bhf_[A-Za-z0-9_]+\b"),
    re.compile(r"\brp_[A-Za-z0-9_]+\b"),
    re.compile(r"(?i)\b(authorization|api[_ -]?key|token|secret)\s*[:=]\s*\S+"),
)
JOIN_PATTERNS = (
    re.compile(r"\bnode\s+name\s*:\s*(tail|body|head)(?:[-_\s]?\d+)?", re.IGNORECASE),
    re.compile(r"\brole\s*[:=]?\s*agora-(tail|body|head)\b", re.IGNORECASE),
    re.compile(r"\b(tail|body|head)[-_\s]?\d*\s+accumulated\s+\d+\s+samples?\b", re.IGNORECASE),
    re.compile(r"\b(joined|admitted|accepted|assigned|registered|selected)\b.*\b(tail|body|head)\b", re.IGNORECASE),
)
MAX_NODE_PATTERNS = (
    "maximum number of active nodes",
    "max active nodes",
    "maximum number of nodes connected",
    "max number of nodes connected",
    "too many nodes connected",
    "max nodes connected",
)
FATAL_CATEGORIES = (
    ("authentication", ("invalid token", "unauthorized", "forbidden", "peer_id is already used")),
    ("storage", ("no space left",)),
    ("gpu_memory", ("cuda out of memory",)),
    ("process_exception", ("traceback",)),
    ("port_conflict", ("exit=98",)),
)


def redact(text: str) -> str:
    redacted = str(text)
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def bounded_evidence(line: str) -> str:
    # Preserve the operator-visible wording.  Only secrets and the outer line
    # ending are removed; internal spacing and punctuation are evidence.
    clean = redact(str(line)).strip("\r\n")
    return clean[-MAX_RAW_EVIDENCE_CHARS:]


def _join_from_line(line: str) -> dict[str, str] | None:
    for index, pattern in enumerate(JOIN_PATTERNS):
        match = pattern.search(line)
        if not match:
            continue
        role_group = 2 if index == 3 else 1
        return {"role": match.group(role_group).lower(), "evidence": bounded_evidence(line)}
    return None


def classify_agora_evidence(text: str) -> dict[str, Any]:
    redacted = redact(text)
    lines = [line.strip() for line in redacted.splitlines() if line.strip()]
    for line in reversed(lines):
        joined = _join_from_line(line)
        if joined:
            return {
                "rulesetVersion": RULESET_VERSION,
                "joinState": "joined",
                "activity": "joined",
                "role": joined["role"],
                "errorCategory": None,
                "rawError": None,
                "evidence": joined["evidence"],
            }
        lowered = line.lower()
        if "authorization queue" in lowered:
            return _activity("joining", "auth_queue", line)
        if "state download" in lowered or "downloaded in" in lowered:
            return _activity("joining", "state_download", line)
        if re.search(r"\baveraged parameters with\s+\d+\s+peers\b", lowered):
            return _activity("joining", "training_progress", line)
        if any(pattern in lowered for pattern in MAX_NODE_PATTERNS):
            return _error("capacity", "max_nodes_connected", line)
        for category, patterns in FATAL_CATEGORIES:
            if any(pattern in lowered for pattern in patterns):
                return _error(category, "fatal_error", line)
    return {
        "rulesetVersion": RULESET_VERSION,
        "joinState": "unknown",
        "activity": "unknown",
        "role": None,
        "errorCategory": None,
        "rawError": None,
        "evidence": bounded_evidence(lines[-1]) if lines else None,
    }


def _activity(join_state: str, activity: str, line: str) -> dict[str, Any]:
    return {
        "rulesetVersion": RULESET_VERSION,
        "joinState": join_state,
        "activity": activity,
        "role": None,
        "errorCategory": None,
        "rawError": None,
        "evidence": bounded_evidence(line),
    }


def _error(category: str, activity: str, line: str) -> dict[str, Any]:
    raw_error = bounded_evidence(line)
    return {
        "rulesetVersion": RULESET_VERSION,
        "joinState": "failed",
        "activity": activity,
        "role": None,
        "errorCategory": category,
        "rawError": raw_error,
        "evidence": raw_error,
    }
