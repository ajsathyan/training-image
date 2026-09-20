#!/usr/bin/env python3
"""Resolve provider boot metadata and invoke the canonical image bootstrap."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA = "agora.machine-boot-launch.v1"
BOOTSTRAP = Path("/opt/agora-image-runtime/agora_image_bootstrap.py")
PYTHON = Path("/opt/agora-venv/bin/python")
DEFAULT_ROOT = Path("/workspace/agora-run")
STAGING_ROOT = Path(os.environ.get("AGORA_BOOT_STAGING_ROOT", "/run"))
STATUS = Path("/run/agora-image-bootstrap.status.json")
CAPABILITY = Path("/opt/agora-image-runtime/capability.json")
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
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(dict(value), stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


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
    if generation > launch_generation:
        return "saved"
    return "launch"


def _run_bootstrap(config: dict[str, Any], token: str, *, persist: bool) -> int:
    root = Path(str(config.get("remoteRoot") or DEFAULT_ROOT))
    root.mkdir(parents=True, exist_ok=True)
    receipt = root / "bootstrap-receipt.json"
    with tempfile.TemporaryDirectory(
        prefix="agora-boot-", dir=str(STAGING_ROOT)
    ) as directory:
        stage = Path(directory)
        config_path = stage / "machine-config.json"
        token_path = stage / "hf-token"
        config_path.write_text(
            json.dumps(config, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        token_path.write_text(token + "\n", encoding="utf-8")
        config_path.chmod(0o600)
        token_path.chmod(0o600)
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
        child_environment = dict(os.environ)
        for name in (
            "HF_TOKEN",
            "AGORA_BOOT_HF_TOKEN",
            "AGORA_BOOT_LAUNCH_B64",
            "AGORA_SENTINEL_BOOTSTRAP_TOKEN",
            "AGORA_SENTINEL_MACHINE_TOKEN",
        ):
            child_environment.pop(name, None)
        return subprocess.run(
            command, check=False, env=child_environment
        ).returncode


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
        canonical = DEFAULT_ROOT / "controller-input"
        if (canonical / "machine-config.json").is_file():
            try:
                saved_config = json.loads(
                    (canonical / "machine-config.json").read_text(encoding="utf-8")
                )
                saved_generation = saved_config.get("assignmentGeneration")
                if not isinstance(saved_generation, int):
                    raise BootInputError("saved controller input is corrupt")
                if (
                    _saved_selection(
                        DEFAULT_ROOT,
                        {"config": {"assignmentGeneration": saved_generation}},
                    )
                    == "stopped"
                ):
                    _status(
                        "stopped",
                        "saved assignment or pause state prevents boot resume",
                        selection="saved",
                    )
                    return 0
            except (OSError, json.JSONDecodeError, BootInputError):
                _status("stopped", "saved controller input is corrupt")
                return 0
            rc = subprocess.run(
                [
                    str(PYTHON),
                    str(BOOTSTRAP),
                    "--config",
                    str(canonical / "machine-config.json"),
                    "--token-file",
                    str(canonical / "hf-token"),
                    "--receipt",
                    str(DEFAULT_ROOT / "bootstrap-receipt.json"),
                    "--boot-resume",
                ],
                check=False,
            ).returncode
            _status(
                "ready" if rc == 0 else "failed",
                "canonical controller input completed"
                if rc == 0
                else f"canonical bootstrap exited with status {rc}",
                selection="saved",
            )
            return 0
        _status("manual", "boot autostart is not enabled")
        print(
            "agora image runtime: no machine configuration; training and inspection remain disabled"
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
        root = Path(str(config.get("remoteRoot") or DEFAULT_ROOT))
        selection = _saved_selection(root, launch)
        if selection == "stopped":
            _status("stopped", "saved assignment or pause state outranks launch input")
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
