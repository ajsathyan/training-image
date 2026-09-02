#!/usr/bin/env python3
"""Run the deterministic Machine Sentinel against its authenticated private ingress."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from machine_sentinel import (
    JsonStateStore,
    MachineSentinel,
    SentinelIngressClient,
    execute_ingress_cycle,
    initial_state,
)

ALLOWED_EFFECT_SCRIPTS = {
    "prepare_setup": "sentinel-verify-setup.sh",
    "start_training": "sentinel-start-training.sh",
    "stop_training": "sentinel-stop-training.sh",
    "cancel_training": "sentinel-cancel-training.sh",
    "repair_heartbeat": "sentinel-repair-heartbeat.sh",
}


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


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
    expires_at = int(secrets.get("AGORA_SENTINEL_MACHINE_EXPIRES_AT_MS") or 0)
    needs_refresh = bool(machine_token) and (expires_at <= 0 or expires_at - int(time.time() * 1000) <= 5 * 60 * 1000)
    if not machine_token:
        if not bootstrap_token:
            raise RuntimeError("Machine Sentinel bootstrap credential is unavailable")
        result = SentinelIngressClient(url, bootstrap_token, timeout=timeout).exchange_bootstrap(identity)
    elif needs_refresh:
        result = SentinelIngressClient(url, machine_token, timeout=timeout).refresh_machine_credential(identity)
    else:
        return secrets
    updated = {
        **secrets,
        "AGORA_SENTINEL_MACHINE_TOKEN": result["principalToken"],
        "AGORA_SENTINEL_MACHINE_EXPIRES_AT_MS": str(result["expiresAt"]),
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


def initial_runtime_state(identity: dict[str, Any], root: Path, setup_revision: str, authority_epoch: int) -> dict[str, Any]:
    state = initial_state(
        reservation_id=identity["reservationId"],
        slot_generation=identity["slotGeneration"],
        machine_id=identity["machineId"],
        boot_id=identity["bootId"],
        setup_revision=setup_revision,
        authority_epoch=authority_epoch,
    )
    if (root / "launch-agora-gpu0.sh").is_file() and (root / "watchdog-agora-tmux.sh").is_file():
        state["setup"].update({"state": "ready", "readyAt": iso_now()})
    return state


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
        ["tmux", "display-message", "-p", "-t", "agora_gpu", "#{session_id}\t#{window_id}\t#{pane_id}\t#{pane_pid}"],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    fields = session.stdout.strip().split("\t") if session.returncode == 0 else []
    watchdog = runner(
        ["pgrep", "-f", str(root / "watch-agora-tmux-loop.sh")],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    sentinel.observe_process_evidence(
        tmux_agora=len(fields) == 4,
        watchdog=watchdog.returncode == 0,
        observed_at=observed_at,
        tmux_session_id=fields[0] if len(fields) == 4 else None,
        tmux_window_id=fields[1] if len(fields) == 4 else None,
        tmux_pane_id=fields[2] if len(fields) == 4 else None,
        tmux_pane_pid=int(fields[3]) if len(fields) == 4 and fields[3].isdigit() else None,
        watchdog_instance_id=watchdog.stdout.splitlines()[0].strip() if watchdog.returncode == 0 and watchdog.stdout.strip() else None,
        retry_continuity_token=str((root / "watchdog-agora-tmux.sh").stat().st_mtime_ns) if (root / "watchdog-agora-tmux.sh").exists() else None,
    )
    sentinel.observe_training_process(running=len(fields) == 4, observed_at=observed_at)
    log_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")[-128 * 1024:]
        for path in (root / "progress.log", root / "logs" / "server_gpu0.log", root / "logs" / "launcher-gpu0.log")
        if path.is_file()
    )
    if log_text:
        sentinel.observe_logs(log_text, observed_at=observed_at)


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


def main() -> int:
    bootstrap_token = os.environ.pop("AGORA_SENTINEL_BOOTSTRAP_TOKEN", "")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--identity-file", required=True)
    parser.add_argument("--credential-env-file", required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--setup-revision", required=True)
    parser.add_argument("--authority-epoch", type=int, required=True)
    parser.add_argument("--fleet-size", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    root = Path(args.root)
    identity = load_identity(Path(args.identity_file))
    state_path = Path(args.state_file)
    store = JsonStateStore(
        state_path,
        initial_runtime_state(identity, root, args.setup_revision, args.authority_epoch) if not state_path.exists() else None,
    )
    sentinel = MachineSentinel(store, effects=effect_handlers(root))
    sentinel.recover_interrupted_commands(observed_at=iso_now())
    credential_path = Path(args.credential_env_file)
    secrets = parse_env_file(credential_path)
    sequence = int(sentinel.snapshot().get("sentinelSequence", 0)) + 1
    while True:
        observed_at = iso_now()
        refresh_local_evidence(sentinel, root, observed_at)
        reconcile_local_commands(sentinel, root, observed_at)
        try:
            secrets = ensure_machine_credential(
                args.url,
                identity,
                credential_path,
                secrets,
                args.timeout,
                bootstrap_token,
            )
        except Exception:
            if args.once:
                raise
            print(json.dumps({
                "sentAt": observed_at,
                "sequence": sequence,
                "state": "credential_exchange_unavailable",
            }, sort_keys=True), flush=True)
            time.sleep(5)
            continue
        if secrets.get("AGORA_SENTINEL_MACHINE_TOKEN"):
            bootstrap_token = ""
        credential_kind, token = choose_credential(
            sentinel.snapshot(),
            secrets.get("AGORA_SENTINEL_BOOTSTRAP_TOKEN", ""),
            secrets.get("AGORA_SENTINEL_MACHINE_TOKEN", ""),
        )
        if credential_kind == "machine" and identity.get("tokenLabel"):
            machine_client = SentinelIngressClient(args.url, token, timeout=args.timeout)
            secrets = ensure_runtime_secret_bundle(
                machine_client,
                identity,
                str(identity["tokenLabel"]),
                credential_path,
                secrets,
            )
        result = execute_ingress_cycle(
            sentinel,
            SentinelIngressClient(args.url, token, timeout=args.timeout),
            identity=identity,
            credential_kind=credential_kind,
            sequence=sequence,
            sent_at=observed_at,
            fleet_size=args.fleet_size,
            launch_active=sentinel.snapshot()["training"]["state"] != "started",
        )
        command_cursor = result.get("commandCursor")
        store.transaction(lambda state: state.update({
            "sentinelSequence": sequence,
            "lastServerCommandCursor": int(command_cursor) if command_cursor is not None else int(state.get("lastServerCommandCursor", 0)),
        }))
        print(json.dumps({"sentAt": observed_at, "sequence": sequence, "result": result}, sort_keys=True), flush=True)
        if args.once:
            return 0
        sequence += 1
        time.sleep(max(1.0, float(result.get("nextPollSeconds") or 15)))


if __name__ == "__main__":
    raise SystemExit(main())
