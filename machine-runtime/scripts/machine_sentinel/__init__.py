"""Deterministic Agora machine setup, command, and evidence sentinel."""

from .client import (
    SentinelCredentialError,
    SentinelIngressClient,
    SentinelObservationError,
    execute_committed_sequence_recovery,
    execute_ingress_cycle,
    execute_ingress_payload,
    ingress_payload,
)
from .event_spool import EventSpool
from .evidence import classify_agora_evidence
from .sentinel import CommandRejected, MachineSentinel, exact_command_hash
from .state import JsonStateStore, MemoryStateStore, initial_state
from .telemetry import heartbeat_interval_seconds, history_report_interval_seconds

__all__ = [
    "CommandRejected",
    "EventSpool",
    "JsonStateStore",
    "MachineSentinel",
    "MemoryStateStore",
    "SentinelCredentialError",
    "classify_agora_evidence",
    "SentinelObservationError",
    "execute_committed_sequence_recovery",
    "execute_ingress_payload",
    "heartbeat_interval_seconds",
    "history_report_interval_seconds",
    "exact_command_hash",
    "initial_state",
]
