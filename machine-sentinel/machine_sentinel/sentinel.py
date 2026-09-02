from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
import threading
from typing import Any, Callable

from .evidence import classify_agora_evidence
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
    ):
        self.store = store
        self.effects = effects
        self._execution_lock = threading.RLock()
        self.telemetry_sink = telemetry_sink
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

    def observe_training_process(self, *, running: bool, observed_at: str) -> dict[str, Any]:
        def apply(state: dict[str, Any]) -> dict[str, Any]:
            if not running:
                return {"accepted": True, "running": False, "state": state["training"]["state"]}
            if state["setup"]["state"] != "ready" or state["publicMapping"]["state"] != "ready":
                raise CommandRejected(
                    "training_observation_out_of_order",
                    "Observed training cannot become current before setup and public mapping are ready",
                )
            state["training"].update({
                "state": "started",
                "startedAt": state["training"].get("startedAt") or observed_at,
            })
            return {"accepted": True, "running": True, "state": "started"}

        return self.store.transaction(apply)

    def observe_logs(self, text: str, *, observed_at: str) -> dict[str, Any]:
        evidence = classify_agora_evidence(text)

        def apply(state: dict[str, Any]) -> dict[str, Any]:
            state["latestEvidence"] = {**evidence, "observedAt": observed_at}
            if evidence["joinState"] == "joined":
                state["protection"]["joined"] = True
                state["protection"]["joinedAt"] = state["protection"]["joinedAt"] or observed_at
            if evidence["rawError"]:
                error_id = hashlib.sha256(
                    f"{evidence['errorCategory']}\0{evidence['rawError']}".encode("utf-8")
                ).hexdigest()[:24]
                existing = state["errors"].get(error_id)
                if existing is None:
                    state["errors"][error_id] = {
                        "errorId": error_id,
                        "category": evidence["errorCategory"],
                        "rawError": evidence["rawError"],
                        "status": "active",
                        "firstObservedAt": observed_at,
                        "lastObservedAt": observed_at,
                        "acknowledgedAt": None,
                        "acknowledgedBy": None,
                        "restoredAt": None,
                    }
                else:
                    existing["lastObservedAt"] = observed_at
                    existing["status"] = "acknowledged" if existing["acknowledgedAt"] else "active"
                    existing["restoredAt"] = None
                state["activeErrorId"] = error_id
                state["latestEvidence"]["errorId"] = error_id
            elif state["activeErrorId"] is not None:
                active = state["errors"][state["activeErrorId"]]
                active["status"] = "restored"
                active["restoredAt"] = observed_at
                state["activeErrorId"] = None
            return copy.deepcopy(state["latestEvidence"])

        result = self.store.transaction(apply)
        self._emit_material("evidence_changed", observed_at)
        return result

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
    ) -> dict[str, Any]:
        def apply(state: dict[str, Any]) -> dict[str, Any]:
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
            }
            current = state["process"]
            protected = state["protection"]["joined"] or state["protection"]["warm"]
            for key in (
                "tmuxSessionId", "tmuxWindowId", "tmuxPaneId", "tmuxPanePid",
                "watchdogInstanceId", "retryContinuityToken",
            ):
                if protected and current.get(key) is not None and proposed[key] != current[key]:
                    raise CommandRejected(
                        "protected_process_identity_changed",
                        f"Protected machine {key} changed",
                    )
            state["process"] = proposed
            return copy.deepcopy(state["process"])

        result = self.store.transaction(apply)
        self._emit_material("process_evidence_changed", observed_at)
        return result

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
            return {"status": "executed", "duplicate": False, "reconciled": True}

        result = self.store.transaction(apply)
        self._emit_material("command_reconciled", observed_at)
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
            return {"status": "executed", "duplicate": False, "effect": effect, "gate": gate}

        return self.store.transaction(apply)

    @staticmethod
    def _apply_action_state(state: dict[str, Any], command: dict[str, Any], observed_at: str) -> None:
        action = command["action"]
        arguments = command["arguments"]
        if action == "prepare_setup":
            state["setup"].update({"state": "ready", "readyAt": observed_at, "profileId": arguments["setupProfileId"]})
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
