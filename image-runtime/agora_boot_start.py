#!/usr/bin/env python3
"""Resolve provider boot metadata and invoke the canonical image bootstrap."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from assignment_transition import (  # noqa: E402
    AssignmentTransitionError,
    ensure_private_directory,
    private_atomic_write,
    verify_private_file,
)

SCHEMA = "agora.machine-boot-launch.v1"
BOOTSTRAP = Path("/opt/agora-image-runtime/agora_image_bootstrap.py")
PYTHON = Path("/opt/agora-venv/bin/python")
DEFAULT_ROOT = Path("/var/lib/agora-runtime")
STAGING_ROOT = Path(os.environ.get("AGORA_BOOT_STAGING_ROOT", "/run"))
STATUS = Path("/run/agora-image-bootstrap.status.json")
CAPABILITY = Path("/opt/agora-image-runtime/capability.json")
ACTIVE_ROOT_POINTER = Path(
    os.environ.get("AGORA_ACTIVE_ROOT_POINTER", "/var/lib/agora/active-root.json")
)
PROVIDER_ENV_FILES = (Path("/root/.env_vars/env_vars.txt"), Path("/etc/rp_environment"))
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$")
PORT = re.compile(r"^[1-9][0-9]{0,4}$")
VAST_LABEL = re.compile(r"^C\.([1-9][0-9]*)$")
VAST_CONTAINER_ID = re.compile(r"^[1-9][0-9]*$")


class BootInputError(RuntimeError):
    pass


def _verify_boot_capability() -> None:
    try:
        capability = json.loads(CAPABILITY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootInputError("image boot capability is unavailable or invalid") from exc
    boot_start = capability.get("bootStart") if isinstance(capability, dict) else None
    if (
        not isinstance(boot_start, dict)
        or boot_start.get("contractVersion") != SCHEMA
        or boot_start.get("path") != str(Path(__file__).resolve())
        or boot_start.get("sha256")
        != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    ):
        raise BootInputError("image boot adapter does not match its capability")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.next")
    if temporary.exists() or temporary.is_symlink():
        raise BootInputError("status temporary path is unsafe")
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
            descriptor = -1
            json.dump(dict(value), stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise BootInputError("status path cannot enforce mode 0600")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists() and not temporary.is_symlink():
            with contextlib.suppress(OSError):
                temporary.unlink()


def _status(state: str, reason: str, **fields: Any) -> None:
    _atomic_json(STATUS, {"schemaVersion": 1, "state": state, "reason": reason, **fields})


def _decode_launch(environment: Mapping[str, str]) -> dict[str, Any]:
    encoded = str(environment.get("AGORA_BOOT_LAUNCH_B64") or "")
    if not encoded:
        raise BootInputError("missing launch envelope")
    try:
        raw = base64.b64decode(encoded, validate=True)
        value = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise BootInputError("launch envelope is not valid base64 JSON") from exc
    if not isinstance(value, dict) or value.get("schemaVersion") != SCHEMA:
        raise BootInputError("launch envelope has unsupported schemaVersion")
    config = value.get("config")
    if not isinstance(config, dict):
        raise BootInputError("launch envelope has no config object")
    if config.get("schemaVersion") != "agora.machine-image-config.v1":
        raise BootInputError("launch envelope config has unsupported schemaVersion")
    return value


def _normalized_remote_root(raw: Any) -> Path:
    text = str(raw or "").strip()
    path = Path(text)
    if (
        not path.is_absolute()
        or path == Path("/")
        or text in {"/workspace", "/root", "/opt"}
        or ".." in path.parts
        or len(path.parts) < 3
    ):
        raise BootInputError(
            "remoteRoot must be a safe absolute path at least two components deep"
        )
    return path


def _root_identity(config: Mapping[str, Any]) -> dict[str, Any]:
    generation = config.get("assignmentGeneration")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise BootInputError("launch assignment generation is invalid")
    identity = {
        "machineId": str(config.get("machineId") or ""),
        "provider": str(config.get("provider") or ""),
        "accountScope": str(config.get("accountScope") or ""),
        "providerResourceId": str(config.get("providerResourceId") or ""),
        "assignmentOperationId": str(config.get("assignmentOperationId") or ""),
        "assignmentGeneration": generation,
    }
    if any(not value for key, value in identity.items() if key != "assignmentGeneration"):
        raise BootInputError("launch root identity is incomplete")
    return identity


def _root_identity_digest(config: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _root_identity(config), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_active_root_pointer() -> dict[str, Any] | None:
    path = ACTIVE_ROOT_POINTER
    if not path.exists() and not path.is_symlink():
        return None
    try:
        verify_private_file(path, label="active root pointer")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootInputError("active root pointer is unavailable or corrupt") from exc
    if (
        not isinstance(value, dict)
        or value.get("schemaVersion") != "agora.active-runtime-root.v1"
        or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("identitySha256") or ""))
        or not isinstance(value.get("assignmentGeneration"), int)
        or isinstance(value.get("assignmentGeneration"), bool)
        or value["assignmentGeneration"] < 1
    ):
        raise BootInputError("active root pointer is invalid")
    value["remoteRoot"] = str(_normalized_remote_root(value.get("remoteRoot")))
    return value


def _record_active_root(
    config: Mapping[str, Any], *, rollback_authorized: bool = False
) -> None:
    root = _normalized_remote_root(config.get("remoteRoot"))
    identity = _root_identity(config)
    wanted = {
        "schemaVersion": "agora.active-runtime-root.v1",
        "remoteRoot": str(root),
        "assignmentGeneration": identity["assignmentGeneration"],
        "assignmentOperationId": identity["assignmentOperationId"],
        "identitySha256": _root_identity_digest(config),
    }
    current = _read_active_root_pointer()
    if current is not None:
        current_generation = int(current["assignmentGeneration"])
        wanted_generation = int(wanted["assignmentGeneration"])
        if wanted_generation < current_generation and not rollback_authorized:
            raise BootInputError("launch root pointer is older than saved assignment")
        if wanted_generation == current_generation and any(
            current.get(key) != wanted.get(key)
            for key in ("remoteRoot", "assignmentOperationId", "identitySha256")
        ):
            raise BootInputError("launch root pointer conflicts with saved assignment")
    try:
        if not ACTIVE_ROOT_POINTER.parent.exists():
            ACTIVE_ROOT_POINTER.parent.mkdir(parents=True, mode=0o700)
        ensure_private_directory(
            ACTIVE_ROOT_POINTER.parent, label="active root pointer directory"
        )
        private_atomic_write(
            ACTIVE_ROOT_POINTER,
            (json.dumps(wanted, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            ),
        )
    except AssignmentTransitionError as exc:
        raise BootInputError(str(exc)) from exc


def _parse_env_file(path: Path) -> dict[str, str]:
    try:
        if not path.is_file() or path.is_symlink():
            return {}
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (value.startswith("'") and value.endswith("'")) or (
            value.startswith('"') and value.endswith('"')
        ):
            value = value[1:-1]
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            values[key] = value
    return values


def _metadata_sources(environment: Mapping[str, str]) -> list[Mapping[str, str]]:
    paths = [PROVIDER_ENV_FILES[0]]
    home_path = Path(str(environment.get("HOME") or "/root")) / ".env_vars/env_vars.txt"
    if home_path != paths[0]:
        paths.append(home_path)
    paths.extend(PROVIDER_ENV_FILES[1:])
    return [environment, *(_parse_env_file(path) for path in paths)]


def resolve_provider_metadata_with_retry(
    provider: str,
    environment: Mapping[str, str],
    *,
    wait_seconds: float,
    interval_seconds: float = 1.0,
) -> tuple[str, int]:
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        try:
            return resolve_provider_metadata(provider, environment)
        except BootInputError as exc:
            if not str(exc).startswith("missing provider metadata:"):
                raise
            if time.monotonic() >= deadline:
                raise
            _status("waiting_for_network_config", str(exc)[:500])
            time.sleep(max(0.01, interval_seconds))


def _consistent(sources: list[Mapping[str, str]], keys: tuple[str, ...], label: str) -> str:
    values = {
        str(source.get(key) or "").strip()
        for source in sources
        for key in keys
        if str(source.get(key) or "").strip()
    }
    if not values:
        raise BootInputError(f"missing provider metadata: {label}")
    if len(values) != 1:
        raise BootInputError(f"conflicting provider metadata: {label}")
    return next(iter(values))


def resolve_provider_metadata(
    provider: str, environment: Mapping[str, str]
) -> tuple[str, int]:
    sources = _metadata_sources(environment)
    if provider == "runpod":
        resource = _consistent(sources, ("RUNPOD_POD_ID",), "RUNPOD_POD_ID")
        raw_port = _consistent(
            sources, ("RUNPOD_TCP_PORT_49200",), "RUNPOD_TCP_PORT_49200"
        )
    elif provider == "vast":
        direct = [
            str(source.get("CONTAINER_ID") or "").strip()
            for source in sources
            if str(source.get("CONTAINER_ID") or "").strip()
        ]
        if any(VAST_CONTAINER_ID.fullmatch(value) is None for value in direct):
            raise BootInputError("malformed provider metadata: CONTAINER_ID")
        labels = [
            str(source.get("VAST_CONTAINERLABEL") or "").strip()
            for source in sources
            if str(source.get("VAST_CONTAINERLABEL") or "").strip()
        ]
        normalized = set(direct)
        for label in labels:
            match = VAST_LABEL.fullmatch(label)
            if match is None:
                raise BootInputError("malformed provider metadata: VAST_CONTAINERLABEL")
            normalized.add(match.group(1))
        if not normalized:
            raise BootInputError("missing provider metadata: Vast instance id")
        if len(normalized) != 1:
            raise BootInputError("conflicting provider metadata: Vast instance id")
        resource = next(iter(normalized))
        raw_port = _consistent(
            sources, ("VAST_TCP_PORT_49200",), "VAST_TCP_PORT_49200"
        )
    else:
        raise BootInputError("launch envelope provider must be runpod or vast")
    if not IDENTIFIER.fullmatch(resource):
        raise BootInputError("provider resource id is malformed")
    if not PORT.fullmatch(raw_port) or not 1 <= int(raw_port) <= 65535:
        raise BootInputError("provider public port is malformed")
    return resource, int(raw_port)


def _saved_selection(root: Path, launch: Mapping[str, Any]) -> str:
    manifest_path = root / "assignment.json"
    intent_path = root / "training-intent.json"
    if not manifest_path.exists():
        return "launch"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootInputError("saved assignment or training intent is corrupt") from exc
    generation = manifest.get("assignmentGeneration")
    launch_generation = launch["config"].get("assignmentGeneration")
    if not isinstance(generation, int) or not isinstance(launch_generation, int):
        raise BootInputError("saved or launch assignment generation is invalid")
    if manifest.get("state") in {"staged", "fenced"}:
        return "stopped"
    if intent.get("desiredState") == "paused":
        return "stopped"
    if generation >= launch_generation:
        return "saved"
    return "launch"


def _run_bootstrap(config: dict[str, Any], token: str, *, persist: bool) -> int:
    root = _normalized_remote_root(config.get("remoteRoot"))
    root.mkdir(parents=True, exist_ok=True)
    receipt = root / "bootstrap-receipt.json"
    with tempfile.TemporaryDirectory(
        prefix="agora-boot-", dir=str(STAGING_ROOT)
    ) as directory:
        stage = Path(directory)
        config_path = stage / "machine-config.json"
        token_path = stage / "hf-token"
        _atomic_json(config_path, config)
        descriptor = os.open(
            token_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        command = [
            str(PYTHON),
            str(BOOTSTRAP),
            "--config",
            str(config_path),
            "--token-file",
            str(token_path),
            "--receipt",
            str(receipt),
        ]
        if persist:
            command.append("--persist-input")
        return subprocess.run(
            command, check=False, env=_child_environment()
        ).returncode


def _child_environment() -> dict[str, str]:
    child_environment = dict(os.environ)
    for name in (
        "HF_TOKEN",
        "AGORA_BOOT_HF_TOKEN",
        "AGORA_BOOT_LAUNCH_B64",
        "AGORA_SENTINEL_BOOTSTRAP_TOKEN",
        "AGORA_SENTINEL_MACHINE_TOKEN",
    ):
        child_environment.pop(name, None)
    return child_environment


def _apply_provider_metadata(
    config: dict[str, Any], resource: str, port: int, *, refresh_port: bool = False
) -> None:
    supplied_resource = str(config.get("providerResourceId") or "").strip()
    if supplied_resource and supplied_resource != resource:
        raise BootInputError("provider resource id conflicts with provider metadata")
    supplied_port = config.get("announcePort")
    if (
        not refresh_port
        and supplied_port is not None
        and (not isinstance(supplied_port, int) or supplied_port != port)
    ):
        raise BootInputError("public training port conflicts with provider metadata")
    config["providerResourceId"] = resource
    config["announcePort"] = port


def _saved_boot(root: Path, environment: Mapping[str, str]) -> int:
    canonical = root / "controller-input"
    try:
        verify_private_file(
            canonical / "machine-config.json", label="saved machine configuration"
        )
        verify_private_file(canonical / "hf-token", label="saved HF token")
        config = json.loads(
            (canonical / "machine-config.json").read_text(encoding="utf-8")
        )
        token = (canonical / "hf-token").read_text(encoding="utf-8").strip()
    except (OSError, json.JSONDecodeError) as exc:
        raise BootInputError("saved controller input is unavailable or corrupt") from exc
    if not isinstance(config, dict) or not token:
        raise BootInputError("saved controller input is unavailable or corrupt")
    resource, port = resolve_provider_metadata_with_retry(
        str(config.get("provider") or "").lower(),
        environment,
        wait_seconds=_metadata_wait_seconds(environment),
    )
    _apply_provider_metadata(config, resource, port, refresh_port=True)
    return _run_bootstrap(config, token, persist=True)


def _restore_saved_observation(root: Path) -> int:
    canonical = root / "controller-input"
    return subprocess.run(
        [
            str(PYTHON),
            str(BOOTSTRAP),
            "--config",
            str(canonical / "machine-config.json"),
            "--token-file",
            str(canonical / "hf-token"),
            "--receipt",
            str(root / "bootstrap-receipt.json"),
            "--observation-resume",
        ],
        check=False,
        env=_child_environment(),
    ).returncode


def _metadata_wait_seconds(environment: Mapping[str, str]) -> float:
    try:
        wait_seconds = float(environment.get("AGORA_BOOT_METADATA_WAIT_SECONDS") or "30")
    except ValueError as exc:
        raise BootInputError("metadata wait duration is invalid") from exc
    if not math.isfinite(wait_seconds) or wait_seconds < 0:
        raise BootInputError("metadata wait duration is invalid")
    return wait_seconds


def main(environment: Mapping[str, str] | None = None) -> int:
    environment = os.environ if environment is None else environment
    if str(environment.get("AGORA_BOOT_AUTOSTART") or "") != "1":
        try:
            pointer = _read_active_root_pointer()
        except BootInputError as exc:
            _status("waiting_for_controller_config", str(exc)[:500])
            return 0
        if pointer is None:
            _status(
                "waiting_for_controller_config",
                "no active runtime root is configured",
            )
            print(
                "agora image runtime: no active runtime root; training and inspection remain disabled"
            )
            return 0
        saved_root = _normalized_remote_root(pointer["remoteRoot"])
        canonical = saved_root / "controller-input"
        if (canonical / "machine-config.json").is_file():
            try:
                verify_private_file(
                    canonical / "machine-config.json",
                    label="saved machine configuration",
                )
                verify_private_file(canonical / "hf-token", label="saved HF token")
                saved_config = json.loads(
                    (canonical / "machine-config.json").read_text(encoding="utf-8")
                )
                saved_generation = saved_config.get("assignmentGeneration")
                if not isinstance(saved_generation, int):
                    raise BootInputError("saved controller input is corrupt")
                if (
                    _root_identity_digest(saved_config)
                    != pointer.get("identitySha256")
                    or saved_generation != pointer.get("assignmentGeneration")
                    or saved_config.get("assignmentOperationId")
                    != pointer.get("assignmentOperationId")
                    or str(_normalized_remote_root(saved_config.get("remoteRoot")))
                    != str(saved_root)
                ):
                    raise BootInputError(
                        "active root pointer does not match saved controller identity"
                    )
                if (
                    _saved_selection(
                        saved_root,
                        {"config": {"assignmentGeneration": saved_generation}},
                    )
                    == "stopped"
                ):
                    rc = _restore_saved_observation(saved_root)
                    _status(
                        "stopped" if rc == 0 else "observation_failed",
                        (
                            "saved assignment or pause state prevents training resume; "
                            "optional observation restored"
                            if rc == 0
                            else f"saved training remains stopped; observation restore exited with status {rc}"
                        ),
                        selection="saved",
                    )
                    return 0
            except (OSError, json.JSONDecodeError, BootInputError):
                _status("stopped", "saved controller input is corrupt")
                return 0
            rc = _saved_boot(saved_root, environment)
            _status(
                "ready" if rc == 0 else "failed",
                "canonical controller input completed"
                if rc == 0
                else f"canonical bootstrap exited with status {rc}",
                selection="saved",
            )
            return 0
        _status(
            "waiting_for_controller_config",
            "active runtime root has no saved controller input",
        )
        print(
            "agora image runtime: active root has no machine configuration; training and inspection remain disabled"
        )
        return 0
    try:
        _verify_boot_capability()
        launch = _decode_launch(environment)
        token = str(environment.get("AGORA_BOOT_HF_TOKEN") or "")
        if not token or any(character in token for character in "\r\n\x00"):
            raise BootInputError("missing or invalid machine-scoped HF token")
        expected = str(launch.get("tokenSha256") or "").lower()
        if hashlib.sha256(token.encode()).hexdigest() != expected:
            raise BootInputError("machine-scoped HF token digest mismatch")
        config = dict(launch["config"])
        root = _normalized_remote_root(config.get("remoteRoot"))
        selection = _saved_selection(root, launch)
        if selection == "stopped":
            rc = _restore_saved_observation(root)
            _status(
                "stopped" if rc == 0 else "observation_failed",
                (
                    "saved assignment or pause state outranks launch input; optional observation restored"
                    if rc == 0
                    else f"saved training remains stopped; observation restore exited with status {rc}"
                ),
            )
            return 0
        if selection == "saved":
            rc = _saved_boot(root, environment)
        else:
            resource, port = resolve_provider_metadata_with_retry(
                str(config.get("provider") or "").lower(),
                environment,
                wait_seconds=_metadata_wait_seconds(environment),
            )
            _apply_provider_metadata(config, resource, port)
            rc = _run_bootstrap(config, token, persist=True)
        if rc != 0:
            raise BootInputError(f"canonical bootstrap exited with status {rc}")
        _status("ready", "canonical bootstrap completed", selection=selection)
        return 0
    except BootInputError as exc:
        reason = str(exc)[:500]
        state = (
            "waiting_for_network_config"
            if reason.startswith("missing provider metadata:")
            else "invalid_input"
        )
        _status(state, reason)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
