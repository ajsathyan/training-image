#!/usr/bin/env python3
"""Prepare a live image-runtime overlay from a compatible released image."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


SCHEMA = "agora.machine-image-capability.v1"


class FixturePreparationError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixturePreparationError(f"{label} is unavailable or invalid") from exc
    if not isinstance(value, dict):
        raise FixturePreparationError(f"{label} must be an object")
    return value


def _verify_runtime(runtime: Path) -> tuple[dict[str, Any], bytes]:
    manifest_path = runtime / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = _json(manifest_path, label="machine runtime manifest")
    if manifest.get("schemaVersion") != "agora.machine-runtime-export.v1":
        raise FixturePreparationError("machine runtime manifest has unsupported schema")
    unsigned = dict(manifest)
    fingerprint = unsigned.pop("artifactFingerprint", "")
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(canonical).hexdigest() != fingerprint:
        raise FixturePreparationError("machine runtime manifest fingerprint is invalid")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise FixturePreparationError("machine runtime manifest has no inventory")
    resolved_root = runtime.resolve()
    for entry in files:
        if not isinstance(entry, dict):
            raise FixturePreparationError("machine runtime inventory entry is invalid")
        candidate = runtime / str(entry.get("path") or "")
        try:
            candidate.resolve().relative_to(resolved_root)
        except ValueError as exc:
            raise FixturePreparationError("machine runtime inventory escapes its root") from exc
        if candidate.is_symlink() or not candidate.is_file():
            raise FixturePreparationError("machine runtime inventory file is unavailable")
        if _sha256(candidate) != entry.get("sha256"):
            raise FixturePreparationError(
                f"machine runtime inventory hash mismatch: {entry.get('path')}"
            )
    return manifest, manifest_bytes


def prepare(repo: Path, base_runtime: Path, output: Path) -> dict[str, Any]:
    repo = repo.resolve()
    base_runtime = base_runtime.resolve()
    output = output.resolve()
    source_runtime = repo / "image-runtime"
    machine_runtime = repo / "machine-runtime"
    if output == Path("/") or output in {repo, base_runtime}:
        raise FixturePreparationError("output must be a dedicated fixture directory")
    if not source_runtime.is_dir() or not base_runtime.is_dir():
        raise FixturePreparationError("image runtime source or base runtime is missing")
    manifest, manifest_bytes = _verify_runtime(machine_runtime)
    capability = _json(base_runtime / "capability.json", label="base image capability")
    if capability.get("schemaVersion") != SCHEMA:
        raise FixturePreparationError("base image capability has unsupported schema")

    if output.exists():
        shutil.rmtree(output)
    shutil.copytree(base_runtime, output, symlinks=False)
    for source in sorted(source_runtime.glob("*.py")):
        if source.is_symlink() or not source.is_file():
            raise FixturePreparationError(f"image runtime source is unsafe: {source.name}")
        shutil.copy2(source, output / source.name)
    heartbeat = machine_runtime / "scripts" / "agora_heartbeat_agent.py"
    if heartbeat.is_symlink() or not heartbeat.is_file():
        raise FixturePreparationError("heartbeat agent is unavailable")
    shutil.copy2(heartbeat, output / "agora_heartbeat_agent.py")

    declared_files = {
        "bootstrap": "agora_image_bootstrap.py",
        "bootStart": "agora_boot_start.py",
        "assignmentTransition": "assignment_transition.py",
        "inspection": "refresh_inspection.py",
        "heartbeat": "agora_heartbeat_agent.py",
    }
    for section, filename in declared_files.items():
        value = capability.get(section)
        path = output / filename
        if not isinstance(value, dict) or not path.is_file():
            raise FixturePreparationError(
                f"base image capability is missing {section} compatibility"
            )
        value["sha256"] = _sha256(path)
    capability["fleetSource"] = manifest.get("source")
    capability["runtimeExport"] = {
        "artifactFingerprint": manifest["artifactFingerprint"],
        "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }
    (output / "capability.json").write_text(
        json.dumps(capability, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for path in output.iterdir():
        if path.is_file():
            path.chmod(0o755 if path.suffix == ".py" else 0o644)
    return capability


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--base-image-runtime", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    capability = prepare(args.repo, args.base_image_runtime, args.output)
    print(
        json.dumps(
            {
                "schemaVersion": SCHEMA,
                "runtimeArtifactFingerprint": capability["runtimeExport"][
                    "artifactFingerprint"
                ],
                "capabilitySha256": _sha256(args.output / "capability.json"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FixturePreparationError as exc:
        print(f"process composition fixture: {exc}", flush=True)
        raise SystemExit(2) from exc
