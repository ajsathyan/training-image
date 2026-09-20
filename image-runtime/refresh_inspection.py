#!/usr/bin/env python3
"""Refresh the bounded, non-secret filesystem served by loopback px0."""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import json
import os
import pwd
import stat
import sys
import time
from pathlib import Path


ROOT_LOGS = ("progress.log", "setup.log", "watchdog.log")
LOG_FILES = (
    "server_gpu0.log",
    "launcher-gpu0.log",
    "launcher-active.log",
    "watchdog-check.log",
    "client-repair.log",
    "machine-sentinel.log",
    "machine-sentinel-install-error.log",
    "heartbeat.log",
    "heartbeat-watchdog-check.log",
    "px0.log",
)
DEFAULT_INSPECTION_ROOT = Path("/run/agora-inspection")
DEFAULT_MAX_BYTES = 2 * 1024 * 1024


def _owner(value: str) -> tuple[int, int]:
    try:
        owner = pwd.getpwnam(value)
        return owner.pw_uid, owner.pw_gid
    except KeyError:
        try:
            if ":" in value:
                uid_text, gid_text = value.split(":", 1)
                return int(uid_text), int(gid_text)
            uid = int(value)
            return uid, uid
        except ValueError as exc:
            raise ValueError("inspection owner does not exist") from exc


def _safe_root(raw: str, *, label: str) -> Path:
    path = Path(raw).resolve()
    if not path.is_absolute() or path in {Path("/"), Path("/workspace"), Path("/run")}:
        raise ValueError(f"unsafe {label}")
    return path


def _iso_timestamp_ns(value: int) -> str:
    timestamp = dt.datetime.fromtimestamp(value / 1_000_000_000, tz=dt.timezone.utc)
    return timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def copy_regular(
    source: Path,
    destination: Path,
    *,
    uid: int,
    gid: int,
    max_bytes: int,
) -> dict[str, object] | None:
    """Copy at most the newest max_bytes without following source symlinks."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        if exc.errno not in {errno.ENOENT, errno.ELOOP}:
            raise
        destination.unlink(missing_ok=True)
        return None
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.next")
    temporary.unlink(missing_ok=True)
    try:
        source_stat = os.fstat(descriptor)
        if not stat.S_ISREG(source_stat.st_mode):
            destination.unlink(missing_ok=True)
            return None
        offset = max(0, source_stat.st_size - max_bytes)
        copied = 0
        with os.fdopen(descriptor, "rb", closefd=False) as source_file:
            source_file.seek(offset)
            with temporary.open("wb") as destination_file:
                remaining = max_bytes
                while remaining > 0 and (
                    chunk := source_file.read(min(1024 * 1024, remaining))
                ):
                    destination_file.write(chunk)
                    copied += len(chunk)
                    remaining -= len(chunk)
        temporary.chmod(0o640)
        os.chown(temporary, uid, gid)
        os.utime(
            temporary,
            ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
            follow_symlinks=False,
        )
        temporary.replace(destination)
        return {
            "name": destination.name,
            "modifiedAt": _iso_timestamp_ns(source_stat.st_mtime_ns),
            "sourceSizeBytes": source_stat.st_size,
            "copiedSizeBytes": copied,
            "truncated": offset > 0,
        }
    finally:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)


def refresh(
    root: Path,
    inspection: Path,
    *,
    uid: int,
    gid: int,
    max_bytes: int,
) -> None:
    logs = root / "logs"
    inspection.mkdir(parents=True, exist_ok=True)
    inspection.chmod(0o750)
    os.chown(inspection, uid, gid)
    permitted = set(ROOT_LOGS) | set(LOG_FILES) | {"inspection-metadata.json"}
    for path in inspection.iterdir():
        if path.name not in permitted or path.is_symlink() or not path.is_file():
            if path.is_dir() and not path.is_symlink():
                raise ValueError(
                    f"inspection root contains unexpected directory: {path.name}"
                )
            path.unlink(missing_ok=True)
    rows = []
    for name in ROOT_LOGS:
        row = copy_regular(
            root / name,
            inspection / name,
            uid=uid,
            gid=gid,
            max_bytes=max_bytes,
        )
        if row is not None:
            rows.append(row)
    for name in LOG_FILES:
        row = copy_regular(
            logs / name,
            inspection / name,
            uid=uid,
            gid=gid,
            max_bytes=max_bytes,
        )
        if row is not None:
            rows.append(row)
    machine = json.loads((root / "machine.json").read_text(encoding="utf-8"))
    captured = (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    metadata: dict[str, object] = {
        "schemaVersion": "agora.inspection-metadata.v1",
        "machineId": machine["machineId"],
        "provider": machine["provider"],
        "accountScope": machine["accountScope"],
        "providerResourceId": machine["providerResourceId"],
        "runId": machine["runId"],
        "trainingSessionId": machine["trainingSessionId"],
        "assignmentOperationId": machine["assignmentOperationId"],
        "assignmentGeneration": machine["assignmentGeneration"],
        "capturedAt": captured,
        "mtimeTimezone": "UTC",
        "maxBytesPerLog": max_bytes,
        "logs": sorted(rows, key=lambda row: str(row["name"])),
    }
    destination = inspection / "inspection-metadata.json"
    temporary = inspection / f".inspection-metadata.{os.getpid()}.next"
    temporary.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.chmod(0o640)
    os.chown(temporary, uid, gid)
    temporary.replace(destination)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, help="private Agora runtime root")
    parser.add_argument(
        "--inspection-root", default=str(DEFAULT_INSPECTION_ROOT), help="px0 data root"
    )
    parser.add_argument("--owner", default="agora-inspection")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=15.0)
    args = parser.parse_args()
    if args.max_bytes < 4096 or args.max_bytes > 16 * 1024 * 1024:
        raise ValueError("max-bytes must be between 4096 and 16777216")
    if args.interval_seconds < 5 or args.interval_seconds > 300:
        raise ValueError("interval-seconds must be between 5 and 300")
    root = _safe_root(args.root, label="runtime root")
    inspection = _safe_root(args.inspection_root, label="inspection root")
    try:
        inspection.relative_to(root)
    except ValueError:
        pass
    else:
        raise ValueError("inspection root must be outside the private runtime root")
    uid, gid = _owner(args.owner)
    while True:
        try:
            refresh(
                root,
                inspection,
                uid=uid,
                gid=gid,
                max_bytes=args.max_bytes,
            )
        except Exception as exc:
            if not args.watch:
                raise
            print(f"inspection refresh failed: {exc}", file=sys.stderr, flush=True)
        if not args.watch:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
