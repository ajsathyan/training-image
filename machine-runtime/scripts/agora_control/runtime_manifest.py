"""Canonical source manifest consumed by later machine image builds."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .monitoring.remote_assets import machine_sentinel_setup_revision
from .provider.runpod_create import RUNPOD_DEPENDENCY_RUNTIME_CONTRACT

SCHEMA_VERSION = "agora.machine-runtime-source.v1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_machine_runtime_manifest(root: Path) -> dict[str, Any]:
    root = root.resolve()
    paths = [
        root / "scripts" / "agora_heartbeat_agent.py",
        root / "scripts" / "agora_machine_sentinel_agent.py",
        root / "scripts" / "agora_control" / "execution" / "assets.py",
        root / "scripts" / "agora_control" / "execution" / "image_runtime.py",
        root / "scripts" / "agora_control" / "monitoring" / "remote_assets.py",
        root / "scripts" / "agora_control" / "runtime_manifest.py",
        *(sorted((root / "scripts" / "machine_sentinel").glob("*.py"))),
    ]
    if not paths or any(not path.is_file() for path in paths):
        raise ValueError("canonical machine runtime source is incomplete")
    sources = [
        {"path": path.relative_to(root).as_posix(), "sha256": _sha256(path)}
        for path in paths
    ]
    contract = {
        "schemaVersion": SCHEMA_VERSION,
        "setupRevision": machine_sentinel_setup_revision(),
        "dependencyRuntimeContract": RUNPOD_DEPENDENCY_RUNTIME_CONTRACT,
        "sources": sources,
        "entrypoints": {
            "collector": "scripts/agora_machine_sentinel_agent.py",
            "heartbeatAgent": "scripts/agora_heartbeat_agent.py",
            "setupRenderer": "scripts/agora_control/execution/assets.py",
            "assignmentConfigRenderer": (
                "scripts/agora_control/execution/image_runtime.py:build_machine_image_config"
            ),
            "assignmentManifestRenderer": (
                "scripts/agora_control/execution/image_runtime.py:build_assignment_manifest"
            ),
            "runtimeBundleRenderer": (
                "scripts/agora_control/execution/assets.py:render_machine_runtime_bundle"
            ),
            "heartbeatBundleRenderer": (
                "scripts/agora_control/execution/assets.py:render_baked_heartbeat_runtime_bundle"
            ),
            "sentinelRenderer": "scripts/agora_control/monitoring/remote_assets.py",
        },
        "runtimeState": {
            "eventSpool": "machine-sentinel/events.jsonl",
            "eventCursor": "machine-sentinel/events.cursor.json",
            "credentials": "launch-injected; never embedded in this artifact",
        },
        "imageBuildStatus": "source_ready_not_built",
    }
    contract["artifactFingerprint"] = hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return contract


def write_machine_runtime_manifest(root: Path, output: Path) -> dict[str, Any]:
    manifest = canonical_machine_runtime_manifest(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest
