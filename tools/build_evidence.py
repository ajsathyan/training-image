#!/usr/bin/env python3
"""Incrementally record build phase and filesystem evidence."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


SCHEMA = "agora.image-build-timing.v1"


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schemaVersion": SCHEMA, "phases": {}, "checkpoints": []}
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schemaVersion") != SCHEMA:
        raise ValueError(f"unexpected evidence schema in {path}")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _filesystem(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "status": "missing"}
    resolved = path.resolve()
    usage = shutil.disk_usage(resolved)
    mount: dict[str, Any] = {}
    try:
        result = subprocess.run(
            ["findmnt", "--json", "--target", str(resolved), "--output", "SOURCE,TARGET,FSTYPE"],
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode == 0:
            mount = json.loads(result.stdout)["filesystems"][0]
    except FileNotFoundError:
        # Local source checks may run on macOS. The hosted Linux workflow has
        # findmnt and records the full mount identity.
        pass
    return {
        "path": str(path),
        "resolvedPath": str(resolved),
        "deviceId": os.stat(resolved).st_dev,
        "source": mount.get("source"),
        "mount": mount.get("target"),
        "filesystemType": mount.get("fstype"),
        "totalBytes": usage.total,
        "usedBytes": usage.used,
        "freeBytes": usage.free,
    }


def _checkpoint(name: str, paths: list[Path]) -> dict[str, Any]:
    return {
        "name": name,
        "observedAtEpoch": int(time.time()),
        "filesystems": [_filesystem(path) for path in paths],
        "note": "phase-boundary observation; not a transient peak measurement",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--path", action="append", type=Path, default=[])
    subparsers = parser.add_subparsers(dest="command", required=True)
    initialize = subparsers.add_parser("init")
    initialize.add_argument("--source-commit", required=True)
    start = subparsers.add_parser("start")
    start.add_argument("name")
    finish = subparsers.add_parser("finish")
    finish.add_argument("name")
    finish.add_argument("--status", required=True)
    checkpoint = subparsers.add_parser("checkpoint")
    checkpoint.add_argument("name")
    cache = subparsers.add_parser("cache")
    cache.add_argument("--status", required=True)
    cache.add_argument("--reference", default="")
    cache.add_argument("--exit-status", type=int)
    arguments = parser.parse_args()

    now = int(time.time())
    paths = arguments.path or [Path.cwd(), Path("/mnt/docker"), Path("/mnt/tmp")]
    value = _load(arguments.output)
    if arguments.command == "init":
        value.update(
            {
                "sourceCommit": arguments.source_commit,
                "startedAtEpoch": now,
                "phases": {},
                "checkpoints": [_checkpoint("initialized", paths)],
            }
        )
    elif arguments.command == "start":
        value["phases"][arguments.name] = {"startedAtEpoch": now, "status": "running"}
        value["checkpoints"].append(_checkpoint(f"pre-{arguments.name}", paths))
    elif arguments.command == "finish":
        phase = value["phases"].setdefault(arguments.name, {})
        phase["finishedAtEpoch"] = now
        phase["status"] = arguments.status
        if "startedAtEpoch" in phase:
            phase["seconds"] = now - int(phase["startedAtEpoch"])
        value["checkpoints"].append(_checkpoint(f"post-{arguments.name}", paths))
    elif arguments.command == "checkpoint":
        value["checkpoints"].append(_checkpoint(arguments.name, paths))
    elif arguments.command == "cache":
        value["cache"] = {
            "status": arguments.status,
            "reference": arguments.reference,
            "exitStatus": arguments.exit_status,
        }
    value["updatedAtEpoch"] = now
    _write(arguments.output, value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
