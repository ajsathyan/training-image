#!/usr/bin/env python3
"""Materialize one exact image-baked Agora runtime from launch configuration."""

from __future__ import annotations

import argparse
import datetime as dt
import functools
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from assignment_transition import (  # noqa: E402
    AssignmentTransitionError,
    assignment_transition,
    verify_assignment_postcondition,
)


RUNTIME_DIR = Path(
    os.environ.get("AGORA_MACHINE_RUNTIME_DIR", "/opt/agora-machine-runtime")
)
TRAINING_SOURCE = Path(os.environ.get("AGORA_TRAINING_SOURCE_DIR", "/opt/agora-source"))
PYTHON_BIN = Path(os.environ.get("AGORA_PYTHON_BIN", "/opt/agora-venv/bin/python"))
DEFAULT_REMOTE_ROOT = "/workspace/agora-run"
DEFAULT_CONFIG_PATH = f"{DEFAULT_REMOTE_ROOT}/controller-input/machine-config.json"
DEFAULT_TOKEN_PATH = f"{DEFAULT_REMOTE_ROOT}/controller-input/hf-token"
DEFAULT_RECEIPT_PATH = f"{DEFAULT_REMOTE_ROOT}/bootstrap-receipt.json"
DEFAULT_INSPECTION_ROOT = Path("/run/agora-inspection")
CAPABILITY_PATH = Path("/opt/agora-image-runtime/capability.json")
FIXED_HOST_PORT = 49200
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$")


class BootstrapError(RuntimeError):
    pass


def _load_runtime_modules():
    scripts = RUNTIME_DIR / "scripts"
    if not scripts.is_dir():
        raise BootstrapError(f"machine runtime is missing: {scripts}")
    sys.path.insert(0, str(RUNTIME_DIR))
    sys.path.insert(0, str(scripts))
    from agora_control.execution import assets  # type: ignore
    from agora_control.monitoring import remote_assets  # type: ignore

    return assets, remote_assets


def _assert_no_owned_training_processes(root: Path, _decision: Any) -> None:
    contract_path = (
        RUNTIME_DIR / "scripts" / "machine_sentinel" / "process_contract.py"
    )
    spec = importlib.util.spec_from_file_location(
        "agora_image_process_contract", contract_path
    )
    if spec is None or spec.loader is None:
        raise BootstrapError("image process ownership contract is unavailable")
    process_contract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(process_contract)
    assignment_owned_process_discovery_shell = getattr(
        process_contract, "assignment_owned_process_discovery_shell", None
    )
    if not callable(assignment_owned_process_discovery_shell):
        raise BootstrapError("image process ownership contract is invalid")

    discovery = assignment_owned_process_discovery_shell(
        training_source_root=str(TRAINING_SOURCE)
    )
    script = f"""set -Eeuo pipefail
ROOT={shlex.quote(str(root))}
assignment_fail() {{ printf 'assignment-fence: %s\\n' "$1" >&2; exit 76; }}
{discovery}
owned="$(assignment_owned_server_inventory)"
[ -z "$owned" ] || {{ printf 'assignment-fence: owned Agora process is still running\\n' >&2; exit 76; }}
"""
    subprocess.run(
        ["bash", "-c", script],
        check=True,
        timeout=30,
        stdout=subprocess.DEVNULL,
    )


def _config(path: Path) -> tuple[dict[str, Any] | None, str]:
    if not path.exists():
        return None, ""
    try:
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise BootstrapError("machine configuration must be mode 0600")
        content = path.read_bytes()
        value = json.loads(content)
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapError("machine configuration is unavailable or invalid") from exc
    if not isinstance(value, dict):
        raise BootstrapError("machine configuration must be an object")
    if value.get("schemaVersion") != "agora.machine-image-config.v1":
        raise BootstrapError("machine configuration has unsupported schemaVersion")
    return value, hashlib.sha256(content).hexdigest()


def _identifier(config: dict[str, Any], name: str) -> str:
    value = str(config.get(name) or "").strip()
    if not IDENTIFIER.fullmatch(value):
        raise BootstrapError(f"machine configuration has invalid {name}")
    return value


def _positive_int(config: dict[str, Any], name: str) -> int:
    value = config.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise BootstrapError(f"machine configuration has invalid {name}")
    return value


def _remote_root(config: dict[str, Any]) -> Path:
    raw = str(config.get("remoteRoot") or DEFAULT_REMOTE_ROOT).strip()
    path = Path(raw)
    if (
        not path.is_absolute()
        or path == Path("/")
        or raw in {"/workspace", "/root", "/opt"}
        or ".." in path.parts
        or len(path.parts) < 3
    ):
        raise BootstrapError(
            "remoteRoot must be a safe absolute path at least two components deep"
        )
    return path


def _secret_file(path: Path, label: str) -> str:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != 0o600:
            raise BootstrapError(f"{label} file must be mode 0600")
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise BootstrapError(f"{label} file is unavailable") from exc


def _validated(config: dict[str, Any], token: str) -> tuple[dict[str, Any], Path]:
    required = (
        "machineId",
        "provider",
        "accountScope",
        "providerResourceId",
        "tokenLabel",
        "assignmentOperationId",
        "runId",
    )
    normalized = {name: _identifier(config, name) for name in required}
    gpu_model = str(config.get("gpuModel") or "").strip()
    if (
        not gpu_model
        or len(gpu_model) > 128
        or any(ord(character) < 32 for character in gpu_model)
    ):
        raise BootstrapError("machine configuration has invalid gpuModel")
    normalized["gpuModel"] = gpu_model
    session_id = str(config.get("trainingSessionId") or "").strip()
    if not IDENTIFIER.fullmatch(session_id):
        raise BootstrapError("machine configuration has invalid trainingSessionId")
    normalized["trainingSessionId"] = session_id
    normalized.update(
        assignmentGeneration=_positive_int(config, "assignmentGeneration"),
        tokenInstance=_positive_int(config, "tokenInstance"),
    )
    for name in (
        "launchId",
        "reservationId",
        "slotId",
        "slotGeneration",
        "machineGenerationId",
    ):
        if name in config:
            normalized[name] = config[name]
    node_type = str(config.get("nodeType") or "").strip().lower()
    if node_type not in {"head", "body", "tail"}:
        raise BootstrapError("machine configuration has invalid nodeType")
    normalized["nodeType"] = node_type
    origin = str(config.get("provisioningOrigin") or "other_non_retiring").strip()
    if not IDENTIFIER.fullmatch(origin):
        raise BootstrapError("machine configuration has invalid provisioningOrigin")
    normalized["provisioningOrigin"] = origin
    host_port = int(config.get("hostPort") or FIXED_HOST_PORT)
    if host_port != FIXED_HOST_PORT:
        raise BootstrapError(f"hostPort must remain {FIXED_HOST_PORT}")
    normalized["hostPort"] = host_port
    announce_port = config.get("announcePort")
    if announce_port is not None:
        if (
            not isinstance(announce_port, int)
            or isinstance(announce_port, bool)
            or not 1 <= announce_port <= 65535
        ):
            raise BootstrapError("machine configuration has invalid announcePort")
        normalized["announcePort"] = announce_port
    else:
        normalized["announcePort"] = None
    if not token:
        raise BootstrapError("configured runtime requires an HF token")
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expected = str(config.get("tokenSha256") or "").strip().lower()
    if expected and expected != digest:
        raise BootstrapError("HF token does not match tokenSha256")
    normalized["tokenSha256"] = digest
    normalized["remoteRoot"] = str(_remote_root(config))
    for name in ("startTraining", "px0Enabled"):
        value = config.get(name, False)
        if not isinstance(value, bool):
            raise BootstrapError(f"machine configuration has invalid {name}")
        normalized[name] = value
    assignment_transition_config = config.get("assignmentTransition")
    if not isinstance(assignment_transition_config, dict):
        raise BootstrapError("machine configuration has invalid assignmentTransition")
    transition_kind = assignment_transition_config.get("kind")
    if transition_kind == "stage" and normalized["startTraining"]:
        raise BootstrapError("stage transition cannot request training start")
    if transition_kind == "ready" and not normalized["startTraining"]:
        raise BootstrapError("ready transition must request training start")
    if transition_kind == "rollback_prior" and normalized["startTraining"]:
        raise BootstrapError("rollback transition cannot request training start")
    normalized["assignmentTransition"] = assignment_transition_config
    sentinel = config.get("sentinel", {})
    if not isinstance(sentinel, dict):
        raise BootstrapError("machine configuration has invalid sentinel")
    normalized["sentinel"] = sentinel
    heartbeat = config.get("heartbeat")
    if not isinstance(heartbeat, dict) or heartbeat.get("mode") not in {
        "disabled",
        "configured",
    }:
        raise BootstrapError("machine configuration has invalid heartbeat")
    if heartbeat["mode"] == "disabled":
        if str(heartbeat.get("url") or ""):
            raise BootstrapError("disabled heartbeat must not name a URL")
    else:
        url = str(heartbeat.get("url") or "").strip()
        if not url.startswith("https://"):
            raise BootstrapError("configured heartbeat requires an HTTPS URL")
        if _identifier(heartbeat, "role") not in {"head", "body", "tail"}:
            raise BootstrapError("configured heartbeat has invalid role")
        if _identifier(heartbeat, "tokenLabel") != normalized["tokenLabel"]:
            raise BootstrapError("heartbeat tokenLabel does not match assignment")
        if _identifier(heartbeat, "runpodPodId") != normalized["providerResourceId"]:
            raise BootstrapError(
                "heartbeat runpodPodId does not match provider resource"
            )
        _identifier(heartbeat, "runpodDcId")
        if not str(heartbeat.get("secretFile") or "").strip():
            raise BootstrapError("configured heartbeat requires secretFile")
        for name in ("intervalSeconds", "jitterSeconds", "timeoutSeconds"):
            value = heartbeat.get(name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or float(value) < 0
                or (name != "jitterSeconds" and float(value) == 0)
            ):
                raise BootstrapError(f"configured heartbeat has invalid {name}")
    normalized["heartbeat"] = heartbeat
    for name in ("fleetId", "authorityEpoch"):
        if name in config and config[name] is not None:
            normalized[name] = (
                _identifier(config, name)
                if name == "fleetId"
                else _positive_int(config, name)
            )
    identity_digest = str(config.get("identityBackupSha256") or "").strip().lower()
    if identity_digest and not re.fullmatch(r"[0-9a-f]{64}", identity_digest):
        raise BootstrapError("machine configuration has invalid identityBackupSha256")
    normalized["identityBackupSha256"] = identity_digest or None
    return normalized, Path(normalized["remoteRoot"])


def _private_write(path: Path, text: str, mode: int = 0o600) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.next")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(mode)
    temporary.replace(path)


def _check_existing_identity(
    root: Path, config: dict[str, Any], *, rollback_authorized: bool = False
) -> None:
    path = root / "machine.json"
    if not path.exists():
        return
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapError("existing machine identity is invalid") from exc
    durable_keys = (
        "machineId",
        "provider",
        "accountScope",
        "providerResourceId",
    )
    if any(existing.get(key) != config.get(key) for key in durable_keys):
        raise BootstrapError(
            "existing durable machine identity does not match launch configuration"
        )
    prior_generation = existing.get("assignmentGeneration")
    if not isinstance(prior_generation, int) or isinstance(prior_generation, bool):
        raise BootstrapError("existing durable assignment generation is invalid")
    if prior_generation > config["assignmentGeneration"] and not rollback_authorized:
        raise BootstrapError(
            "launch configuration is older than the durable assignment"
        )
    if prior_generation == config["assignmentGeneration"]:
        assignment_keys = (
            "assignmentOperationId",
            "tokenLabel",
            "tokenInstance",
        )
        if any(existing.get(key) != config.get(key) for key in assignment_keys):
            raise BootstrapError(
                "launch configuration conflicts with the durable assignment generation"
            )
    elif not rollback_authorized:
        tmux = shutil.which("tmux")
        if (
            tmux
            and subprocess.run(
                [tmux, "has-session", "-t", "agora_gpu"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        ):
            raise BootstrapError(
                "active prior assignment must be stopped before reassignment"
            )


def _check_private_identity(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    path = root / "private_gpu0.key"
    ownership_path = root / "private-identity.json"
    expected = config.get("identityBackupSha256")
    if path.is_symlink():
        raise BootstrapError("private_gpu0.key must be a regular private file")
    if not path.exists():
        if expected:
            raise BootstrapError("verified private_gpu0.key backup is missing")
        return {"status": "new", "sha256": None}
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise BootstrapError("private_gpu0.key must be a regular 0600 file")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if expected and actual != expected:
        raise BootstrapError("private_gpu0.key does not match identityBackupSha256")
    binding_keys = (
        "machineId",
        "provider",
        "accountScope",
        "providerResourceId",
        "assignmentGeneration",
        "assignmentOperationId",
    )
    binding = {key: config.get(key) for key in binding_keys}
    ownership: dict[str, Any] | None = None
    if ownership_path.exists():
        if (
            ownership_path.is_symlink()
            or stat.S_IMODE(ownership_path.stat().st_mode) != 0o600
        ):
            raise BootstrapError(
                "private identity ownership marker must be a regular 0600 file"
            )
        try:
            ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BootstrapError(
                "private identity ownership marker is invalid"
            ) from exc
        if (
            not isinstance(ownership, dict)
            or ownership.get("schemaVersion") != "agora.private-identity-ownership.v1"
            or ownership.get("sha256") != actual
        ):
            raise BootstrapError("private identity ownership marker does not match")
        if any(ownership.get(key) != value for key, value in binding.items()):
            if not expected:
                raise BootstrapError("private identity ownership marker does not match")
            ownership = None
    elif not expected:
        machine_path = root / "machine.json"
        try:
            prior = json.loads(machine_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BootstrapError(
                "existing private_gpu0.key is not owned by this durable assignment"
            ) from exc
        if not isinstance(prior, dict) or any(
            prior.get(key) != value for key, value in binding.items()
        ):
            raise BootstrapError(
                "existing private_gpu0.key is not owned by this durable assignment"
            )
    if ownership is None:
        _private_write(
            ownership_path,
            json.dumps(
                {
                    "schemaVersion": "agora.private-identity-ownership.v1",
                    "sha256": actual,
                    **binding,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
    status = "verified_existing" if expected else "adopted_same_assignment"
    return {"status": status, "sha256": actual}


def _verify_baked_training_source() -> tuple[Path, str]:
    if not (TRAINING_SOURCE / ".git").is_dir():
        raise BootstrapError("image-baked Agora source is not a git checkout")
    commit = subprocess.run(
        ["git", "-C", str(TRAINING_SOURCE), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise BootstrapError("image-baked Agora source has invalid provenance")
    if not PYTHON_BIN.is_file():
        raise BootstrapError("image Python runtime is missing")
    probe = subprocess.run(
        [
            str(PYTHON_BIN),
            "-c",
            (
                "import json,pathlib,agora,agora_server,pithos;"
                "print(json.dumps([str(pathlib.Path(m.__file__).resolve()) "
                "for m in (pithos,agora_server,agora)]))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    paths = json.loads(probe.stdout)
    source_prefix = str(TRAINING_SOURCE.resolve()) + os.sep
    if any(not str(Path(path).resolve()).startswith(source_prefix) for path in paths):
        raise BootstrapError("active interpreter still imports a stale Agora package")
    return TRAINING_SOURCE, commit


def _runtime_training_provenance(
    root: Path,
    config: dict[str, Any],
    capability: dict[str, Any],
    commit: str,
    *,
    current_assignment: dict[str, Any] | None = None,
    rollback_authorized: bool = False,
) -> dict[str, Any]:
    build_commit = str(capability.get("trainingSource", {}).get("commit") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", build_commit):
        raise BootstrapError("image capability has invalid training source provenance")
    if commit == build_commit:
        return {
            "status": "image_baked",
            "buildCommit": build_commit,
            "runtimeCommit": commit,
        }
    marker_path = root / "agora-client-repair-provenance.json"
    if (
        marker_path.is_symlink()
        or not marker_path.is_file()
        or stat.S_IMODE(marker_path.stat().st_mode) != 0o600
    ):
        raise BootstrapError("training source drift has no approved repair provenance")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        machine = json.loads((root / "machine.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapError("approved repair provenance is invalid") from exc
    binding = {
        "machineId": config["machineId"],
        "provider": config["provider"],
        "accountScope": config["accountScope"],
        "providerResourceId": config["providerResourceId"],
        "assignmentGeneration": config["assignmentGeneration"],
        "assignmentOperationId": config["assignmentOperationId"],
    }
    machine_binding = binding
    allowed_marker_bindings = [binding]
    if rollback_authorized:
        if not isinstance(current_assignment, dict):
            raise BootstrapError("rollback repair provenance has no current assignment")
        machine_binding = {
            "machineId": current_assignment["machineId"],
            "provider": current_assignment["provider"],
            "accountScope": current_assignment["accountScope"],
            "providerResourceId": current_assignment["providerResourceId"],
            "assignmentGeneration": current_assignment["assignmentGeneration"],
            "assignmentOperationId": current_assignment["operationId"],
        }
        allowed_marker_bindings.append(machine_binding)
    marker_matches_assignment = isinstance(marker, dict) and any(
        all(marker.get(key) == value for key, value in candidate.items())
        for candidate in allowed_marker_bindings
    )
    if (
        not isinstance(marker, dict)
        or marker.get("schemaVersion") != "agora.client-repair-provenance.v1"
        or marker.get("afterCommit") != commit
        or marker.get("sourcePath") != str(TRAINING_SOURCE.resolve())
        or not re.fullmatch(r"[0-9a-f]{40}", str(marker.get("beforeCommit") or ""))
        or not marker_matches_assignment
        or not isinstance(machine, dict)
        or machine.get("agoraCommit") != commit
        or any(machine.get(key) != value for key, value in machine_binding.items())
    ):
        raise BootstrapError("approved repair provenance does not bind runtime source")
    return {
        "status": "approved_repair",
        "buildCommit": build_commit,
        "runtimeCommit": commit,
        "beforeCommit": marker["beforeCommit"],
        "provenanceSha256": hashlib.sha256(marker_path.read_bytes()).hexdigest(),
    }


def _machine(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": config["machineId"],
        "provider": config["provider"],
        "accountScope": config["accountScope"],
        "providerResourceId": config["providerResourceId"],
        "tokenLabel": config["tokenLabel"],
        "tokenInstance": config["tokenInstance"],
        "assignmentGeneration": config["assignmentGeneration"],
        "assignmentOperationId": config["assignmentOperationId"],
        "tokenSha256": config["tokenSha256"],
        "runId": config["runId"],
        "launchId": config.get("launchId"),
        "reservationId": config.get("reservationId"),
        "slotId": config.get("slotId"),
        "slotGeneration": config.get("slotGeneration"),
        "machineGenerationId": config.get("machineGenerationId"),
        "trainingSessionId": config["trainingSessionId"],
        "gpuModel": config["gpuModel"],
        "agoraJoinRole": config["nodeType"],
        "provisioningOrigin": config["provisioningOrigin"],
        "hostPort": config["hostPort"],
        "announcePort": config["announcePort"],
        "remoteRoot": config["remoteRoot"],
        "tmuxSession": "agora_gpu",
        **({"fleetId": config["fleetId"]} if config.get("fleetId") is not None else {}),
        **(
            {"authorityEpoch": config["authorityEpoch"]}
            if config.get("authorityEpoch") is not None
            else {}
        ),
    }


def _render_training_assets(root: Path, config: dict[str, Any], assets: Any) -> None:
    machine = _machine(config)

    def shell_quote(value: Any) -> str:
        return shlex.quote("" if value is None else str(value))

    render = assets.ScriptRenderers(
        shell_quote=shell_quote,
        heartbeat_install_body=lambda _machine, _settings: "",
        sentinel_install_body=lambda _machine, _settings: "",
        default_remote_root=str(root),
        default_agora_repo_url="file:///opt/agora-source",
    )
    bundle = assets.render_machine_runtime_bundle(
        machine,
        render=render,
        training_source_root=str(TRAINING_SOURCE),
    )
    expected = {
        "assignment-start-guard.sh",
        "launch-agora-gpu0.sh",
        "repair-agora-client.sh",
        "supervise-agora-gpu0.sh",
        "install-watchdog.sh",
    }
    if set(bundle) != expected or not all(
        isinstance(value, str) for value in bundle.values()
    ):
        raise BootstrapError("fleet runtime renderer returned an invalid asset bundle")
    for name, content in bundle.items():
        _private_write(root / name, content, 0o700)


def _heartbeat(root: Path, config: dict[str, Any], assets: Any) -> dict[str, object]:
    heartbeat = config["heartbeat"]
    if heartbeat["mode"] == "disabled":
        return {"requested": False, "status": "disabled"}
    secret_path = Path(str(heartbeat["secretFile"])).resolve()
    try:
        secret_path.relative_to((root / "controller-input").resolve())
    except ValueError as exc:
        raise BootstrapError(
            "heartbeat secretFile must be under controller-input"
        ) from exc
    secret = _secret_file(secret_path, "heartbeat secretFile")
    if not secret.startswith("AGORA_HEARTBEAT_SECRET=") or "\n" in secret:
        raise BootstrapError("heartbeat secretFile has invalid format")

    def shell_quote(value: Any) -> str:
        return shlex.quote("" if value is None else str(value))

    render = assets.ScriptRenderers(
        shell_quote=shell_quote,
        heartbeat_install_body=lambda _machine, _settings: "",
        sentinel_install_body=lambda _machine, _settings: "",
        default_remote_root=str(root),
        default_agora_repo_url="file:///opt/agora-source",
    )
    renderer = getattr(assets, "render_baked_heartbeat_runtime_bundle", None)
    if not callable(renderer):
        raise BootstrapError("fleet runtime has no baked heartbeat renderer")
    bundle = renderer(_machine(config), heartbeat, render=render)
    expected = {
        "start-agora-heartbeat.sh",
        "watchdog-heartbeat-tmux.sh",
        "watch-heartbeat-tmux-loop.sh",
        "install-heartbeat.sh",
    }
    if set(bundle) != expected or not all(
        isinstance(value, str) for value in bundle.values()
    ):
        raise BootstrapError("fleet runtime returned an invalid heartbeat bundle")
    for name, content in bundle.items():
        _private_write(root / name, content, 0o700)
    if not config["startTraining"]:
        return {"requested": True, "status": "staged"}
    agent = Path("/opt/agora-image-runtime/agora_heartbeat_agent.py")
    if not agent.is_file() or stat.S_IMODE(agent.stat().st_mode) & 0o111 == 0:
        raise BootstrapError("image-baked heartbeat agent is unavailable")
    subprocess.run([str(root / "install-heartbeat.sh")], check=True, timeout=30)
    if (
        subprocess.run(
            ["tmux", "has-session", "-t", "agora_heartbeat"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        != 0
    ):
        raise BootstrapError("heartbeat supervision did not start")
    return {"requested": True, "status": "started"}


def _materialize_state(
    root: Path, config: dict[str, Any], token: str, commit: str
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    logs.chmod(0o700)
    cache = root.parent / "pluralis-agora-cache"
    for path in (cache / "hf", cache / "pip", cache / "tmp", root / "tmp"):
        path.mkdir(parents=True, exist_ok=True)
    announce = config.get("announcePort") or ""
    environment = {
        "HF_TOKEN": token,
        "TOKEN_LABEL": config["tokenLabel"],
        "MACHINE_ID": config["machineId"],
        "AGORA_TRAINING_RUN_ID": config["runId"],
        "AGORA_TRAINING_SESSION_ID": config["trainingSessionId"],
        "HOST_PORT": str(config["hostPort"]),
        "ANNOUNCE_PORT": str(announce),
        "PYTHON_BIN": str(PYTHON_BIN),
        "AGORA_COMMIT": commit,
        "HF_HOME": str(cache / "hf"),
        "TRANSFORMERS_CACHE": str(cache / "hf"),
        "XDG_CACHE_HOME": str(cache),
        "PIP_CACHE_DIR": str(cache / "pip"),
        "TMPDIR": str(cache / "tmp"),
    }
    _private_write(
        root / "agora.env",
        "".join(f"{key}={shlex.quote(value)}\n" for key, value in environment.items()),
    )
    machine = {**config, "agoraCommit": commit}
    _private_write(
        root / "machine.json",
        json.dumps(machine, sort_keys=True, separators=(",", ":")) + "\n",
    )
    _private_write(root / "token-label.txt", config["tokenLabel"] + "\n")


def _sentinel_secret_path(
    root: Path, sentinel: dict[str, Any], name: str
) -> Path | None:
    raw = str(sentinel.get(name) or "").strip()
    if not raw:
        return None
    path = Path(raw).resolve()
    allowed = (root / "controller-input").resolve()
    try:
        path.relative_to(allowed)
    except ValueError as exc:
        raise BootstrapError(f"sentinel {name} must be under controller-input") from exc
    return path


def _sentinel_machine(config: dict[str, Any]) -> dict[str, Any]:
    machine = {
        **_machine(config),
        "providerIdentitySource": "launch_configuration",
        "providerMachineId": config["providerResourceId"],
    }
    for name in ("launchId", "reservationId", "slotId", "machineGenerationId"):
        machine[name] = _identifier(config, name)
    machine["slotGeneration"] = _positive_int(config, "slotGeneration")
    return machine


def _durable_sentinel_token(root: Path) -> str:
    path = root / "machine-sentinel" / "credential.env"
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("AGORA_SENTINEL_MACHINE_TOKEN="):
            continue
        words = shlex.split(line, posix=True)
        if len(words) == 1 and "=" in words[0]:
            return words[0].split("=", 1)[1]
    return ""


def _sentinel(root: Path, config: dict[str, Any], remote_assets: Any) -> None:
    sentinel = config["sentinel"]
    mode = str(sentinel.get("mode") or "local").strip().lower()
    url = str(sentinel.get("url") or "").strip()
    bootstrap_path = _sentinel_secret_path(root, sentinel, "bootstrapTokenFile")
    machine_path = _sentinel_secret_path(root, sentinel, "machineTokenFile")
    bootstrap = (
        _secret_file(bootstrap_path, "sentinel bootstrapTokenFile")
        if bootstrap_path is not None and bootstrap_path.exists()
        else ""
    )
    machine_token = (
        _secret_file(machine_path, "sentinel machineTokenFile")
        if machine_path is not None and machine_path.exists()
        else _durable_sentinel_token(root)
    )
    if mode not in {"local", "remote"}:
        raise BootstrapError("sentinel mode must be local or remote")
    if mode == "remote" and (
        not url.startswith("https://") or not (bootstrap or machine_token)
    ):
        raise BootstrapError("sentinel remote configuration is incomplete")
    sentinel_fleet_id = sentinel.get("fleetId")
    sentinel_authority_epoch = sentinel.get("authorityEpoch")
    machine_fleet_id = config.get("fleetId")
    machine_authority_epoch = config.get("authorityEpoch")
    if (
        sentinel_fleet_id is not None
        and machine_fleet_id is not None
        and sentinel_fleet_id != machine_fleet_id
    ):
        raise BootstrapError("sentinel fleetId conflicts with machine fleetId")
    if mode == "remote":
        sentinel_fleet_id = _identifier(sentinel, "fleetId")
        sentinel_authority_epoch = _positive_int(sentinel, "authorityEpoch")
        if (
            machine_authority_epoch is not None
            and sentinel_authority_epoch
            != _positive_int(config, "authorityEpoch")
        ):
            raise BootstrapError(
                "sentinel authorityEpoch conflicts with machine authorityEpoch"
            )
    else:
        if machine_authority_epoch is None:
            raise BootstrapError(
                "sentinel local configuration requires machine authorityEpoch"
            )
        machine_authority_epoch = _positive_int(config, "authorityEpoch")
        if sentinel_authority_epoch is not None:
            nested_authority_epoch = _positive_int(sentinel, "authorityEpoch")
            if nested_authority_epoch != machine_authority_epoch:
                raise BootstrapError(
                    "sentinel authorityEpoch conflicts with machine authorityEpoch"
                )
        sentinel_authority_epoch = machine_authority_epoch
    settings = {
        "url": url,
        "exportEnabled": mode == "remote",
        "bootstrapToken": "",
        "machineToken": machine_token,
        "setupRevision": "computed-from-export",
        "timeoutSeconds": float(sentinel.get("timeoutSeconds", 5)),
        "fleetId": sentinel_fleet_id,
        "authorityEpoch": sentinel_authority_epoch,
        "includeProviderBindingIdentity": True,
    }
    machine = _sentinel_machine(config)
    identity_fn = functools.partial(
        remote_assets.machine_sentinel_identity, FleetError=BootstrapError
    )
    body = remote_assets.remote_machine_sentinel_install_body(
        machine,
        settings,
        DEFAULT_REMOTE_ROOT=str(root),
        FleetError=BootstrapError,
        MACHINE_SENTINEL_AGENT_FILE=RUNTIME_DIR
        / "scripts"
        / "agora_machine_sentinel_agent.py",
        MACHINE_SENTINEL_PACKAGE_DIR=RUNTIME_DIR / "scripts" / "machine_sentinel",
        machine_sentinel_identity=identity_fn,
        sh_single=shlex.quote,
        record_progress=True,
        manage_session=False,
    )
    subprocess.run(
        ["bash"], input="set -Eeuo pipefail\n" + body, text=True, check=True, timeout=60
    )
    credential = root / "machine-sentinel" / "credential.env"
    if not credential.exists():
        _private_write(credential, "")
    session = subprocess.run(
        ["tmux", "has-session", "-t", "agora_sentinel"], check=False
    )
    if session.returncode == 0:
        return
    command = ["tmux", "new-session", "-d", "-s", "agora_sentinel"]
    if bootstrap:
        command.extend(["-e", f"AGORA_SENTINEL_BOOTSTRAP_TOKEN={bootstrap}"])
    command.append(str(root / "start-machine-sentinel.sh"))
    subprocess.run(command, check=True, timeout=15)
    if bootstrap:
        subprocess.run(
            [
                "tmux",
                "set-environment",
                "-t",
                "agora_sentinel",
                "-u",
                "AGORA_SENTINEL_BOOTSTRAP_TOKEN",
            ],
            check=False,
        )
    for path in (bootstrap_path, machine_path):
        if path is not None:
            path.unlink(missing_ok=True)


def _inspection_root(capability: dict[str, Any]) -> Path:
    inspection = capability.get("inspection")
    raw = str(inspection.get("root") if isinstance(inspection, dict) else "")
    path = Path(raw)
    if path != DEFAULT_INSPECTION_ROOT:
        raise BootstrapError("image inspection capability names an unsafe data root")
    return path


def _refresh_inspection(root: Path, capability: dict[str, Any]) -> None:
    inspection = capability.get("inspection")
    path = Path(str(inspection.get("path") if isinstance(inspection, dict) else ""))
    if (
        not isinstance(inspection, dict)
        or path != Path("/opt/agora-image-runtime/refresh_inspection.py")
        or not path.is_file()
        or hashlib.sha256(path.read_bytes()).hexdigest() != inspection.get("sha256")
    ):
        raise BootstrapError("image inspection runtime does not match its capability")
    inspection_root = _inspection_root(capability)
    subprocess.run(
        [
            str(PYTHON_BIN),
            str(path),
            "--root",
            str(root),
            "--inspection-root",
            str(inspection_root),
        ],
        check=True,
        timeout=30,
    )
    if (
        subprocess.run(
            ["tmux", "has-session", "-t", "agora_inspection"], check=False
        ).returncode
        != 0
    ):
        command = " ".join(
            shlex.quote(value)
            for value in (
                str(PYTHON_BIN),
                str(path),
                "--root",
                str(root),
                "--inspection-root",
                str(inspection_root),
                "--watch",
                "--interval-seconds",
                "15",
            )
        )
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", "agora_inspection", command],
            check=True,
            timeout=15,
        )


def _px0(root: Path, enabled: bool, capability: dict[str, Any]) -> None:
    if not enabled:
        return
    if not shutil.which("px0"):
        raise BootstrapError("px0 is unavailable")
    px0_capability = capability.get("px0")
    px0_path = Path(
        str(px0_capability.get("path") if isinstance(px0_capability, dict) else "")
    )
    if (
        not isinstance(px0_capability, dict)
        or px0_path != Path("/usr/local/bin/px0")
        or not px0_path.is_file()
        or hashlib.sha256(px0_path.read_bytes()).hexdigest()
        != px0_capability.get("sha256")
    ):
        raise BootstrapError("px0 does not match the image capability")
    if (
        subprocess.run(
            ["tmux", "has-session", "-t", "agora_px0"], check=False
        ).returncode
        == 0
    ):
        return
    command = (
        "exec runuser -u agora-inspection -- px0 -host 127.0.0.1 -port 7777 -no-open -no-agent "
        f"-no-lsp -no-git -no-telemetry -quiet {shlex.quote(str(_inspection_root(capability)))} "
        f">> {shlex.quote(str(root / 'logs' / 'px0.log'))} 2>&1"
    )
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", "agora_px0", command],
        check=True,
        timeout=15,
    )


def _capability() -> tuple[dict[str, Any], str]:
    try:
        content = CAPABILITY_PATH.read_bytes()
        capability = json.loads(content)
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapError("image capability is unavailable or invalid") from exc
    if (
        not isinstance(capability, dict)
        or capability.get("schemaVersion") != "agora.machine-image-capability.v1"
    ):
        raise BootstrapError("image capability has unsupported schema")
    bootstrap = capability.get("bootstrap")
    if not isinstance(bootstrap, dict) or bootstrap.get("path") != str(
        Path(__file__).resolve()
    ):
        raise BootstrapError("image capability names a different bootstrap")
    actual = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if bootstrap.get("sha256") != actual:
        raise BootstrapError("image bootstrap does not match its capability")
    assignment_capability = capability.get("assignmentTransition")
    assignment_path = Path(
        str(
            assignment_capability.get("path")
            if isinstance(assignment_capability, dict)
            else ""
        )
    )
    if (
        not isinstance(assignment_capability, dict)
        or assignment_capability.get("contractVersion")
        != "agora.assignment-transition.v1"
        or assignment_path != Path("/opt/agora-image-runtime/assignment_transition.py")
        or not assignment_path.is_file()
        or hashlib.sha256(assignment_path.read_bytes()).hexdigest()
        != assignment_capability.get("sha256")
    ):
        raise BootstrapError(
            "image assignment transition does not match its capability"
        )
    heartbeat = capability.get("heartbeat")
    heartbeat_path = Path(
        str(heartbeat.get("path") if isinstance(heartbeat, dict) else "")
    )
    if (
        not isinstance(heartbeat, dict)
        or heartbeat_path != Path("/opt/agora-image-runtime/agora_heartbeat_agent.py")
        or not heartbeat_path.is_file()
        or hashlib.sha256(heartbeat_path.read_bytes()).hexdigest()
        != heartbeat.get("sha256")
    ):
        raise BootstrapError("image heartbeat agent does not match its capability")
    manifest_path = RUNTIME_DIR / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    unsigned = dict(manifest)
    fingerprint = unsigned.pop("artifactFingerprint", "")
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if hashlib.sha256(canonical).hexdigest() != fingerprint:
        raise BootstrapError("image runtime export manifest fingerprint is invalid")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise BootstrapError("image runtime export inventory is missing")
    for entry in files:
        if not isinstance(entry, dict):
            raise BootstrapError("image runtime export inventory is invalid")
        raw_artifact = RUNTIME_DIR / str(entry.get("path") or "")
        artifact = raw_artifact.resolve()
        try:
            artifact.relative_to(RUNTIME_DIR.resolve())
        except ValueError as exc:
            raise BootstrapError("image runtime export path escapes its root") from exc
        if raw_artifact.is_symlink() or not raw_artifact.is_file():
            raise BootstrapError("image runtime export artifact is unavailable")
        if hashlib.sha256(raw_artifact.read_bytes()).hexdigest() != entry.get("sha256"):
            raise BootstrapError("image runtime export artifact hash mismatch")
    runtime = capability.get("runtimeExport")
    if (
        not isinstance(runtime, dict)
        or runtime.get("artifactFingerprint") != manifest.get("artifactFingerprint")
        or runtime.get("manifestSha256") != hashlib.sha256(manifest_bytes).hexdigest()
    ):
        raise BootstrapError("image runtime export does not match its capability")
    return capability, hashlib.sha256(content).hexdigest()


def _verify_declared_capability(
    config: dict[str, Any], capability: dict[str, Any]
) -> None:
    declared = config.get("imageCapability")
    runtime = capability.get("runtimeExport")
    if not isinstance(declared, dict):
        raise BootstrapError("machine configuration has no imageCapability declaration")
    if declared.get("contractVersion") != "agora.machine-image-capability.v1":
        raise BootstrapError(
            "machine configuration declares a different image capability"
        )
    if not isinstance(runtime, dict) or declared.get(
        "runtimeArtifactFingerprint"
    ) != runtime.get("artifactFingerprint"):
        raise BootstrapError(
            "machine configuration declares a different runtime artifact"
        )


def _write_receipt(path: Path, payload: dict[str, Any]) -> None:
    payload = {
        "schemaVersion": "agora.machine-image-bootstrap-receipt.v1",
        "recordedAt": dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        **payload,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    _private_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _record_optional(root: Path, name: str, operation: Any) -> dict[str, str]:
    try:
        operation()
        return {"status": "ready"}
    except Exception as exc:  # Optional diagnostics must not block core setup.
        message = f"{name} unavailable: {exc}"
        with (root / "progress.log").open("a", encoding="utf-8") as handle:
            handle.write(message + "\n")
        return {"status": "unavailable", "detail": str(exc)[:500]}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(DEFAULT_CONFIG_PATH))
    parser.add_argument("--token-file", type=Path, default=Path(DEFAULT_TOKEN_PATH))
    parser.add_argument("--receipt", type=Path, default=Path(DEFAULT_RECEIPT_PATH))
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    config, config_hash = _config(args.config)
    if config is None:
        print(
            "agora image runtime: no machine configuration; training and inspection remain disabled"
        )
        return 0
    root = _remote_root(config)
    receipt_path = args.receipt.resolve()
    expected_receipt = (root / "bootstrap-receipt.json").resolve()
    if receipt_path != expected_receipt:
        raise BootstrapError("receipt path must be the canonical file under remoteRoot")
    try:
        capability, capability_hash = _capability()
        _verify_declared_capability(config, capability)
        token_path = args.token_file.resolve()
        try:
            token_path.relative_to((root / "controller-input").resolve())
        except ValueError as exc:
            raise BootstrapError(
                "HF token file must be under controller-input"
            ) from exc
        token = _secret_file(token_path, "HF token")
        normalized, root = _validated(config, token)
        assets, remote_assets = _load_runtime_modules()
        root.mkdir(parents=True, exist_ok=True)
        _source, commit = _verify_baked_training_source()
        with assignment_transition(
            root,
            normalized,
            owned_process_guard=lambda decision: _assert_no_owned_training_processes(
                root, decision
            ),
        ) as transition:
            preserved_ready = (
                transition.kind == "stage"
                and transition.idempotent
                and transition.target_state == "ready"
            )
            runtime_training_source = _runtime_training_provenance(
                root,
                normalized,
                capability,
                commit,
                current_assignment=transition.current,
                rollback_authorized=transition.rollback_authorized,
            )
            _check_existing_identity(
                root,
                normalized,
                rollback_authorized=transition.rollback_authorized,
            )
            identity = _check_private_identity(root, normalized)
            if not preserved_ready:
                _materialize_state(root, normalized, token, commit)
                _render_training_assets(root, normalized, assets)
            assignment_manifest = transition.commit()
        training = {
            "requested": normalized["startTraining"],
            "status": (
                "fenced"
                if normalized["assignmentTransition"]["kind"] == "rollback_prior"
                else ("already_started" if preserved_ready else "staged")
            ),
        }
        if normalized["startTraining"]:
            if normalized.get("announcePort") is None:
                raise BootstrapError("training start requires an announcePort")
            subprocess.run([str(root / "install-watchdog.sh")], check=True, timeout=30)
            if (
                subprocess.run(
                    ["tmux", "has-session", "-t", "agora_gpu"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ).returncode
                != 0
            ):
                raise BootstrapError("training supervisor did not start")
            training["status"] = "started"
        if preserved_ready:
            heartbeat = {
                "requested": normalized["heartbeat"]["mode"] == "configured",
                "status": (
                    "started"
                    if normalized["heartbeat"]["mode"] == "configured"
                    and subprocess.run(
                        ["tmux", "has-session", "-t", "agora_heartbeat"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    ).returncode
                    == 0
                    else (
                        "disabled"
                        if normalized["heartbeat"]["mode"] == "disabled"
                        else "staged"
                    )
                ),
            }
            optional = {
                name: {"status": "preserved"}
                for name in ("sentinel", "inspection", "px0")
            }
        else:
            heartbeat = _heartbeat(root, normalized, assets)
            inspection = _record_optional(
                root, "inspection", lambda: _refresh_inspection(root, capability)
            )
            if not normalized["px0Enabled"]:
                px0 = {"status": "disabled"}
            elif inspection["status"] != "ready":
                px0 = {"status": "blocked_by_inspection"}
            else:
                px0 = _record_optional(
                    root, "px0", lambda: _px0(root, True, capability)
                )
            optional = {
                "sentinel": _record_optional(
                    root, "sentinel", lambda: _sentinel(root, normalized, remote_assets)
                ),
                "inspection": inspection,
                "px0": px0,
            }
        transition_kind = normalized["assignmentTransition"]["kind"]
        required_state = assignment_manifest["state"]
        if required_state in {"staged", "fenced"} and (
            subprocess.run(
                ["tmux", "has-session", "-t", "agora_gpu"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        ):
            raise BootstrapError(
                f"{transition_kind} postcondition requires stopped training"
            )
        if required_state == "ready" and (
            subprocess.run(
                ["tmux", "has-session", "-t", "agora_gpu"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            != 0
        ):
            raise BootstrapError("ready postcondition requires supervised training")
        verify_assignment_postcondition(root, normalized, required_state=required_state)
        _write_receipt(
            receipt_path,
            {
                "status": "ready",
                "machineId": normalized["machineId"],
                "provider": normalized["provider"],
                "accountScope": normalized["accountScope"],
                "providerResourceId": normalized["providerResourceId"],
                "launchId": normalized.get("launchId"),
                "reservationId": normalized.get("reservationId"),
                "slotId": normalized.get("slotId"),
                "slotGeneration": normalized.get("slotGeneration"),
                "machineGenerationId": normalized.get("machineGenerationId"),
                "assignmentOperationId": normalized["assignmentOperationId"],
                "assignmentGeneration": normalized["assignmentGeneration"],
                "assignmentTransition": {
                    "kind": normalized["assignmentTransition"]["kind"],
                    "state": assignment_manifest["state"],
                    "rollbackAuthorized": transition.rollback_authorized,
                    "idempotent": transition.idempotent,
                },
                "configSha256": config_hash,
                "capabilitySha256": capability_hash,
                "fleetSource": capability["fleetSource"],
                "runtimeExport": capability["runtimeExport"],
                "trainingSource": capability["trainingSource"],
                "runtimeTrainingSource": runtime_training_source,
                "training": training,
                "heartbeat": heartbeat,
                "privateIdentity": identity,
                "optional": optional,
            },
        )
        print(
            f"agora image runtime: ready machine={normalized['machineId']} root={root} commit={commit}"
        )
        return 0
    except (
        AssignmentTransitionError,
        BootstrapError,
        subprocess.SubprocessError,
        OSError,
        ValueError,
    ) as exc:
        _write_receipt(
            receipt_path,
            {
                "status": "failed",
                "configSha256": config_hash,
                "detail": str(exc)[:500],
            },
        )
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        AssignmentTransitionError,
        BootstrapError,
        subprocess.SubprocessError,
        OSError,
        ValueError,
    ) as exc:
        print(f"agora image runtime: {exc}", file=sys.stderr)
        raise SystemExit(70)
