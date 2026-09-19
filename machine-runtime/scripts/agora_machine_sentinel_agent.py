#!/usr/bin/env python3
"""Run the deterministic Machine Sentinel against its authenticated private ingress."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from machine_sentinel import (
    EventSpool,
    JsonStateStore,
    MachineSentinel,
    SentinelCredentialError,
    SentinelIngressClient,
    SentinelObservationError,
    execute_committed_sequence_recovery,
    execute_ingress_payload,
    ingress_payload,
    initial_state,
)
from machine_sentinel.process_contract import (
    owned_process_probe_command,
    parse_owned_process_identity,
)

ALLOWED_EFFECT_SCRIPTS = {
    "prepare_setup": "sentinel-verify-setup.sh",
    "start_training": "sentinel-start-training.sh",
    "stop_training": "sentinel-stop-training.sh",
    "cancel_training": "sentinel-cancel-training.sh",
    "repair_heartbeat": "sentinel-repair-heartbeat.sh",
}
PROVISIONING_ORIGINS = frozenset({
    "autoscaler_provider_create",
    "drop_recovery",
    "other_non_retiring",
    "read_model_migration",
    "unknown_non_retiring",
    "warm_waiting",
})
LEGACY_NON_RETIRING_PROVISIONING_ORIGINS = frozenset({"zulip_opening"})
PENDING_OBSERVATION_KEY = "pendingSentinelObservation"
NON_EXPIRING_CREDENTIAL_MARKER = "never"
LOCAL_OBSERVE_INTERVAL_SECONDS = 3.0
REMOTE_REPORT_INTERVAL_SECONDS = 60.0


def active_log_metadata(root: Path) -> list[dict[str, Any]]:
    candidates = (
        ("progress.log", root / "progress.log"),
        ("server_gpu0.log", root / "logs" / "server_gpu0.log"),
        ("launcher-gpu0.log", root / "logs" / "launcher-gpu0.log"),
        ("launcher-active.log", root / "logs" / "launcher-active.log"),
    )
    result = []
    for name, path in candidates:
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        if path.is_file():
            result.append({
                "name": name,
                "inode": int(stat.st_ino),
                "sizeBytes": int(stat.st_size),
                "modifiedAtMs": int(stat.st_mtime_ns // 1_000_000),
            })
    return result


def incremental_log_records(
    store: JsonStateStore,
    root: Path,
    *,
    max_bytes_per_log: int = 256 * 1024,
) -> list[dict[str, Any]]:
    candidates = (
        ("progress.log", root / "progress.log"),
        ("server_gpu0.log", root / "logs" / "server_gpu0.log"),
        ("launcher-gpu0.log", root / "logs" / "launcher-gpu0.log"),
        ("launcher-active.log", root / "logs" / "launcher-active.log"),
    )
    checkpoints = store.read().get("logCheckpoints", {})
    records: list[dict[str, Any]] = []
    for name, path in candidates:
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        if not path.is_file():
            continue
        prior = checkpoints.get(name, {}) if isinstance(checkpoints, dict) else {}
        same_source = (
            prior.get("device") == int(stat.st_dev)
            and prior.get("inode") == int(stat.st_ino)
            and int(prior.get("offset") or 0) <= int(stat.st_size)
        )
        start = int(prior.get("offset") or 0) if same_source else 0
        rotation = int(prior.get("rotationGeneration") or 0) + (0 if same_source else 1)
        with path.open("rb") as handle:
            handle.seek(start)
            raw = handle.read(max_bytes_per_log)
        complete_end = raw.rfind(b"\n") + 1
        if complete_end <= 0:
            continue
        offset = start
        for encoded in raw[:complete_end].splitlines(keepends=True):
            end = offset + len(encoded)
            line = encoded.rstrip(b"\r\n").decode("utf-8", errors="replace")
            if line:
                records.append({
                    "text": line,
                    "source": {
                        "name": name,
                        "device": int(stat.st_dev),
                        "inode": int(stat.st_ino),
                        "rotationGeneration": rotation,
                        "byteStart": offset,
                        "byteEnd": end,
                    },
                })
            offset = end
        records.append({
            "checkpoint": {
                "name": name,
                "device": int(stat.st_dev),
                "inode": int(stat.st_ino),
                "rotationGeneration": rotation,
                "offset": start + complete_end,
            }
        })
    return records


def commit_log_checkpoint(store: JsonStateStore, checkpoint: dict[str, Any]) -> None:
    def apply(state: dict[str, Any]) -> None:
        state.setdefault("logCheckpoints", {})[checkpoint["name"]] = copy.deepcopy(
            checkpoint
        )

    store.transaction(apply)


def append_local_event(spool: EventSpool, payload: dict[str, Any]) -> dict[str, Any]:
    return spool.append(
        event_type=payload["eventType"],
        occurred_at=payload["occurredAt"],
        identity=payload["identity"],
        training_run_id=payload.get("trainingRunId"),
        training_session_id=payload.get("trainingSessionId"),
        evidence=payload.get("evidence"),
        source_event_id=payload.get("sourceEventId"),
    )


def history_ingress_payload(
    spool: EventSpool,
    *,
    identity: dict[str, Any],
    credential_kind: str,
) -> dict[str, Any] | None:
    pending = spool.pending(limit=100)
    if not pending["events"]:
        return None
    return {
        "schemaVersion": "agora.machine-sentinel-history-batch.v1",
        "credentialKind": credential_kind,
        "identity": copy.deepcopy(identity),
        "afterCursor": pending["afterCursor"],
        "events": pending["events"],
    }


def acknowledge_consolidated_history(
    spool: EventSpool,
    payload: dict[str, Any],
    result: dict[str, Any],
) -> None:
    history = payload.get("historyBatch")
    if history is None:
        return
    expected = [event["eventId"] for event in history["events"]]
    if result.get("acceptedEventIds") != expected:
        raise RuntimeError("Machine Sentinel history acknowledgement changed the exact batch")
    cursor = result.get("historyCursor")
    if not isinstance(cursor, str) or not cursor:
        raise RuntimeError("Machine Sentinel consolidated response has no history cursor")
    spool.acknowledge(expected, remote_cursor=cursor)


def iso_now() -> str:
    value = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")
    return value.removesuffix("+00:00") + "Z"


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def update_credential_env(path: Path, updates: dict[str, str]) -> None:
    current = parse_env_file(path)
    current.update(updates)
    lines = [f"{key}={current[key]}" for key in sorted(current) if current[key]]
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def ensure_machine_credential(
    url: str,
    identity: dict[str, Any],
    credential_path: Path,
    secrets: dict[str, str],
    timeout: float,
    bootstrap_token: str = "",
) -> dict[str, str]:
    machine_token = secrets.get("AGORA_SENTINEL_MACHINE_TOKEN", "")
    raw_expiry = secrets.get("AGORA_SENTINEL_MACHINE_EXPIRES_AT_MS", "")
    non_expiring = raw_expiry.strip().lower() == NON_EXPIRING_CREDENTIAL_MARKER
    expires_at = 0 if non_expiring else int(raw_expiry or 0)
    now_ms = int(time.time() * 1000)
    definitively_expired = bool(machine_token) and expires_at > 0 and expires_at <= now_ms
    needs_refresh = (
        bool(machine_token)
        and not non_expiring
        and (expires_at <= 0 or expires_at - now_ms <= 5 * 60 * 1000)
    )
    if not machine_token:
        if not bootstrap_token:
            raise RuntimeError("Machine Sentinel bootstrap credential is unavailable")
        result = SentinelIngressClient(url, bootstrap_token, timeout=timeout).exchange_bootstrap(identity)
    elif definitively_expired and bootstrap_token:
        result = SentinelIngressClient(url, bootstrap_token, timeout=timeout).exchange_bootstrap(identity)
    elif needs_refresh:
        try:
            result = SentinelIngressClient(url, machine_token, timeout=timeout).refresh_machine_credential(identity)
        except SentinelCredentialError as error:
            if error.status not in {401, 403} or not bootstrap_token:
                raise
            result = SentinelIngressClient(url, bootstrap_token, timeout=timeout).exchange_bootstrap(identity)
    else:
        return secrets
    updated = {
        **secrets,
        "AGORA_SENTINEL_MACHINE_TOKEN": result["principalToken"],
        "AGORA_SENTINEL_MACHINE_EXPIRES_AT_MS": (
            NON_EXPIRING_CREDENTIAL_MARKER
            if result["expiresAt"] is None
            else str(result["expiresAt"])
        ),
    }
    updated.pop("AGORA_SENTINEL_BOOTSTRAP_TOKEN", None)
    update_credential_env(credential_path, updated)
    return updated


def ensure_runtime_secret_bundle(
    client: SentinelIngressClient,
    identity: dict[str, Any],
    token_label: str,
    credential_path: Path,
    secrets: dict[str, str],
) -> dict[str, str]:
    if secrets.get("AGORA_SENTINEL_RUNTIME_TOKEN_LABEL") == token_label and secrets.get("HF_TOKEN") and secrets.get("AGORA_MACHINE_SIGNING_SECRET"):
        return secrets
    bundle = client.machine_runtime_bundle(identity, token_label)
    if bundle["authorityEpoch"] != int(identity.get("authorityEpoch") or bundle["authorityEpoch"]):
        raise RuntimeError("Machine Sentinel runtime bundle authority epoch is stale")
    updated = {
        **secrets,
        "AGORA_SENTINEL_RUNTIME_TOKEN_LABEL": bundle["tokenLabel"],
        "HF_TOKEN": bundle["huggingFaceToken"],
        "AGORA_MACHINE_SIGNING_SECRET": bundle["machineSigningSecret"],
        "AGORA_SENTINEL_RUNTIME_AUTHORITY_EPOCH": str(bundle["authorityEpoch"]),
        "AGORA_SENTINEL_CREDENTIAL_GENERATION": str(bundle["credentialGeneration"]),
    }
    update_credential_env(credential_path, updated)
    return updated


def load_identity(path: Path) -> dict[str, Any]:
    identity = json.loads(path.read_text(encoding="utf-8"))
    if identity.get("bootId") == "__HOST_BOOT_ID__":
        identity["bootId"] = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    return identity


def _run_effect(root: Path, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if action == "apply_configuration":
        settings = arguments["settings"]
        updates: dict[str, str] = {}
        if "announcePort" in settings:
            updates["ANNOUNCE_PORT"] = str(settings["announcePort"])
        if "nodeType" in settings:
            updates["AGORA_NODE_TYPE"] = settings["nodeType"]
        update_credential_env(root / "agora.env", updates)
        return {"returncode": 0, "action": action, "restartRequested": False}
    script_name = ALLOWED_EFFECT_SCRIPTS.get(action)
    if script_name is None:
        raise RuntimeError("Machine Sentinel action is not allowlisted")
    script = root / script_name
    if not script.is_file() or not os.access(script, os.X_OK):
        raise RuntimeError(f"Machine Sentinel {action} effect is structurally unavailable")
    ordered_fields = {
        "prepare_setup": ("setupProfileId", "setupRevision"),
        "start_training": ("trainingRunId", "trainingPlanId", "configurationRevision", "announcePort"),
        "stop_training": ("trainingRunId", "graceSeconds", "reasonCode", "approvalReceiptId"),
        "cancel_training": ("trainingRunId", "reasonCode", "approvalReceiptId"),
        "repair_heartbeat": ("trainingRunId", "watchdogRevision"),
    }
    command = [str(script), *(str(arguments[field]) for field in ordered_fields[action])]
    completed = subprocess.run(command, text=True, capture_output=True, timeout=600, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"Machine Sentinel {action} effect failed with exit {completed.returncode}")
    return {"returncode": 0, "action": action}


def effect_handlers(root: Path) -> dict[str, Any]:
    return {
        action: (lambda arguments, action=action: _run_effect(root, action, arguments))
        for action in (*ALLOWED_EFFECT_SCRIPTS, "apply_configuration")
    }


def initial_runtime_state(
    identity: dict[str, Any],
    root: Path,
    setup_revision: str,
    authority_epoch: int,
    provisioning_origin: str,
) -> dict[str, Any]:
    if provisioning_origin not in PROVISIONING_ORIGINS:
        raise ValueError("Machine Sentinel provisioning origin is invalid")
    state = initial_state(
        reservation_id=identity["reservationId"],
        slot_generation=identity["slotGeneration"],
        machine_id=identity["machineId"],
        boot_id=identity["bootId"],
        setup_revision=setup_revision,
        authority_epoch=authority_epoch,
    )
    for field in (
        "machineGenerationId",
        "provider",
        "accountScope",
        "providerResourceId",
    ):
        if identity.get(field) is not None:
            state["identity"][field] = identity[field]
    state["provisioningOrigin"] = provisioning_origin
    if (root / "launch-agora-gpu0.sh").is_file() and (root / "watchdog-agora-tmux.sh").is_file():
        state["setup"].update({"state": "ready", "readyAt": iso_now()})
    return state


def reconcile_runtime_identity(
    store: JsonStateStore,
    identity: dict[str, Any],
    *,
    setup_revision: str,
    authority_epoch: int,
    observed_at: str,
) -> bool:
    def apply(state: dict[str, Any]) -> bool:
        current = state["identity"]
        expected = {
            "reservationId": identity["reservationId"],
            "slotGeneration": identity["slotGeneration"],
            "machineId": identity["machineId"],
        }
        for field, value in expected.items():
            if current.get(field) != value:
                raise RuntimeError(
                    f"Machine Sentinel persisted {field} does not match the installed identity"
                )
        identity_hydrated = False
        for field in (
            "machineGenerationId",
            "provider",
            "accountScope",
            "providerResourceId",
        ):
            installed = identity.get(field)
            persisted = current.get(field)
            if installed is None:
                continue
            if persisted is None:
                current[field] = installed
                identity_hydrated = True
            elif persisted != installed:
                raise RuntimeError(
                    f"Machine Sentinel persisted {field} does not match the installed identity"
                )
        previous_boot_id = current.get("bootId")
        boot_changed = previous_boot_id != identity["bootId"]
        lifecycle_changed = (
            boot_changed
            or current.get("setupRevision") != setup_revision
            or current.get("authorityEpoch") != authority_epoch
        )
        changed = identity_hydrated or lifecycle_changed
        if not changed:
            return False
        state.setdefault("identityTransitions", []).append({
            "observedAt": observed_at,
            "previousBootId": previous_boot_id,
            "bootId": identity["bootId"],
            "previousSetupRevision": current.get("setupRevision"),
            "setupRevision": setup_revision,
        })
        state["identityTransitions"] = state["identityTransitions"][-20:]
        current.update({
            "bootId": identity["bootId"],
            "setupRevision": setup_revision,
            "authorityEpoch": authority_epoch,
        })
        if boot_changed:
            process = state.get("process")
            if isinstance(process, dict) and process.get("bootId") is None:
                process["bootId"] = previous_boot_id
            state["logObservationSession"] = None
            state["sentinelSequence"] = 0
        if lifecycle_changed:
            state["lastServerCommandCursor"] = 0
            state["lastAcceptedIssuedAt"] = None
            state.pop(PENDING_OBSERVATION_KEY, None)
        return True

    return store.transaction(apply)


def normalize_provisioning_origin(value: str) -> str:
    if value in PROVISIONING_ORIGINS:
        return value
    if value in LEGACY_NON_RETIRING_PROVISIONING_ORIGINS:
        return "other_non_retiring"
    raise ValueError("Machine Sentinel provisioning origin is invalid")


def refresh_local_evidence(sentinel: MachineSentinel, root: Path, observed_at: str, *, runner=subprocess.run) -> None:
    env = parse_env_file(root / "agora.env")
    announce_port = env.get("ANNOUNCE_PORT", "")
    if announce_port.isdigit() and int(announce_port) > 0:
        identity = sentinel.snapshot()["identity"]
        sentinel.observe_public_mapping(
            reservation_id=identity["reservationId"],
            slot_generation=identity["slotGeneration"],
            internal_port=int(env.get("HOST_PORT") or 49200),
            external_port=int(announce_port),
            mapping_generation=f"{identity['machineId']}:{announce_port}",
            observed_at=observed_at,
        )
    session = runner(
        ["tmux", "display-message", "-p", "-t", "agora_gpu", "#{session_id}\t#{window_id}\t#{pane_id}\t#{pane_pid}\t#{session_created}"],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    fields = session.stdout.strip().split("\t") if session.returncode == 0 else []
    session_observed = len(fields) in {4, 5}
    owned_process_identity = None
    if session_observed and fields[3].isdigit():
        owned_process = runner(
            owned_process_probe_command(root, int(fields[3])),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        owned_process_identity = (
            parse_owned_process_identity(owned_process.stdout)
            if owned_process.returncode == 0
            else None
        )
    owned_process_verified_running = owned_process_identity is not None
    watchdog = runner(
        ["pgrep", "-f", str(root / "watch-agora-tmux-loop.sh")],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    training_session_id = None
    if session_observed:
        training_session_id = hashlib.sha256(
            f"{sentinel.snapshot()['identity']['bootId']}\0{fields[0]}\0{fields[4] if len(fields) == 5 else fields[3]}".encode()
        ).hexdigest()[:32]
    before_process = sentinel.snapshot()
    training_session_changed = (
        training_session_id is not None
        and training_session_id
        != before_process["training"].get("trainingSessionId")
    )
    bind_training_before_process = owned_process_verified_running and (
        before_process["process"].get("tmuxAgora") is not True
        or before_process["process"].get("bootId")
        != before_process["identity"].get("bootId")
        or training_session_changed
    )
    if bind_training_before_process:
        sentinel.observe_training_process(
            running=True,
            observed_at=observed_at,
            training_run_id=env.get("AGORA_TRAINING_RUN_ID") or None,
            training_session_id=training_session_id,
            training_plan_id=env.get("AGORA_TRAINING_PLAN_ID") or None,
            configuration_revision=env.get("AGORA_CONFIGURATION_REVISION") or None,
        )
    sentinel.observe_passive_process_evidence(
        tmux_agora=session_observed,
        watchdog=watchdog.returncode == 0,
        observed_at=observed_at,
        tmux_session_id=fields[0] if session_observed else None,
        tmux_window_id=fields[1] if session_observed else None,
        tmux_pane_id=fields[2] if session_observed else None,
        tmux_pane_pid=int(fields[3]) if session_observed and fields[3].isdigit() else None,
        watchdog_instance_id=watchdog.stdout.splitlines()[0].strip() if watchdog.returncode == 0 and watchdog.stdout.strip() else None,
        retry_continuity_token=str((root / "watchdog-agora-tmux.sh").stat().st_mtime_ns) if (root / "watchdog-agora-tmux.sh").exists() else None,
        owned_process_verified_running=owned_process_verified_running,
        owned_process_identity=owned_process_identity,
    )
    sentinel.observe_active_logs(active_log_metadata(root), observed_at=observed_at)
    if not bind_training_before_process:
        sentinel.observe_training_process(
            running=owned_process_verified_running,
            observed_at=observed_at,
            training_run_id=env.get("AGORA_TRAINING_RUN_ID") or None,
            training_session_id=training_session_id,
            training_plan_id=env.get("AGORA_TRAINING_PLAN_ID") or None,
            configuration_revision=env.get("AGORA_CONFIGURATION_REVISION") or None,
        )
    for record in incremental_log_records(sentinel.store, root):
        checkpoint = record.get("checkpoint")
        if checkpoint is not None:
            commit_log_checkpoint(sentinel.store, checkpoint)
            continue
        sentinel.observe_logs(
            record["text"],
            observed_at=observed_at,
            source=record["source"],
        )


def reconcile_local_commands(
    sentinel: MachineSentinel,
    root: Path,
    observed_at: str,
    *,
    runner=subprocess.run,
) -> list[dict[str, Any]]:
    snapshot = sentinel.snapshot()
    env = parse_env_file(root / "agora.env")
    outcomes = []
    for record in sentinel.pending_commands():
        if record["status"] != "effect_unknown":
            continue
        action = record["action"]
        arguments = record["arguments"]
        if action == "prepare_setup":
            observed = all((root / name).is_file() for name in ("launch-agora-gpu0.sh", "watchdog-agora-tmux.sh", "agora.env"))
        elif action == "start_training":
            observed = bool(snapshot.get("process", {}).get("tmuxAgora"))
        elif action in {"stop_training", "cancel_training"}:
            observed = snapshot.get("process", {}).get("tmuxAgora") is False
        elif action == "repair_heartbeat":
            probe = runner(["tmux", "has-session", "-t", "agora_heartbeat"], text=True, capture_output=True, timeout=5, check=False)
            observed = probe.returncode == 0
        elif action == "apply_configuration":
            settings = arguments["settings"]
            observed = (
                ("announcePort" not in settings or env.get("ANNOUNCE_PORT") == str(settings["announcePort"]))
                and ("nodeType" not in settings or env.get("AGORA_NODE_TYPE") == settings["nodeType"])
            )
        else:
            observed = False
        outcomes.append(sentinel.reconcile_command(record["commandId"], effect_observed=observed, observed_at=observed_at))
    return outcomes


def choose_credential(state: dict[str, Any], bootstrap_token: str, machine_token: str) -> tuple[str, str]:
    training = state.get("training", {})
    training_started = bool(training.get("startedAt")) or training.get("state") in {"started", "stopped"}
    if training_started:
        if not machine_token:
            raise RuntimeError("Machine Sentinel machine credential is unavailable after training starts")
        return "machine", machine_token
    if bootstrap_token:
        return "bootstrap", bootstrap_token
    if machine_token:
        return "machine", machine_token
    raise RuntimeError("Machine Sentinel credential is unavailable")


def load_pending_observation(store: JsonStateStore) -> dict[str, Any] | None:
    record = store.read().get(PENDING_OBSERVATION_KEY)
    if record is None:
        return None
    if not isinstance(record, dict) or record.get("schemaVersion") != 1:
        raise RuntimeError("Machine Sentinel pending observation record is invalid")
    payload = record.get("payload")
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("sequence"), int)
        or isinstance(payload.get("sequence"), bool)
        or payload["sequence"] < 1
        or not isinstance(payload.get("sentAt"), str)
        or payload.get("credentialKind") not in {"bootstrap", "machine"}
    ):
        raise RuntimeError("Machine Sentinel pending observation payload is invalid")
    return copy.deepcopy(record)


def persist_pending_observation(
    store: JsonStateStore,
    payload: dict[str, Any],
    *,
    prepared_at: str,
) -> dict[str, Any]:
    frozen = copy.deepcopy(payload)

    def apply(state: dict[str, Any]) -> dict[str, Any]:
        existing = state.get(PENDING_OBSERVATION_KEY)
        if existing is not None:
            if existing.get("payload") != frozen:
                raise RuntimeError("Machine Sentinel pending observation conflicts with the frozen payload")
            return existing
        record = {
            "schemaVersion": 1,
            "payload": frozen,
            "preparedAt": prepared_at,
            "attemptCount": 0,
            "lastAttemptAt": None,
        }
        state[PENDING_OBSERVATION_KEY] = record
        return record

    return store.transaction(apply)


def record_pending_attempt(store: JsonStateStore, payload: dict[str, Any], *, attempted_at: str) -> int:
    frozen = copy.deepcopy(payload)

    def apply(state: dict[str, Any]) -> int:
        record = state.get(PENDING_OBSERVATION_KEY)
        if not isinstance(record, dict) or record.get("payload") != frozen:
            raise RuntimeError("Machine Sentinel pending observation changed before send")
        record["attemptCount"] = int(record.get("attemptCount", 0)) + 1
        record["lastAttemptAt"] = attempted_at
        return record["attemptCount"]

    return store.transaction(apply)


def rebuild_pending_observation(
    store: JsonStateStore,
    previous_payload: dict[str, Any],
    replacement_payload: dict[str, Any],
    *,
    prepared_at: str,
) -> dict[str, Any]:
    previous = copy.deepcopy(previous_payload)
    replacement = copy.deepcopy(replacement_payload)
    if replacement.get("sequence") != previous.get("sequence"):
        raise RuntimeError("Machine Sentinel stale observation rebuild changed sequence")
    if replacement.get("sentAt") == previous.get("sentAt"):
        raise RuntimeError("Machine Sentinel stale observation rebuild did not refresh sentAt")

    def apply(state: dict[str, Any]) -> dict[str, Any]:
        record = state.get(PENDING_OBSERVATION_KEY)
        if not isinstance(record, dict) or record.get("payload") != previous:
            raise RuntimeError("Machine Sentinel pending observation changed before rebuild")
        rebuilt = {
            "schemaVersion": 1,
            "payload": replacement,
            "preparedAt": prepared_at,
            "attemptCount": int(record.get("attemptCount", 0)),
            "lastAttemptAt": record.get("lastAttemptAt"),
            "rebuildCount": int(record.get("rebuildCount", 0)) + 1,
        }
        state[PENDING_OBSERVATION_KEY] = rebuilt
        return rebuilt

    return store.transaction(apply)


def complete_pending_observation(
    store: JsonStateStore,
    payload: dict[str, Any],
    result: dict[str, Any],
) -> None:
    frozen = copy.deepcopy(payload)

    def apply(state: dict[str, Any]) -> None:
        record = state.get(PENDING_OBSERVATION_KEY)
        if not isinstance(record, dict) or record.get("payload") != frozen:
            raise RuntimeError("Machine Sentinel pending observation changed before completion")
        command_cursor = result.get("commandCursor")
        state["sentinelSequence"] = frozen["sequence"]
        if command_cursor is not None:
            state["lastServerCommandCursor"] = int(command_cursor)
        else:
            state["lastServerCommandCursor"] = int(state.get("lastServerCommandCursor", 0))
        del state[PENDING_OBSERVATION_KEY]

    store.transaction(apply)


def retry_log(
    error: SentinelObservationError,
    payload: dict[str, Any],
    attempt: int,
    secrets: dict[str, str],
    *,
    state: str,
) -> str:
    detail = str(error)
    for key, value in secrets.items():
        if value and ("TOKEN" in key or "SECRET" in key):
            detail = detail.replace(value, "[REDACTED]")
    return json.dumps({
        "attempt": attempt,
        "detail": detail[:240],
        "reason": error.reason or ("transport_error" if error.status is None else "http_error"),
        "sentAt": payload["sentAt"],
        "sequence": payload["sequence"],
        "state": state,
        "status": error.status,
    }, sort_keys=True)


def should_emit_retry_log(attempt: int) -> bool:
    return attempt == 1 or (attempt > 0 and attempt & (attempt - 1) == 0)


def retry_backoff_seconds(error: SentinelObservationError, attempt: int) -> float:
    if error.blocked_sequence_conflict:
        return float(min(60, 15 * (2 ** min(max(0, attempt - 1), 3))))
    return 5.0


def can_recover_committed_sequence(
    error: SentinelObservationError,
    payload: dict[str, Any],
) -> bool:
    return error.recoverable_committed_sequence and payload.get("credentialKind") == "machine"


def credential_retry_metadata(
    error: Exception,
    secrets: dict[str, str],
    bootstrap_token: str,
) -> dict[str, Any]:
    if not isinstance(error, SentinelCredentialError):
        return {"status": None, "reason": "credential_error"}
    reason = error.reason or ("transport_error" if error.status is None else "http_error")
    sensitive_values = [
        value
        for key, value in secrets.items()
        if value and ("TOKEN" in key or "SECRET" in key)
    ]
    if bootstrap_token:
        sensitive_values.append(bootstrap_token)
    for value in sensitive_values:
        reason = reason.replace(value, "[REDACTED]")
    return {"status": error.status, "reason": reason[:120]}


def main() -> int:
    bootstrap_token = os.environ.pop("AGORA_SENTINEL_BOOTSTRAP_TOKEN", "")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="")
    parser.add_argument("--export-mode", choices=("local", "remote", "legacy"), default="legacy")
    parser.add_argument("--identity-file", required=True)
    parser.add_argument("--credential-env-file", required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--setup-revision", required=True)
    parser.add_argument(
        "--provisioning-origin",
        choices=sorted(PROVISIONING_ORIGINS | LEGACY_NON_RETIRING_PROVISIONING_ORIGINS),
        required=True,
    )
    parser.add_argument("--authority-epoch", type=int, required=True)
    # Retain the legacy flag so already-installed launchers continue to work.
    # It is deliberately ignored: Sentinel cadence no longer depends on fleet
    # size, and newly generated observations omit that field.
    parser.add_argument("--fleet-size", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.export_mode == "local":
        args.url = ""
        bootstrap_token = ""
    elif args.export_mode == "remote" and not args.url:
        raise ValueError("Machine Sentinel remote export requires an ingress URL")

    root = Path(args.root)
    identity = load_identity(Path(args.identity_file))
    state_path = Path(args.state_file)
    store = JsonStateStore(
        state_path,
        initial_runtime_state(
            identity,
            root,
            args.setup_revision,
            args.authority_epoch,
            normalize_provisioning_origin(args.provisioning_origin),
        ) if not state_path.exists() else None,
    )
    reconcile_runtime_identity(
        store,
        identity,
        setup_revision=args.setup_revision,
        authority_epoch=args.authority_epoch,
        observed_at=iso_now(),
    )
    spool = EventSpool(
        state_path.with_name("events.jsonl"),
        state_path.with_name("events.cursor.json"),
    )
    sentinel = MachineSentinel(
        store,
        effects=effect_handlers(root),
        event_sink=lambda payload: append_local_event(spool, payload),
    )
    sentinel.retry_local_events()
    sentinel.record_started(observed_at=iso_now())
    sentinel.recover_interrupted_commands(observed_at=iso_now())
    credential_path = Path(args.credential_env_file)
    secrets = parse_env_file(credential_path)
    once_rebuild_used = False
    credential_exchange_failure_count = 0
    runtime_secret_failure_count = 0
    local_due = 0.0
    remote_due = 0.0
    while True:
        monotonic_now = time.monotonic()
        if monotonic_now >= local_due:
            observed_at = iso_now()
            refresh_local_evidence(sentinel, root, observed_at)
            reconcile_local_commands(sentinel, root, observed_at)
            local_due = monotonic_now + LOCAL_OBSERVE_INTERVAL_SECONDS
        else:
            observed_at = iso_now()
        if not args.url:
            if args.once:
                return 0
            time.sleep(max(0.01, local_due - time.monotonic()))
            continue
        if monotonic_now < remote_due:
            time.sleep(max(0.01, min(local_due, remote_due) - time.monotonic()))
            continue
        try:
            secrets = ensure_machine_credential(
                args.url,
                identity,
                credential_path,
                secrets,
                args.timeout,
                bootstrap_token,
            )
        except Exception as error:
            credential_exchange_failure_count += 1
            if args.once:
                raise
            pending = load_pending_observation(store)
            if should_emit_retry_log(credential_exchange_failure_count):
                failure = credential_retry_metadata(error, secrets, bootstrap_token)
                print(json.dumps({
                    "attempt": credential_exchange_failure_count,
                    "reason": failure["reason"],
                    "sentAt": observed_at,
                    "sequence": pending["payload"]["sequence"] if pending else int(sentinel.snapshot().get("sentinelSequence", 0)) + 1,
                    "state": "credential_exchange_unavailable",
                    "status": failure["status"],
                }, sort_keys=True), flush=True)
            remote_due = time.monotonic() + 5.0
            continue
        if credential_exchange_failure_count:
            print(json.dumps({
                "attempts": credential_exchange_failure_count,
                "sentAt": observed_at,
                "state": "credential_exchange_recovered",
            }, sort_keys=True), flush=True)
            credential_exchange_failure_count = 0
        if secrets.get("AGORA_SENTINEL_MACHINE_TOKEN"):
            bootstrap_token = ""
        pending = load_pending_observation(store)
        if pending is None:
            credential_kind, token = choose_credential(
                sentinel.snapshot(),
                secrets.get("AGORA_SENTINEL_BOOTSTRAP_TOKEN", ""),
                secrets.get("AGORA_SENTINEL_MACHINE_TOKEN", ""),
            )
        else:
            credential_kind = pending["payload"]["credentialKind"]
            token = (
                secrets.get("AGORA_SENTINEL_MACHINE_TOKEN", "")
                if credential_kind == "machine"
                else secrets.get("AGORA_SENTINEL_BOOTSTRAP_TOKEN", "") or bootstrap_token
            )
            if not token:
                raise RuntimeError(f"Machine Sentinel {credential_kind} credential is unavailable for pending observation")
        if credential_kind == "machine" and identity.get("tokenLabel"):
            machine_client = SentinelIngressClient(args.url, token, timeout=args.timeout)
            try:
                secrets = ensure_runtime_secret_bundle(
                    machine_client,
                    identity,
                    str(identity["tokenLabel"]),
                    credential_path,
                    secrets,
                )
            except SentinelCredentialError as error:
                if not error.retryable or args.once:
                    raise
                runtime_secret_failure_count += 1
                if should_emit_retry_log(runtime_secret_failure_count):
                    current_pending = load_pending_observation(store)
                    print(json.dumps({
                        "attempt": runtime_secret_failure_count,
                        "reason": error.reason or ("transport_error" if error.status is None else "http_error"),
                        "sentAt": observed_at,
                        "sequence": current_pending["payload"]["sequence"] if current_pending else int(sentinel.snapshot().get("sentinelSequence", 0)) + 1,
                        "state": "runtime_secret_retry_pending",
                        "status": error.status,
                    }, sort_keys=True), flush=True)
                remote_due = time.monotonic() + 5.0
                continue
            if runtime_secret_failure_count:
                print(json.dumps({
                    "attempts": runtime_secret_failure_count,
                    "sentAt": observed_at,
                    "state": "runtime_secret_recovered",
                }, sort_keys=True), flush=True)
                runtime_secret_failure_count = 0
        if pending is None:
            sequence = int(sentinel.snapshot().get("sentinelSequence", 0)) + 1
            history = history_ingress_payload(
                spool,
                identity=identity,
                credential_kind=credential_kind,
            )
            payload = ingress_payload(
                sentinel,
                identity=identity,
                credential_kind=credential_kind,
                sequence=sequence,
                sent_at=observed_at,
                launch_active=sentinel.snapshot()["training"]["state"] != "started",
                history_batch=(
                    {
                        "afterCursor": history["afterCursor"],
                        "events": history["events"],
                    }
                    if history is not None
                    else None
                ),
            )
            pending = persist_pending_observation(store, payload, prepared_at=observed_at)
        payload = pending["payload"]
        attempt = record_pending_attempt(store, payload, attempted_at=iso_now())
        try:
            result = execute_ingress_payload(
                sentinel,
                SentinelIngressClient(args.url, token, timeout=args.timeout),
                payload,
                command_observed_at=iso_now(),
            )
        except SentinelObservationError as error:
            if can_recover_committed_sequence(error, payload):
                try:
                    result = execute_committed_sequence_recovery(
                        sentinel,
                        SentinelIngressClient(args.url, token, timeout=args.timeout),
                        identity=identity,
                        sequence=payload["sequence"],
                        command_observed_at=iso_now(),
                    )
                except SentinelObservationError as recovery_error:
                    if recovery_error.retryable or recovery_error.blocked_sequence_conflict:
                        if should_emit_retry_log(attempt):
                            print(retry_log(
                                recovery_error,
                                payload,
                                attempt,
                                secrets,
                                state="committed_sequence_recovery_pending",
                            ), flush=True)
                        if args.once:
                            raise
                        remote_due = time.monotonic() + retry_backoff_seconds(
                            recovery_error, attempt
                        )
                        continue
                    raise
                print(json.dumps({
                    "sentAt": payload["sentAt"],
                    "sequence": payload["sequence"],
                    "state": "committed_sequence_recovered",
                }, sort_keys=True), flush=True)
            elif error.rebuildable_stale_no_cache:
                if args.once and once_rebuild_used:
                    print(retry_log(error, payload, attempt, secrets, state="stale_rebuild_exhausted"), flush=True)
                    raise
                replacement_sent_at = iso_now()
                replacement = ingress_payload(
                    sentinel,
                    identity=identity,
                    credential_kind=payload["credentialKind"],
                    sequence=payload["sequence"],
                    sent_at=replacement_sent_at,
                    launch_active=sentinel.snapshot()["training"]["state"] != "started",
                    history_batch=copy.deepcopy(payload.get("historyBatch")),
                )
                rebuild_pending_observation(
                    store,
                    payload,
                    replacement,
                    prepared_at=replacement_sent_at,
                )
                once_rebuild_used = True
                if should_emit_retry_log(attempt):
                    print(retry_log(error, payload, attempt, secrets, state="stale_rebuilt"), flush=True)
                if not args.once:
                    remote_due = time.monotonic() + 5.0
                continue
            elif error.retryable or error.blocked_sequence_conflict:
                if should_emit_retry_log(attempt):
                    retry_state = "sequence_conflict_pending" if error.blocked_sequence_conflict else "observation_retry_pending"
                    print(retry_log(error, payload, attempt, secrets, state=retry_state), flush=True)
                if args.once:
                    raise
                remote_due = time.monotonic() + retry_backoff_seconds(error, attempt)
                continue
            else:
                raise
        acknowledge_consolidated_history(spool, payload, result)
        complete_pending_observation(store, payload, result)
        if attempt > 1:
            print(json.dumps({
                "attempts": attempt,
                "sentAt": payload["sentAt"],
                "sequence": payload["sequence"],
                "state": "observation_recovered",
            }, sort_keys=True), flush=True)
        print(json.dumps({"sentAt": payload["sentAt"], "sequence": payload["sequence"], "result": result}, sort_keys=True), flush=True)
        if args.once:
            return 0
        once_rebuild_used = False
        remote_due = time.monotonic() + max(
            REMOTE_REPORT_INTERVAL_SECONDS,
            float(result.get("nextPollSeconds") or REMOTE_REPORT_INTERVAL_SECONDS),
        )


if __name__ == "__main__":
    raise SystemExit(main())
