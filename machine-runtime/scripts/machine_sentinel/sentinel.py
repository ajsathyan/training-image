from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
import threading
from typing import Any, Callable

from .evidence import canonical_observation, classify_agora_evidence
from .observation_contract import own_server_join_occurrence
from .telemetry import AsyncTelemetryDispatcher, heartbeat_payload

ALLOWED_ACTIONS = {
    "prepare_setup",
    "start_training",
    "stop_training",
    "cancel_training",
    "repair_heartbeat",
    "apply_configuration",
}
POST_START_ACTIONS = {"stop_training", "repair_heartbeat", "apply_configuration"}
COMMAND_FIELDS = {
    "commandId", "action", "scope", "authorityEpoch", "issuedAt", "expiresAt", "arguments",
}
SCOPE_FIELDS = {"reservationId", "slotGeneration", "machineId", "bootId", "setupRevision"}
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
REASON_CODES = {"operator_requested", "maintenance", "superseded"}


class CommandRejected(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(dt.timezone.utc)


def _fatal_replacement_order(
    reported_at: Any,
    replacement: Any,
    *,
    training_run_id: Any,
    training_session_id: Any,
) -> str:
    """Order source-timestamped fatal evidence around an observed replacement."""

    if not isinstance(reported_at, str) or not isinstance(replacement, dict):
        return "unknown"
    if (
        replacement.get("trainingRunId") != training_run_id
        or replacement.get("trainingSessionId") != training_session_id
    ):
        return "unknown"
    lower_bound = replacement.get("previousIdentityVerifiedAt")
    upper_bound = replacement.get("observedAt")
    if not isinstance(lower_bound, str) or not isinstance(upper_bound, str):
        return "unknown"
    try:
        fatal_time = _parse_time(reported_at)
        lower_time = _parse_time(lower_bound)
        upper_time = _parse_time(upper_bound)
    except (TypeError, ValueError):
        return "unknown"
    if lower_time > upper_time:
        return "unknown"
    if fatal_time <= lower_time:
        return "before"
    if fatal_time > upper_time:
        return "after"
    return "unknown"


def _replacement_lower_bound_covers(
    occurrence_known_at: str | None, replacement: dict[str, Any]
) -> bool:
    if occurrence_known_at is None:
        return True
    lower_bound = replacement.get("previousIdentityVerifiedAt")
    if not isinstance(lower_bound, str):
        return False
    try:
        return _parse_time(occurrence_known_at) <= _parse_time(lower_bound)
    except (TypeError, ValueError):
        return False


def _fatal_replacement_correlation(
    reported_at: Any,
    captured_at: str,
    replacement: Any,
    *,
    error_group_id: str | None = None,
    training_run_id: Any,
    training_session_id: Any,
    failure_boot_id: Any = None,
    occurrence_id: str | None = None,
    occurrence_source: dict[str, Any] | None = None,
    occurrence_known_at: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(replacement, dict) or (
        replacement.get("trainingRunId") != training_run_id
        or replacement.get("trainingSessionId") != training_session_id
    ):
        return None
    boot_orders_replacement = (
        failure_boot_id is not None
        and replacement.get("previousBootId") == failure_boot_id
        and replacement.get("bootId") != failure_boot_id
    )
    if boot_orders_replacement:
        basis = "boot_transition"
    elif (
        _fatal_replacement_order(
            reported_at,
            replacement,
            training_run_id=training_run_id,
            training_session_id=training_session_id,
        )
        == "before"
        and _replacement_lower_bound_covers(occurrence_known_at, replacement)
    ):
        basis = "source_interval"
    else:
        return None
    return {
        "kind": "identity_replacement",
        "ordering": "before",
        "basis": basis,
        "errorGroupId": error_group_id,
        "errorReportedAt": reported_at,
        "errorCapturedAt": captured_at,
        "previousIdentityVerifiedAt": replacement.get("previousIdentityVerifiedAt"),
        "replacementObservedAt": replacement.get("observedAt"),
        "trainingRunId": replacement.get("trainingRunId"),
        "trainingSessionId": replacement.get("trainingSessionId"),
        "failureBootId": failure_boot_id,
        "replacementBootId": replacement.get("bootId"),
        "occurrenceId": occurrence_id,
        "occurrenceSource": copy.deepcopy(occurrence_source),
        "occurrenceKnownAt": occurrence_known_at,
    }


def _fatal_occurrence(
    source: Any,
    *,
    error_group_id: str,
    training_run_id: Any,
) -> tuple[str | None, dict[str, Any] | None]:
    """Identify one exact generated log record without using observation time."""

    if not isinstance(source, dict):
        return None, None
    name = source.get("name")
    rotation = source.get("rotationGeneration")
    byte_start = source.get("byteStart")
    byte_end = source.get("byteEnd")
    if (
        not isinstance(name, str)
        or not name
        or not isinstance(rotation, int)
        or isinstance(rotation, bool)
        or rotation < 0
        or not isinstance(byte_start, int)
        or isinstance(byte_start, bool)
        or byte_start < 0
        or not isinstance(byte_end, int)
        or isinstance(byte_end, bool)
        or byte_end <= byte_start
    ):
        return None, None
    exact_source = {
        "name": name,
        "rotationGeneration": rotation,
        "byteStart": byte_start,
        "byteEnd": byte_end,
    }
    device = source.get("device")
    inode = source.get("inode")
    if (
        isinstance(device, int)
        and not isinstance(device, bool)
        and device >= 0
        and isinstance(inode, int)
        and not isinstance(inode, bool)
        and inode >= 0
    ):
        exact_source.update({"device": device, "inode": inode})
    material = {
        "errorGroupId": error_group_id,
        "source": exact_source,
        "trainingRunId": training_run_id,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:32], exact_source


def _intent_hash(command: dict[str, Any]) -> str:
    material = {
        "action": command["action"],
        "scope": command["scope"],
        "arguments": command.get("arguments", {}),
        "authorityEpoch": command["authorityEpoch"],
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def exact_command_hash(command: dict[str, Any]) -> str:
    material = copy.deepcopy(command)
    material.pop("intentHash", None)
    material.pop("commandHash", None)
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise CommandRejected("command_invalid", f"Command {field} must be an identifier")
    return value


def _integer(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise CommandRejected("command_invalid", f"Command {field} is outside its allowed range")
    return value


def _exact_arguments(action: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CommandRejected("command_invalid", "Command arguments must be an object")
    required = {
        "prepare_setup": {"setupProfileId", "setupRevision"},
        "start_training": {"trainingRunId", "trainingPlanId", "configurationRevision", "announcePort"},
        "stop_training": {"trainingRunId", "graceSeconds", "reasonCode", "approvalReceiptId"},
        "cancel_training": {"trainingRunId", "reasonCode", "approvalReceiptId"},
        "repair_heartbeat": {"trainingRunId", "watchdogRevision"},
        "apply_configuration": {"configurationId", "fromRevision", "toRevision", "approvalReceiptId", "settings"},
    }[action]
    if set(value) != required:
        raise CommandRejected("command_invalid", f"Command {action} arguments do not match the exact schema")
    normalized = copy.deepcopy(value)
    for field in required - {"announcePort", "graceSeconds", "settings"}:
        normalized[field] = _identifier(normalized[field], field)
    if "announcePort" in required:
        normalized["announcePort"] = _integer(normalized["announcePort"], "announcePort", minimum=1, maximum=65535)
    if "graceSeconds" in required:
        normalized["graceSeconds"] = _integer(normalized["graceSeconds"], "graceSeconds", minimum=0, maximum=600)
    if "reasonCode" in required and normalized["reasonCode"] not in REASON_CODES:
        raise CommandRejected("command_invalid", "Command reasonCode is not allowlisted")
    if action == "apply_configuration":
        settings = normalized["settings"]
        if not isinstance(settings, dict) or not settings or not set(settings) <= {"announcePort", "nodeType"}:
            raise CommandRejected("command_invalid", "Configuration settings must contain only announcePort or nodeType")
        if "announcePort" in settings:
            settings["announcePort"] = _integer(settings["announcePort"], "settings.announcePort", minimum=1, maximum=65535)
        if "nodeType" in settings and settings["nodeType"] not in {"head", "body", "tail"}:
            raise CommandRejected("command_invalid", "Configuration nodeType is invalid")
    return normalized


class MachineSentinel:
    def __init__(
        self,
        store: Any,
        *,
        effects: dict[str, Callable[[dict[str, Any]], Any]],
        telemetry_sink: Callable[[dict[str, Any]], Any] | None = None,
        event_sink: Callable[[dict[str, Any]], Any] | None = None,
    ):
        self.store = store
        self.effects = effects
        self._execution_lock = threading.RLock()
        self.telemetry_sink = telemetry_sink
        self.event_sink = event_sink
        self.telemetry_dispatcher = (
            AsyncTelemetryDispatcher(
                telemetry_sink,
                self._record_telemetry_failure,
                self._record_telemetry_success,
            )
            if telemetry_sink is not None
            else None
        )

    def snapshot(self) -> dict[str, Any]:
        return self.store.read()

    def flush_telemetry(self, timeout: float | None = None) -> bool:
        if self.telemetry_dispatcher is None:
            return True
        return self.telemetry_dispatcher.flush(timeout)

    def retry_telemetry(self) -> int:
        if self.telemetry_dispatcher is None:
            return 0
        queued = 0
        for payload in self.snapshot()["telemetryOutbox"].values():
            if self.telemetry_dispatcher.submit(payload):
                queued += 1
        return queued

    def record_started(self, *, observed_at: str) -> bool:
        def apply(state: dict[str, Any]) -> dict[str, Any]:
            markers = state.setdefault("localEventMarkers", {})
            boot_id = state["identity"]["bootId"]
            started = markers.get("sentinelStartedBootId") != boot_id
            setup_ready = (
                state["setup"].get("state") == "ready"
                and markers.get("setupReadyRevision")
                != state["identity"]["setupRevision"]
            )
            if started:
                markers["sentinelStartedBootId"] = boot_id
            if setup_ready:
                markers["setupReadyRevision"] = state["identity"]["setupRevision"]
            queued = []
            if started:
                queued.append(self._queue_local_event(state, "sentinel_started", observed_at))
            if setup_ready:
                queued.append(self._queue_local_event(state, "setup_ready", observed_at))
            return {"started": started, "setupReady": setup_ready, "queued": queued}

        events = self.store.transaction(apply)
        self.retry_local_events()
        return events["started"] or events["setupReady"]

    def retry_local_events(self) -> int:
        if self.event_sink is None:
            return 0
        delivered = 0
        pending = self.snapshot().get("localEventOutbox", [])
        if isinstance(pending, dict):
            # Read old state without reordering newly written records.  Legacy
            # entries have no source order, so their insertion order is the
            # only evidence available during migration.
            pending = list(pending.values())
        for payload in sorted(
            list(pending), key=lambda item: int(item.get("sourceSequence") or 0)
        ):
            if self._deliver_local_event(payload):
                delivered += 1
            else:
                break
        return delivered

    def observe_public_mapping(
        self,
        *,
        reservation_id: str,
        slot_generation: int,
        internal_port: int,
        external_port: int,
        mapping_generation: str,
        observed_at: str,
    ) -> dict[str, Any]:
        if internal_port != 49200 or external_port < 1:
            raise CommandRejected("mapping_invalid", "A public mapping for internal port 49200 is required")

        def apply(state: dict[str, Any]) -> dict[str, Any]:
            identity = state["identity"]
            if reservation_id != identity["reservationId"] or slot_generation != identity["slotGeneration"]:
                raise CommandRejected("mapping_scope_stale", "Public mapping targets another reservation generation")
            if state["training"]["state"] == "started":
                current = state["publicMapping"]
                if current["mappingGeneration"] != mapping_generation or current["externalPort"] != external_port:
                    raise CommandRejected("post_start_mapping_change", "Public mapping cannot change after training starts")
            state["publicMapping"] = {
                "state": "ready",
                "internalPort": 49200,
                "externalPort": external_port,
                "mappingGeneration": mapping_generation,
                "observedAt": observed_at,
            }
            return copy.deepcopy(state["publicMapping"])

        result = self.store.transaction(apply)
        self._emit_material("public_mapping_ready", observed_at)
        return result

    def observe_service_reachability(self, *, reachable: bool, observed_at: str) -> dict[str, Any]:
        def apply(state: dict[str, Any]) -> dict[str, Any]:
            if state["training"]["state"] == "not_started" and not reachable:
                return {"accepted": True, "gating": False, "reason": "pre_start_probe_observational_only"}
            state["training"]["serviceReachable"] = bool(reachable)
            state["training"]["serviceObservedAt"] = observed_at
            return {"accepted": True, "gating": False, "reachable": bool(reachable)}

        return self.store.transaction(apply)

    def observe_training_process(
        self,
        *,
        running: bool,
        observed_at: str,
        training_run_id: str | None = None,
        training_session_id: str | None = None,
        training_plan_id: str | None = None,
        configuration_revision: str | None = None,
    ) -> dict[str, Any]:
        def apply(state: dict[str, Any]) -> dict[str, Any]:
            if not running:
                return {"accepted": True, "running": False, "state": state["training"]["state"]}
            if state["setup"]["state"] != "ready" or state["publicMapping"]["state"] != "ready":
                raise CommandRejected(
                    "training_observation_out_of_order",
                    "Observed training cannot become current before setup and public mapping are ready",
                )
            current_run = state["training"].get("trainingRunId")
            current_session = state["training"].get("trainingSessionId")
            new_session = (
                training_session_id is not None
                and training_session_id != current_session
            )
            if (
                current_session
                and training_session_id == current_session
                and current_run
                and training_run_id
                and current_run != training_run_id
            ):
                raise CommandRejected(
                    "training_run_binding_conflict",
                    "A live training session cannot change its exact run binding",
                )
            changed = state["training"]["state"] != "started" or new_session
            resolved_run = (
                training_run_id
                if training_run_id is not None
                else None
                if new_session
                else current_run
            )
            resolved_session = training_session_id or current_session
            if resolved_run:
                state["runBindingGap"] = None
            else:
                state["runBindingGap"] = {
                    "reason": "training_run_id_missing",
                    "observedAt": observed_at,
                    "trainingSessionId": resolved_session,
                }
            if new_session:
                state["logObservationSession"] = None
            state["training"].update({
                "state": "started",
                "startedAt": observed_at if changed else state["training"].get("startedAt") or observed_at,
                "trainingRunId": resolved_run,
                "trainingSessionId": resolved_session,
                "trainingPlanId": training_plan_id if new_session else training_plan_id or state["training"].get("trainingPlanId"),
                "configurationRevision": configuration_revision if new_session else configuration_revision or state["training"].get("configurationRevision"),
            })
            if changed and state["training"].get("trainingRunId"):
                self._queue_local_event(state, "training_started", observed_at)
            return {"accepted": True, "running": True, "state": "started"}

        result = self.store.transaction(apply)
        self.retry_local_events()
        return result

    def observe_logs(
        self,
        text: str,
        *,
        observed_at: str,
        source: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        evidence = classify_agora_evidence(text, captured_at=observed_at)
        def apply(state: dict[str, Any]) -> dict[str, Any]:
            before_join = (state.get("latestEvidence") or {}).get("joinState")
            before_error = state.get("activeErrorId")
            state["latestEvidence"] = {**evidence, "observedAt": observed_at}
            if evidence["joinState"] == "joined":
                state["protection"]["joined"] = True
                state["protection"]["joinedAt"] = state["protection"]["joinedAt"] or observed_at
            if evidence.get("kind") == "join" and evidence.get("nodeName"):
                identity = state["identity"]
                occurrence = own_server_join_occurrence(
                    str(evidence.get("evidence") or text),
                    observed_at,
                    identity={
                        "provider": identity.get("provider"),
                        "accountScope": identity.get("accountScope"),
                        "resourceId": identity.get("providerResourceId"),
                    },
                    machine_generation_id=identity.get("machineGenerationId"),
                    assignment_generation=identity.get("slotGeneration"),
                    assignment_operation_id=identity.get("assignmentOperationId"),
                    boot_id=identity.get("bootId"),
                    node_name=str(evidence["nodeName"]),
                )
                if occurrence is not None:
                    join_id, joined_at = occurrence
                    state["logObservationSession"] = {
                        "joinId": join_id,
                        "nodeName": evidence["nodeName"],
                        "joinedAt": joined_at,
                        "bootId": identity.get("bootId"),
                        "trainingRunId": state["training"].get("trainingRunId"),
                        "trainingSessionId": state["training"].get("trainingSessionId"),
                    }
            if evidence["rawError"]:
                error_group_id = hashlib.sha256(
                    f"{evidence['errorCategory']}\0{evidence['rawError']}".encode(
                        "utf-8"
                    )
                ).hexdigest()[:24]
                error_reported_at = evidence.get("reportedAt")
                process = state["process"]
                replacement = process.get("lastIdentityReplacement")
                existing = state["errors"].get(error_group_id)
                prior_correlation = (
                    existing.get("recoveryCorrelation")
                    if isinstance(existing, dict)
                    else None
                )
                training_run_id = state["training"].get("trainingRunId")
                training_session_id = state["training"].get("trainingSessionId")
                occurrence_id, occurrence_source = _fatal_occurrence(
                    source,
                    error_group_id=error_group_id,
                    training_run_id=training_run_id,
                )
                distinct_occurrence = isinstance(existing, dict) and (
                    occurrence_id is None
                    or existing.get("occurrenceId") != occurrence_id
                )
                same_exact_occurrence = (
                    isinstance(existing, dict)
                    and occurrence_id is not None
                    and existing.get("occurrenceId") == occurrence_id
                )
                persisted_occurrence_known_at = (
                    existing.get("occurrenceKnownAt")
                    if same_exact_occurrence
                    else None
                )
                already_resolved_by_replacement = (
                    isinstance(existing, dict)
                    and existing.get("status") == "restored"
                    and occurrence_id is not None
                    and isinstance(prior_correlation, dict)
                    and prior_correlation.get("kind") == "identity_replacement"
                    and prior_correlation.get("ordering") == "before"
                    and prior_correlation.get("occurrenceId") == occurrence_id
                    and prior_correlation.get("errorGroupId") == error_group_id
                    and prior_correlation.get("trainingRunId") == training_run_id
                    and prior_correlation.get("trainingSessionId")
                    == training_session_id
                )
                source_replacement_order = (
                    "unknown"
                    if distinct_occurrence
                    else _fatal_replacement_order(
                        error_reported_at,
                        replacement,
                        training_run_id=training_run_id,
                        training_session_id=training_session_id,
                    )
                )
                inferred_correlation = (
                    _fatal_replacement_correlation(
                        error_reported_at,
                        observed_at,
                        replacement,
                        error_group_id=error_group_id,
                        training_run_id=training_run_id,
                        training_session_id=training_session_id,
                        occurrence_id=occurrence_id,
                        occurrence_source=occurrence_source,
                        occurrence_known_at=persisted_occurrence_known_at,
                    )
                    if source_replacement_order == "before"
                    else None
                )
                recovery_correlation = (
                    inferred_correlation
                    if inferred_correlation is not None
                    else copy.deepcopy(prior_correlation)
                    if already_resolved_by_replacement
                    else None
                )
                replacement_supersedes_error = recovery_correlation is not None
                replacement_order = (
                    "before"
                    if inferred_correlation is not None
                    else source_replacement_order
                    if source_replacement_order == "after"
                    else "unknown"
                )
                restored_at = (
                    recovery_correlation.get("replacementObservedAt")
                    if isinstance(recovery_correlation, dict)
                    else None
                )
                if existing is None:
                    state["errors"][error_group_id] = {
                        "errorId": error_group_id,
                        "errorGroupId": error_group_id,
                        "category": evidence["errorCategory"],
                        "rawError": evidence["rawError"],
                        "reportedAt": error_reported_at,
                        "occurrenceId": occurrence_id,
                        "occurrenceSource": copy.deepcopy(occurrence_source),
                        "occurrenceKnownAt": observed_at,
                        "status": (
                            "restored" if replacement_supersedes_error else "active"
                        ),
                        "firstObservedAt": observed_at,
                        "lastObservedAt": observed_at,
                        "acknowledgedAt": None,
                        "acknowledgedBy": None,
                        "restoredAt": restored_at,
                        "recoveryCorrelation": recovery_correlation,
                    }
                else:
                    existing["lastObservedAt"] = observed_at
                    existing["errorGroupId"] = error_group_id
                    existing["reportedAt"] = error_reported_at
                    existing["occurrenceId"] = occurrence_id
                    existing["occurrenceSource"] = copy.deepcopy(occurrence_source)
                    if distinct_occurrence:
                        existing["occurrenceKnownAt"] = observed_at
                    existing["status"] = (
                        "restored"
                        if replacement_supersedes_error
                        else "acknowledged"
                        if existing["acknowledgedAt"]
                        else "active"
                    )
                    existing["restoredAt"] = restored_at
                    existing["recoveryCorrelation"] = recovery_correlation
                state["activeErrorId"] = (
                    None if replacement_supersedes_error else error_group_id
                )
                state["latestEvidence"]["errorId"] = error_group_id
                state["latestEvidence"]["errorGroupId"] = error_group_id
                state["latestEvidence"]["occurrenceId"] = occurrence_id
                if replacement_supersedes_error:
                    episode = process.get("recoveryEpisode")
                    if (
                        isinstance(episode, dict)
                        and episode.get("errorId") == error_group_id
                        and episode.get("occurrenceId") == occurrence_id
                    ):
                        process["recoveryEpisode"] = None
                    if (
                        inferred_correlation is not None
                        and not already_resolved_by_replacement
                    ):
                        self._queue_local_event(
                            state,
                            "process_restored",
                            restored_at,
                            evidence={
                                "ownedProcessVerifiedRunning": True,
                                "ownedProcessIdentity": replacement.get(
                                    "currentIdentity"
                                ),
                                "errorId": error_group_id,
                                "errorGroupId": error_group_id,
                                "occurrenceId": occurrence_id,
                                "occurrenceSource": copy.deepcopy(
                                    occurrence_source
                                ),
                                "recoveryCause": "late_fatal_before_verified_replacement",
                                "recoveryCorrelation": recovery_correlation,
                            },
                            source_event_id=hashlib.sha256(
                                f"late-fatal-recovery\0{error_group_id}\0{occurrence_id or ''}\0{restored_at}".encode()
                            ).hexdigest(),
                        )
                else:
                    episode = process.get("recoveryEpisode")
                    advance_episode = not isinstance(episode, dict) or (
                        episode.get("errorId") == error_group_id
                        and (
                            occurrence_id is None
                            or episode.get("occurrenceId") != occurrence_id
                        )
                    )
                    if advance_episode:
                        process["recoveryEpisode"] = {
                            "episodeId": (
                                f"fatal:{error_group_id}:{occurrence_id}"
                                if occurrence_id
                                else f"fatal:{error_group_id}"
                            ),
                            "cause": "fatal_error",
                            "errorId": error_group_id,
                            "errorGroupId": error_group_id,
                            "occurrenceId": occurrence_id,
                            "occurrenceSource": copy.deepcopy(occurrence_source),
                            "occurrenceKnownAt": observed_at,
                            "occurredAt": error_reported_at,
                            "reportedAt": error_reported_at,
                            "capturedAt": observed_at,
                            "timeBasis": ("source" if error_reported_at else "unknown"),
                            "replacementOrdering": replacement_order,
                            "observedAt": observed_at,
                            "trainingRunId": state["training"].get("trainingRunId"),
                            "trainingSessionId": state["training"].get(
                                "trainingSessionId"
                            ),
                            "bootId": state["identity"].get("bootId"),
                            "processIdentityAtFailure": (
                                copy.deepcopy(process.get("ownedProcessIdentity"))
                                if process.get("ownedProcessVerifiedRunning")
                                else None
                            ),
                            "downtimeObserved": False,
                        }
            elif state["activeErrorId"] is not None:
                # Preserve the legacy acknowledged-error ledger. Canonical
                # recovery still requires process_restored with explicit owned
                # process proof and is not emitted from this log transition.
                active = state["errors"][state["activeErrorId"]]
                active["status"] = "restored"
                active["restoredAt"] = observed_at
                state["activeErrorId"] = None
            after_join = state["latestEvidence"].get("joinState")
            after_error = state.get("activeErrorId")
            if source is not None and evidence.get("recognized"):
                source_id = hashlib.sha256(
                    json.dumps(
                        {
                            "source": source,
                            "evidence": evidence.get("evidence"),
                            "interpretationVersion": "local-observation.v1",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                join_session = state.get("logObservationSession")
                if isinstance(join_session, dict):
                    current = state["training"]
                    identity = state["identity"]
                    if (
                        join_session.get("bootId") != identity.get("bootId")
                        or join_session.get("trainingRunId") != current.get("trainingRunId")
                        or join_session.get("trainingSessionId") != current.get("trainingSessionId")
                    ):
                        join_session = None
                observation = canonical_observation(
                    evidence,
                    captured_at=observed_at,
                    source={**source, "line": text},
                    identity=state["identity"],
                    training_run_id=state["training"].get("trainingRunId"),
                    training_session_id=state["training"].get("trainingSessionId"),
                    join_session=join_session if evidence.get("kind") in {"join", "batch_summary"} else None,
                )
                self._queue_local_event(
                    state,
                    "observation_recorded",
                    observation.get("reportedAt") or observed_at,
                    evidence={"observation": observation},
                    source_event_id=source_id,
                )
            if before_join != after_join:
                self._queue_local_event(
                    state,
                    "join_state_changed",
                    observed_at,
                    evidence={"previous": before_join, "current": after_join},
                )
            if before_error != after_error:
                if after_error is not None:
                    error = state["errors"][after_error]
                    self._queue_local_event(
                        state,
                        "error_raised",
                        observed_at,
                        evidence={
                            "errorId": after_error,
                            "errorGroupId": error.get("errorGroupId") or after_error,
                            "occurrenceId": error.get("occurrenceId"),
                            "occurrenceSource": copy.deepcopy(
                                error.get("occurrenceSource")
                            ),
                            "category": error.get("category"),
                        },
                    )
                elif before_error is not None:
                    self._queue_local_event(
                        state,
                        "error_restored",
                        observed_at,
                        evidence={"errorId": before_error},
                    )
            return copy.deepcopy(state["latestEvidence"])

        result = self.store.transaction(apply)
        self._emit_material("evidence_changed", observed_at)
        self.retry_local_events()
        return result

    def observe_active_logs(
        self, logs: list[dict[str, Any]], *, observed_at: str
    ) -> list[dict[str, Any]]:
        allowed = {
            "progress.log",
            "server_gpu0.log",
            "launcher-gpu0.log",
            "launcher-active.log",
        }
        normalized: list[dict[str, Any]] = []
        for value in logs[:4]:
            if not isinstance(value, dict) or value.get("name") not in allowed:
                raise CommandRejected("active_log_invalid", "Active log metadata is outside the owned allowlist")
            size = value.get("sizeBytes")
            inode = value.get("inode")
            if not isinstance(size, int) or size < 0 or not isinstance(inode, int) or inode < 0:
                raise CommandRejected("active_log_invalid", "Active log metadata is invalid")
            normalized.append({
                "name": value["name"],
                "inode": inode,
                "sizeBytes": size,
                "modifiedAtMs": int(value.get("modifiedAtMs") or 0),
                "observedAt": observed_at,
            })
        normalized.sort(key=lambda item: item["name"])

        def apply(state: dict[str, Any]) -> list[dict[str, Any]]:
            state["activeLogs"] = copy.deepcopy(normalized)
            return copy.deepcopy(normalized)

        return self.store.transaction(apply)

    def acknowledge_error(self, error_id: str, *, acknowledged_at: str, acknowledged_by: str) -> dict[str, Any]:
        def apply(state: dict[str, Any]) -> dict[str, Any]:
            error = state["errors"].get(error_id)
            if error is None:
                raise CommandRejected("error_missing", "Error is not present in the durable lifecycle")
            if error["acknowledgedAt"] is None:
                error["acknowledgedAt"] = acknowledged_at
                error["acknowledgedBy"] = acknowledged_by
                if error["status"] != "restored":
                    error["status"] = "acknowledged"
            return copy.deepcopy(error)

        result = self.store.transaction(apply)
        self._emit_material("error_acknowledged", acknowledged_at)
        return result

    def mark_warm(self, *, observed_at: str) -> dict[str, Any]:
        def apply(state: dict[str, Any]) -> dict[str, Any]:
            state["protection"]["warm"] = True
            state["protection"]["warmAt"] = state["protection"]["warmAt"] or observed_at
            return copy.deepcopy(state["protection"])

        result = self.store.transaction(apply)
        self._emit_material("machine_warm_protected", observed_at)
        return result

    def observe_process_evidence(
        self,
        *,
        tmux_agora: bool,
        watchdog: bool,
        observed_at: str,
        tmux_session_id: str | None = None,
        tmux_window_id: str | None = None,
        tmux_pane_id: str | None = None,
        tmux_pane_pid: int | None = None,
        watchdog_instance_id: str | None = None,
        retry_continuity_token: str | None = None,
        owned_process_verified_running: bool = False,
        owned_process_identity: dict[str, Any] | None = None,
        _passive_observation: bool = False,
    ) -> dict[str, Any]:
        normalized_owned_identity = None
        if owned_process_identity is not None:
            if set(owned_process_identity) != {
                "pid", "processGroupId", "startedTicks", "kind",
            }:
                raise CommandRejected(
                    "owned_process_identity_invalid",
                    "Owned process identity does not match the exact schema",
                )
            if (
                any(
                    not isinstance(owned_process_identity[key], int)
                    or isinstance(owned_process_identity[key], bool)
                    or owned_process_identity[key] < 1
                    for key in ("pid", "processGroupId", "startedTicks")
                )
                or owned_process_identity["kind"] not in {"server", "cli"}
            ):
                raise CommandRejected(
                    "owned_process_identity_invalid",
                    "Owned process identity is invalid",
                )
            normalized_owned_identity = copy.deepcopy(owned_process_identity)
        if normalized_owned_identity is not None and not owned_process_verified_running:
            raise CommandRejected(
                "owned_process_identity_invalid",
                "Owned process identity requires verified running proof",
            )

        def apply(state: dict[str, Any]) -> dict[str, Any]:
            current = state["process"]
            protected = state["protection"]["joined"] or state["protection"]["warm"]
            process_boot = current.get("bootId")
            current_boot = state["identity"].get("bootId")
            same_boot = process_boot is None or process_boot == current_boot
            identity_keys = (
                "tmuxSessionId", "tmuxWindowId", "tmuxPaneId", "tmuxPanePid",
                "watchdogInstanceId", "retryContinuityToken",
            )
            proposed = {
                "tmuxAgora": bool(tmux_agora),
                "watchdog": bool(watchdog),
                "observedAt": observed_at,
                "tmuxSessionId": tmux_session_id,
                "tmuxWindowId": tmux_window_id,
                "tmuxPaneId": tmux_pane_id,
                "tmuxPanePid": tmux_pane_pid,
                "watchdogInstanceId": watchdog_instance_id,
                "retryContinuityToken": retry_continuity_token,
                "ownedProcessVerifiedRunning": bool(owned_process_verified_running),
                "ownedProcessIdentity": normalized_owned_identity,
                "lastOwnedProcessIdentity": (
                    normalized_owned_identity
                    or current.get("lastOwnedProcessIdentity")
                    or current.get("ownedProcessIdentity")
                ),
                "recoveryOutstanding": bool(current.get("recoveryOutstanding")),
                "lossObservedAt": current.get("lossObservedAt"),
                "recoveryEpisode": copy.deepcopy(current.get("recoveryEpisode")),
                "unresolvedRecoveryEpisodes": copy.deepcopy(
                    current.get("unresolvedRecoveryEpisodes") or []
                ),
                "lastVerifiedIdentityAt": current.get("lastVerifiedIdentityAt"),
                "lastIdentityReplacement": copy.deepcopy(
                    current.get("lastIdentityReplacement")
                ),
                "protectedIdentity": (
                    copy.deepcopy(current.get("protectedIdentity"))
                    if same_boot else None
                ),
                "identityDiscrepancy": (
                    copy.deepcopy(current.get("identityDiscrepancy"))
                    if same_boot else None
                ),
                "bootId": current_boot,
            }
            passive_identity_changes: dict[str, dict[str, Any]] = {}
            if protected and same_boot and current.get("tmuxAgora") is True:
                for key in identity_keys:
                    if proposed[key] is None and not _passive_observation:
                        proposed[key] = current.get(key)
                    if (
                        tmux_agora
                        and current.get(key) is not None
                        and proposed[key] != current[key]
                    ):
                        if not _passive_observation:
                            raise CommandRejected(
                                "protected_process_identity_changed",
                                f"Protected machine {key} changed",
                            )
                        passive_identity_changes[key] = {
                            "protected": current.get(key),
                            "observed": proposed.get(key),
                        }
            if protected and proposed["protectedIdentity"] is None and current.get("tmuxAgora"):
                proposed["protectedIdentity"] = {
                    **{key: current.get(key) for key in identity_keys},
                    "bootId": process_boot or current_boot,
                }
            if (
                protected
                and same_boot
                and (
                    current.get("tmuxAgora") is False
                    or bool(passive_identity_changes)
                )
                and tmux_agora
                and isinstance(proposed.get("protectedIdentity"), dict)
            ):
                changed = {
                    key: {
                        "protected": proposed["protectedIdentity"].get(key),
                        "observed": proposed.get(key),
                    }
                    for key in identity_keys
                    if proposed["protectedIdentity"].get(key) is not None
                    and proposed.get(key) is not None
                    and proposed["protectedIdentity"].get(key) != proposed.get(key)
                }
                proposed["identityDiscrepancy"] = (
                    {
                        "observedAt": observed_at,
                        "bootId": current_boot,
                        "fields": changed,
                    }
                    if changed else None
                )
            if passive_identity_changes:
                self._queue_local_event(
                    state,
                    "process_identity_changed",
                    observed_at,
                    evidence={
                        "passiveObservation": True,
                        "fields": passive_identity_changes,
                        "ownedProcessVerifiedRunning": bool(
                            owned_process_verified_running
                        ),
                    },
                )

            previous_owned_identity = current.get("ownedProcessIdentity")
            previous_owned_boot = current.get("bootId")
            boot_identity_changed = (
                previous_owned_boot is not None
                and current_boot is not None
                and previous_owned_boot != current_boot
            )
            owned_identity_changed = (
                bool(current.get("ownedProcessVerifiedRunning"))
                and bool(owned_process_verified_running)
                and isinstance(previous_owned_identity, dict)
                and isinstance(normalized_owned_identity, dict)
                and (
                    previous_owned_identity != normalized_owned_identity
                    or boot_identity_changed
                )
            )
            if owned_process_verified_running:
                proposed["lastVerifiedIdentityAt"] = observed_at
            if owned_identity_changed:
                proposed["lastIdentityReplacement"] = {
                    "observedAt": observed_at,
                    "previousIdentityVerifiedAt": current.get("lastVerifiedIdentityAt"),
                    "previousIdentity": copy.deepcopy(previous_owned_identity),
                    "currentIdentity": copy.deepcopy(normalized_owned_identity),
                    "trainingRunId": state["training"].get("trainingRunId"),
                    "trainingSessionId": state["training"].get("trainingSessionId"),
                    "previousBootId": previous_owned_boot,
                    "bootId": current_boot,
                }

            previously_present = (
                current.get("tmuxAgora") is True
                or bool(current.get("ownedProcessVerifiedRunning"))
            )
            lost_now = previously_present and (
                not tmux_agora
                or (
                    bool(current.get("ownedProcessVerifiedRunning"))
                    and not owned_process_verified_running
                )
            )
            if lost_now and not proposed["recoveryOutstanding"]:
                proposed["recoveryOutstanding"] = True
                proposed["lossObservedAt"] = observed_at
                episode = proposed.get("recoveryEpisode")
                if isinstance(episode, dict):
                    episode["downtimeObserved"] = True
                    episode["lossObservedAt"] = observed_at
                else:
                    episode = {
                        "episodeId": hashlib.sha256(
                            f"process-loss\0{current_boot}\0{observed_at}".encode()
                        ).hexdigest()[:24],
                        "cause": "observed_process_loss",
                        "errorId": None,
                        "occurredAt": observed_at,
                        "observedAt": observed_at,
                        "trainingRunId": state["training"].get("trainingRunId"),
                        "trainingSessionId": state["training"].get("trainingSessionId"),
                        "bootId": current_boot,
                        "processIdentityAtFailure": copy.deepcopy(
                            current.get("ownedProcessIdentity")
                        ),
                        "downtimeObserved": True,
                        "lossObservedAt": observed_at,
                    }
                    proposed["recoveryEpisode"] = episode
                self._queue_local_event(
                    state,
                    "process_lost",
                    observed_at,
                    evidence={
                        "ownedProcessVerifiedRunning": False,
                        "lastOwnedProcessIdentity": proposed["lastOwnedProcessIdentity"],
                        "recoveryEpisode": copy.deepcopy(episode),
                    },
                )
            episode = proposed.get("recoveryEpisode")
            if proposed["recoveryOutstanding"] and not isinstance(episode, dict):
                episode = {
                    "episodeId": "legacy-process-loss",
                    "cause": "observed_process_loss",
                    "errorId": None,
                    "occurredAt": proposed.get("lossObservedAt") or observed_at,
                    "observedAt": proposed.get("lossObservedAt") or observed_at,
                    "trainingRunId": state["training"].get("trainingRunId"),
                    "trainingSessionId": state["training"].get("trainingSessionId"),
                    "bootId": current_boot,
                    "processIdentityAtFailure": copy.deepcopy(
                        current.get("lastOwnedProcessIdentity")
                    ),
                    "downtimeObserved": True,
                }
                proposed["recoveryEpisode"] = episode
            episode_matches_run = isinstance(episode, dict) and episode.get(
                "trainingRunId"
            ) == state["training"].get("trainingRunId")
            replacement = proposed.get("lastIdentityReplacement")
            fatal_correlation = (
                _fatal_replacement_correlation(
                    episode.get("reportedAt"),
                    episode.get("capturedAt")
                    or episode.get("observedAt")
                    or observed_at,
                    replacement,
                    error_group_id=episode.get("errorGroupId")
                    or episode.get("errorId"),
                    training_run_id=state["training"].get("trainingRunId"),
                    training_session_id=state["training"].get("trainingSessionId"),
                    failure_boot_id=episode.get("bootId"),
                    occurrence_id=episode.get("occurrenceId"),
                    occurrence_source=episode.get("occurrenceSource"),
                    occurrence_known_at=episode.get("occurrenceKnownAt"),
                )
                if (
                    isinstance(episode, dict)
                    and episode.get("cause") == "fatal_error"
                    and owned_identity_changed
                )
                else None
            )
            fatal_replacement = fatal_correlation is not None
            restored_now = (
                bool(owned_process_verified_running)
                and (proposed["recoveryOutstanding"] or fatal_replacement)
                and episode_matches_run
            )
            if (
                isinstance(episode, dict)
                and bool(owned_process_verified_running)
                and not episode_matches_run
            ):
                unresolved = proposed["unresolvedRecoveryEpisodes"]
                unresolved.append(copy.deepcopy(episode))
                proposed["unresolvedRecoveryEpisodes"] = unresolved[-20:]
                proposed["recoveryEpisode"] = None
                proposed["recoveryOutstanding"] = False
                proposed["lossObservedAt"] = None
            if restored_now:
                proposed["recoveryOutstanding"] = False
                proposed["lossObservedAt"] = None
                self._queue_local_event(
                    state,
                    "process_restored",
                    observed_at,
                    evidence={
                        "ownedProcessVerifiedRunning": True,
                        "ownedProcessIdentity": normalized_owned_identity,
                        "recoveryEpisode": copy.deepcopy(episode),
                        "recoveryCorrelation": copy.deepcopy(fatal_correlation),
                    },
                )
                active_error_id = state.get("activeErrorId") or (
                    episode.get("errorId") if isinstance(episode, dict) else None
                )
                if active_error_id is not None:
                    active = state["errors"][active_error_id]
                    was_restored = active["status"] == "restored"
                    active["status"] = "restored"
                    active["restoredAt"] = active.get("restoredAt") or observed_at
                    if (
                        fatal_replacement
                        and isinstance(episode, dict)
                        and episode.get("errorId") == active_error_id
                    ):
                        active["recoveryCorrelation"] = fatal_correlation
                    state["activeErrorId"] = None
                    if not was_restored:
                        self._queue_local_event(
                            state,
                            "error_restored",
                            observed_at,
                            evidence={
                                "errorId": active_error_id,
                                "ownedProcessVerifiedRunning": True,
                                "recoveryCorrelation": copy.deepcopy(
                                    fatal_correlation
                                ),
                            },
                        )
                proposed["recoveryEpisode"] = None
            state["process"] = proposed
            return copy.deepcopy(state["process"])

        result = self.store.transaction(apply)
        self._emit_material("process_evidence_changed", observed_at)
        self.retry_local_events()
        return result

    def observe_passive_process_evidence(self, **evidence: Any) -> dict[str, Any]:
        """Record process discovery without granting mutation authority."""

        return self.observe_process_evidence(
            **evidence,
            _passive_observation=True,
        )

    def execute_command(self, command: dict[str, Any], *, now: str) -> dict[str, Any]:
        with self._execution_lock:
            return self._execute_command_serial(command, now=now)

    def _execute_command_serial(self, command: dict[str, Any], *, now: str) -> dict[str, Any]:
        normalized = self._validate_command(command, now)
        claim = self._journal_received(normalized, now)
        if not claim["claimed"]:
            existing = claim["record"]
            if existing.get("intentHash") != normalized["intentHash"] or existing.get("commandHash") != normalized["commandHash"]:
                raise CommandRejected("command_identity_conflict", "Command ID was reused with different intent")
            if existing["status"] == "executed":
                return {"status": "executed", "duplicate": True, "effect": existing.get("effect")}
            if existing["status"] == "rejected":
                raise CommandRejected(existing["reasonCode"], existing["reason"])
            if existing["status"] in {"received", "executing", "effect_unknown"}:
                raise CommandRejected(
                    "command_effect_unknown",
                    "Command may have executed and requires observation-based reconciliation",
                )

        try:
            gate = self._gate(normalized, now)
            self._journal_executing(normalized, now, gate)
            effect_arguments = copy.deepcopy(normalized.get("arguments", {}))
            effect = self.effects[normalized["action"]](effect_arguments)
            result = self._journal_executed(normalized, now, effect, gate)
        except CommandRejected as error:
            self._journal_rejected(normalized, now, error)
            raise
        except Exception as error:
            self._journal_unknown(normalized, now, error)
            raise
        self._emit_material(f"command_{normalized['action']}_executed", now)
        self.retry_local_events()
        return result

    def pending_commands(self, *, after_cursor: int = 0) -> list[dict[str, Any]]:
        state = self.snapshot()
        output = []
        for command_id in state["commandOrder"]:
            record = state["commands"][command_id]
            if record["cursor"] > after_cursor and record["status"] in {"received", "effect_unknown"}:
                output.append(copy.deepcopy(record))
        return output

    def reconcile_command(self, command_id: str, *, effect_observed: bool, observed_at: str) -> dict[str, Any]:
        def apply(state: dict[str, Any]) -> dict[str, Any]:
            record = state["commands"].get(command_id)
            if not record:
                raise CommandRejected("command_missing", "Command is not journaled")
            if record["status"] == "executed":
                return {"status": "executed", "duplicate": True}
            if record["status"] not in {"executing", "effect_unknown"}:
                raise CommandRejected("command_not_reconcilable", "Command is not awaiting effect reconciliation")
            if not effect_observed:
                record.update({
                    "status": "rejected",
                    "rejectedAt": observed_at,
                    "reasonCode": "effect_absent_after_reconciliation",
                    "reason": "Expected local effect was conclusively absent",
                })
                return {"status": "rejected", "duplicate": False}
            record.update({"status": "executed", "executedAt": observed_at, "reconciled": True})
            self._apply_action_state(state, record, observed_at)
            event_type = self._action_event_type(record.get("action"))
            if event_type is not None:
                self._queue_local_event(state, event_type, observed_at)
            return {"status": "executed", "duplicate": False, "reconciled": True}

        result = self.store.transaction(apply)
        self._emit_material("command_reconciled", observed_at)
        self.retry_local_events()
        return result

    def recover_interrupted_commands(self, *, observed_at: str) -> list[str]:
        def apply(state: dict[str, Any]) -> list[str]:
            recovered = []
            for command_id in state["commandOrder"]:
                record = state["commands"][command_id]
                if record["status"] == "executing":
                    record.update({
                        "status": "effect_unknown",
                        "effectUnknownAt": observed_at,
                        "reasonCode": "command_effect_unknown",
                        "reason": "Sentinel restarted after durable execution began",
                    })
                    recovered.append(command_id)
            return recovered

        return self.store.transaction(apply)

    def build_heartbeat(self, *, seq: int, sent_at: str) -> dict[str, Any]:
        return heartbeat_payload(self.snapshot(), seq=seq, sent_at=sent_at)

    def _validate_command(self, command: dict[str, Any], now: str) -> dict[str, Any]:
        if not isinstance(command, dict) or set(command) != COMMAND_FIELDS:
            raise CommandRejected("command_invalid", "Command does not match the exact wire schema")
        _identifier(command["commandId"], "commandId")
        action = command["action"]
        if action not in ALLOWED_ACTIONS:
            raise CommandRejected("action_not_allowed", "Command action is not allowlisted")
        if _parse_time(command["expiresAt"]) <= _parse_time(now):
            raise CommandRejected("command_expired", "Command has expired")
        state = self.snapshot()
        identity = state["identity"]
        if command["authorityEpoch"] != identity["authorityEpoch"]:
            raise CommandRejected("authority_epoch_stale", "Command authority epoch is stale")
        scope = command["scope"]
        if not isinstance(scope, dict) or set(scope) != SCOPE_FIELDS:
            raise CommandRejected("command_invalid", "Command scope does not match the exact schema")
        for key in ("reservationId", "slotGeneration", "machineId", "bootId", "setupRevision"):
            if scope.get(key) != identity[key]:
                raise CommandRejected("command_scope_stale", f"Command {key} is stale")
        issued_at = _parse_time(command["issuedAt"])
        if issued_at > _parse_time(now):
            raise CommandRejected("command_issued_in_future", "Command issue time is in the future")
        last_issued_at = state.get("lastAcceptedIssuedAt")
        if last_issued_at and issued_at < _parse_time(last_issued_at):
            raise CommandRejected("command_reordered", "Command predates the last accepted command")
        normalized = copy.deepcopy(command)
        normalized["arguments"] = _exact_arguments(action, command["arguments"])
        normalized["intentHash"] = _intent_hash(command)
        normalized["commandHash"] = exact_command_hash(command)
        return normalized

    def _gate(self, command: dict[str, Any], now: str) -> dict[str, Any]:
        state = self.snapshot()
        action = command["action"]
        protection = state["protection"]
        if (protection["joined"] or protection["warm"]) and action in {
            "prepare_setup", "start_training", "cancel_training",
        }:
            raise CommandRejected(
                "protected_machine",
                "Joined and warm machines exclude provisioning and cancellation commands",
            )
        if state["training"]["state"] == "started" and action not in POST_START_ACTIONS:
            raise CommandRejected("post_start_action_not_allowed", "Only proved maintenance is allowed after start")
        if action == "prepare_setup":
            if state["setup"]["state"] != "pending":
                raise CommandRejected("setup_already_prepared", "Setup is not pending")
            if command["arguments"]["setupRevision"] != state["identity"]["setupRevision"]:
                raise CommandRejected("setup_revision_stale", "Setup command targets another revision")
            return {"setupMayStartBeforePublicMapping": True}
        if action == "start_training":
            training = state["training"]
            if training["state"] == "started":
                raise CommandRejected("training_already_started", "Training is already live")
            if (
                training["state"] in {"stopped", "cancelled"}
                and training.get("trainingRunId") == command["arguments"]["trainingRunId"]
            ):
                raise CommandRejected(
                    "training_run_terminal",
                    "A stopped or cancelled training run cannot be restarted",
                )
            if state["setup"]["state"] != "ready":
                raise CommandRejected("setup_not_ready", "Training requires setup_ready")
            mapping = state["publicMapping"]
            if mapping["state"] != "ready" or mapping["internalPort"] != 49200:
                raise CommandRejected("public_mapping_not_ready", "Training requires public_mapping_ready")
            if command["arguments"]["announcePort"] != mapping["externalPort"]:
                raise CommandRejected("announce_port_stale", "Training command targets another public mapping")
            return {
                "announcePort": mapping["externalPort"],
                "mappingGeneration": mapping["mappingGeneration"],
                "preStartTcpProbeRequired": False,
            }
        if action == "stop_training":
            if state["training"]["state"] != "started":
                raise CommandRejected("training_not_started", "Stop requires a live training run")
            if state["training"].get("trainingRunId") != command["arguments"]["trainingRunId"]:
                raise CommandRejected("training_run_stale", "Stop targets another training run")
            return {"exactTrainingRun": True, "graceSeconds": command["arguments"]["graceSeconds"]}
        if action == "cancel_training":
            if state["training"]["state"] not in {"not_started", "queued"}:
                raise CommandRejected("training_not_cancellable", "Cancel requires queued or not-started training")
            current_run = state["training"].get("trainingRunId")
            if current_run is not None and current_run != command["arguments"]["trainingRunId"]:
                raise CommandRejected("training_run_stale", "Cancel targets another training run")
            return {"exactTrainingRun": True, "trainingWasLive": False}
        if action == "repair_heartbeat":
            if state["training"]["state"] != "started":
                raise CommandRejected("training_not_started", "Heartbeat repair is a post-start action")
            if state["training"].get("trainingRunId") != command["arguments"]["trainingRunId"]:
                raise CommandRejected("training_run_stale", "Heartbeat repair targets another training run")
            return {"exactTrainingRun": True, "watchdogRevision": command["arguments"]["watchdogRevision"]}
        if action == "apply_configuration":
            current = state["configuration"].get("currentRevision")
            if current is not None and current != command["arguments"]["fromRevision"]:
                raise CommandRejected("configuration_revision_stale", "Configuration base revision is stale")
            if command["arguments"]["fromRevision"] == command["arguments"]["toRevision"]:
                raise CommandRejected("configuration_revision_unchanged", "Configuration must advance revision")
            return {"restartAllowed": False, "fromRevision": current}
        raise CommandRejected("action_not_allowed", "Command action is not allowlisted")

    def _journal_received(self, command: dict[str, Any], now: str) -> dict[str, Any]:
        def apply(state: dict[str, Any]) -> dict[str, Any]:
            existing = state["commands"].get(command["commandId"])
            if existing is not None:
                return {"claimed": False, "record": copy.deepcopy(existing)}
            last_issued_at = state.get("lastAcceptedIssuedAt")
            if last_issued_at and _parse_time(command["issuedAt"]) < _parse_time(last_issued_at):
                raise CommandRejected("command_reordered", "Command predates the last accepted command")
            cursor = state["lastAcknowledgementCursor"] + 1
            state["lastAcknowledgementCursor"] = cursor
            state["commandOrder"].append(command["commandId"])
            state["commands"][command["commandId"]] = {
                **copy.deepcopy(command),
                "status": "received",
                "receivedAt": now,
                "cursor": cursor,
            }
            state["lastAcceptedIssuedAt"] = command["issuedAt"]
            return {"claimed": True, "record": copy.deepcopy(state["commands"][command["commandId"]])}

        return self.store.transaction(apply)

    def _journal_executed(
        self,
        command: dict[str, Any],
        now: str,
        effect: Any,
        gate: dict[str, Any],
    ) -> dict[str, Any]:
        def apply(state: dict[str, Any]) -> dict[str, Any]:
            record = state["commands"][command["commandId"]]
            record.update({"status": "executed", "executedAt": now, "effect": effect, "gate": gate})
            self._apply_action_state(state, command, now)
            event_type = self._action_event_type(command.get("action"))
            if event_type is not None:
                self._queue_local_event(state, event_type, now)
            return {"status": "executed", "duplicate": False, "effect": effect, "gate": gate}

        return self.store.transaction(apply)

    @staticmethod
    def _action_event_type(action: str | None) -> str | None:
        return {
            "prepare_setup": "setup_ready",
            "start_training": "training_started",
            "stop_training": "training_stopped",
            "cancel_training": "training_cancelled",
        }.get(action)

    @staticmethod
    def _apply_action_state(state: dict[str, Any], command: dict[str, Any], observed_at: str) -> None:
        action = command["action"]
        arguments = command["arguments"]
        if action == "prepare_setup":
            state["setup"].update({"state": "ready", "readyAt": observed_at, "profileId": arguments["setupProfileId"]})
            state.setdefault("localEventMarkers", {})["setupReadyRevision"] = state[
                "identity"
            ]["setupRevision"]
        elif action == "start_training":
            state["training"].update({
                "state": "started", "startedAt": observed_at,
                "trainingRunId": arguments["trainingRunId"],
                "trainingPlanId": arguments["trainingPlanId"],
                "configurationRevision": arguments["configurationRevision"],
            })
        elif action == "stop_training":
            state["training"].update({"state": "stopped", "stoppedAt": observed_at})
        elif action == "cancel_training":
            state["training"].update({
                "state": "cancelled", "cancelledAt": observed_at,
                "trainingRunId": arguments["trainingRunId"],
            })
        elif action == "repair_heartbeat":
            state["lastHeartbeatRepairAt"] = observed_at
            state["watchdogRevision"] = arguments["watchdogRevision"]
        elif action == "apply_configuration":
            state["configuration"].update({
                "configurationId": arguments["configurationId"],
                "currentRevision": arguments["toRevision"],
                "settings": copy.deepcopy(arguments["settings"]),
                "appliedAt": observed_at,
                "restartRequested": False,
            })

    def _journal_executing(self, command: dict[str, Any], now: str, gate: dict[str, Any]) -> None:
        def apply(state: dict[str, Any]) -> None:
            state["commands"][command["commandId"]].update({
                "status": "executing",
                "executionStartedAt": now,
                "gate": gate,
            })

        self.store.transaction(apply)

    def _journal_rejected(self, command: dict[str, Any], now: str, error: CommandRejected) -> None:
        def apply(state: dict[str, Any]) -> None:
            state["commands"][command["commandId"]].update({
                "status": "rejected",
                "rejectedAt": now,
                "reasonCode": error.code,
                "reason": str(error),
            })

        self.store.transaction(apply)

    def _journal_unknown(self, command: dict[str, Any], now: str, error: Exception) -> None:
        def apply(state: dict[str, Any]) -> None:
            state["commands"][command["commandId"]].update({
                "status": "effect_unknown",
                "effectUnknownAt": now,
                "reasonCode": "command_effect_unknown",
                "reason": str(error)[:1_000],
            })

        self.store.transaction(apply)

    def _emit_material(self, event_type: str, observed_at: str) -> None:
        if self.telemetry_dispatcher is None:
            return
        payload = {"eventType": event_type, "observedAt": observed_at, "state": self.snapshot()}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        payload["notificationId"] = hashlib.sha256(encoded).hexdigest()

        def persist(state: dict[str, Any]) -> None:
            state["telemetryOutbox"].setdefault(payload["notificationId"], copy.deepcopy(payload))

        self.store.transaction(persist)
        self.telemetry_dispatcher.submit(payload)

    def _queue_local_event(
        self,
        state: dict[str, Any],
        event_type: str,
        observed_at: str,
        *,
        evidence: dict[str, Any] | None = None,
        source_event_id: str | None = None,
    ) -> dict[str, Any] | None:
        if self.event_sink is None:
            return None
        material = {
            "eventType": event_type,
            "occurredAt": observed_at,
            "identity": copy.deepcopy(state["identity"]),
            "trainingRunId": state["training"].get("trainingRunId"),
            "trainingSessionId": state["training"].get("trainingSessionId"),
            "evidence": copy.deepcopy(evidence or {}),
        }
        source_event_id = source_event_id or hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        outbox = state.setdefault("localEventOutbox", [])
        if isinstance(outbox, dict):
            outbox = list(outbox.values())
            state["localEventOutbox"] = outbox
        existing = next(
            (item for item in outbox if item.get("sourceEventId") == source_event_id),
            None,
        )
        if existing is not None:
            return copy.deepcopy(existing)
        source_sequence = int(state.get("nextLocalEventSequence") or 1)
        state["nextLocalEventSequence"] = source_sequence + 1
        payload = {
            **material,
            "sourceEventId": source_event_id,
            "sourceSequence": source_sequence,
        }
        outbox.append(copy.deepcopy(payload))
        return payload

    def _deliver_local_event(self, payload: dict[str, Any]) -> bool:
        try:
            self.event_sink(payload)
        except Exception as error:
            detail = str(error)[:500]

            def record_failure(current: dict[str, Any]) -> None:
                current.setdefault("eventSpoolFailures", []).append({
                    "eventType": payload["eventType"],
                    "occurredAt": payload["occurredAt"],
                    "error": detail,
                })
                current["eventSpoolFailures"] = current["eventSpoolFailures"][-20:]

            self.store.transaction(record_failure)
            return False

        def complete(current: dict[str, Any]) -> None:
            outbox = current.setdefault("localEventOutbox", [])
            if isinstance(outbox, dict):
                outbox = list(outbox.values())
            current["localEventOutbox"] = [
                item
                for item in outbox
                if not (
                    item.get("sourceEventId") == payload["sourceEventId"]
                    and item == payload
                )
            ]

        self.store.transaction(complete)
        return True

    def _record_telemetry_success(self, payload: dict[str, Any]) -> None:
        def apply(state: dict[str, Any]) -> None:
            state["telemetryOutbox"].pop(payload["notificationId"], None)

        self.store.transaction(apply)

    def _record_telemetry_failure(self, payload: dict[str, Any], error: Exception) -> None:
        def apply(state: dict[str, Any]) -> None:
            state["telemetryFailures"].append({
                "eventType": payload["eventType"],
                "observedAt": payload["observedAt"],
                "error": str(error)[:500],
            })
            state["telemetryFailures"] = state["telemetryFailures"][-20:]

        self.store.transaction(apply)
