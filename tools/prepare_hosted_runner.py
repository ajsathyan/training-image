#!/usr/bin/env python3
"""Prepare bounded disk space for the hosted image-build runner.

The mutating entrypoint is deliberately unavailable outside a GitHub-hosted
Linux runner.  Tests exercise the pure planning helpers; they cannot enable the
production mutation path with a flag or injected environment.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable


MIN_FREE_BYTES = 96 * 1024**3
EXPECTED_PATHS = (Path("/mnt"), Path("/mnt/docker"), Path("/mnt/tmp"))
CLEANUP_ALLOWLIST = (
    Path("/usr/local/lib/android"),
    Path("/usr/share/dotnet"),
    Path("/opt/ghc"),
    Path("/usr/local/.ghcup"),
    Path("/usr/share/swift"),
    Path("/opt/az"),
    Path("/opt/microsoft"),
    Path("/usr/lib/google-cloud-sdk"),
    Path("/usr/local/share/boost"),
    Path("/usr/local/share/chromium"),
    Path("/usr/local/share/powershell"),
    Path("/usr/share/miniconda"),
)


class PreparationError(RuntimeError):
    """A safe hosted-runner preparation precondition was not met."""


def is_github_hosted_linux(environ: dict[str, str] | os._Environ[str]) -> bool:
    """Return whether the immutable production identity checks are satisfied."""
    return (
        environ.get("GITHUB_ACTIONS") == "true"
        and environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and environ.get("RUNNER_OS") == "Linux"
        and sys.platform.startswith("linux")
        and platform.system() == "Linux"
    )


def cleanup_candidates(existing: Iterable[Path]) -> list[Path]:
    """Return existing candidates in the reviewed deletion order."""
    existing_set = set(existing)
    return [path for path in CLEANUP_ALLOWLIST if path in existing_set]


def _mount_for(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    result = subprocess.run(
        ["findmnt", "--json", "--target", str(resolved), "--output", "SOURCE,TARGET,FSTYPE"],
        check=True,
        text=True,
        capture_output=True,
    )
    filesystem = json.loads(result.stdout)["filesystems"][0]
    usage = shutil.disk_usage(resolved)
    return {
        "path": str(path),
        "resolvedPath": str(resolved),
        "deviceId": os.stat(resolved).st_dev,
        "source": filesystem["source"],
        "mount": filesystem["target"],
        "filesystemType": filesystem["fstype"],
        "totalBytes": usage.total,
        "usedBytes": usage.used,
        "freeBytes": usage.free,
    }


def measure_filesystems(paths: Iterable[Path]) -> list[dict[str, Any]]:
    return [_mount_for(path) for path in paths]


def deficient_devices(
    measurements: Iterable[dict[str, Any]], minimum: int = MIN_FREE_BYTES
) -> list[dict[str, Any]]:
    """Return one record for each unique device below the capacity budget."""
    by_device: dict[int, dict[str, Any]] = {}
    for measurement in measurements:
        device = int(measurement["deviceId"])
        current = by_device.get(device)
        if current is None or int(measurement["freeBytes"]) < int(current["freeBytes"]):
            by_device[device] = measurement
    return [item for item in by_device.values() if int(item["freeBytes"]) < minimum]


def _write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _record(evidence_path: Path, evidence: dict[str, Any], stage: str) -> None:
    evidence["stage"] = stage
    evidence["updatedAtEpoch"] = int(time.time())
    _write_evidence(evidence_path, evidence)


def _assert_safe_host() -> None:
    if not is_github_hosted_linux(os.environ):
        raise PreparationError(
            "refusing mutation outside a GitHub-hosted Linux runner "
            "(requires GITHUB_ACTIONS=true, RUNNER_ENVIRONMENT=github-hosted, RUNNER_OS=Linux)"
        )
    if os.geteuid() != 0:
        raise PreparationError("hosted runner preparation must run as root")
    for parent in (Path("/mnt"), Path("/opt"), Path("/usr"), Path("/var/lib")):
        resolved = parent.resolve(strict=True)
        metadata = resolved.stat()
        if resolved != parent or metadata.st_uid != 0 or not stat.S_ISDIR(metadata.st_mode):
            raise PreparationError(f"unsafe hosted runner parent: {parent}")
    if Path("/opt/hostedtoolcache") in CLEANUP_ALLOWLIST:
        raise PreparationError("hosted toolcache must never be a cleanup target")


def _mountpoints() -> list[Path]:
    """Read decoded mountpoints without invoking a mutable helper."""
    points: list[Path] = []
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        decoded = (
            fields[4]
            .replace("\\040", " ")
            .replace("\\011", "\t")
            .replace("\\012", "\n")
            .replace("\\134", "\\")
        )
        points.append(Path(decoded))
    return points


def _contains_path(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_mutation_target(
    path: Path, *, root_device: int, mountpoints: Iterable[Path]
) -> dict[str, Any]:
    if path.is_symlink():
        raise PreparationError(f"refusing symlink mutation target: {path}")
    if not path.exists():
        raise PreparationError(f"mutation target does not exist: {path}")
    resolved = path.resolve(strict=True)
    metadata = resolved.stat()
    if resolved != path or metadata.st_uid != 0 or not stat.S_ISDIR(metadata.st_mode):
        raise PreparationError(f"unsafe mutation target: {path}")
    if metadata.st_dev != root_device:
        raise PreparationError(f"mutation target is not on the hosted root filesystem: {path}")
    nested = [
        str(mountpoint)
        for mountpoint in mountpoints
        if mountpoint != Path("/") and _contains_path(resolved, mountpoint)
    ]
    if nested:
        raise PreparationError(
            f"mutation target contains a mountpoint: {path}: {', '.join(nested)}"
        )
    return {"path": str(path), "resolvedPath": str(resolved), "deviceId": metadata.st_dev}


def _docker_root() -> Path:
    result = subprocess.run(
        ["docker", "info", "--format", "{{.DockerRootDir}}"],
        check=True,
        text=True,
        capture_output=True,
    )
    value = result.stdout.strip()
    if value != "/var/lib/docker":
        raise PreparationError(f"unexpected initial Docker root: {value!r}")
    return Path(value)


def _preflight_layout(github_env: Path) -> list[dict[str, Any]]:
    if (
        not github_env.is_absolute()
        or github_env.is_symlink()
        or not github_env.is_file()
        or github_env.resolve(strict=True) != github_env
    ):
        raise PreparationError(f"refusing unsafe GITHUB_ENV: {github_env}")
    root_device = os.stat("/").st_dev
    mountpoints = _mountpoints()
    targets = [Path("/mnt"), _docker_root()]
    targets.extend(path for path in (Path("/mnt/docker"), Path("/mnt/tmp")) if path.exists())
    targets.extend(path for path in CLEANUP_ALLOWLIST if path.exists())
    return [
        _validate_mutation_target(path, root_device=root_device, mountpoints=mountpoints)
        for path in targets
    ]


def _safe_remove(candidate: Path) -> None:
    if candidate not in CLEANUP_ALLOWLIST:
        raise PreparationError(f"path is not allowlisted: {candidate}")
    if candidate.is_symlink():
        raise PreparationError(f"refusing symlink cleanup target: {candidate}")
    if not candidate.exists():
        return
    resolved = candidate.resolve(strict=True)
    if resolved != candidate or resolved == Path("/"):
        raise PreparationError(f"unsafe cleanup target: {candidate} -> {resolved}")
    shutil.rmtree(resolved)


def _run(*command: str) -> None:
    subprocess.run(command, check=True)


def _prepare_mounts(github_env: Path) -> None:
    _run("systemctl", "stop", "docker")
    Path("/mnt/docker").mkdir(mode=0o755, parents=True, exist_ok=True)
    Path("/mnt/tmp").mkdir(mode=0o1777, parents=True, exist_ok=True)
    os.chmod("/mnt/tmp", 0o1777)
    docker_root = Path("/mnt/docker")
    if docker_root.resolve(strict=True) != docker_root:
        raise PreparationError("unsafe /mnt/docker path")
    for child in docker_root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    Path("/etc/docker/daemon.json").write_text(
        json.dumps({"data-root": "/mnt/docker"}) + "\n", encoding="utf-8"
    )
    _run("systemctl", "start", "docker")
    with github_env.open("a", encoding="utf-8") as stream:
        stream.write("TMPDIR=/mnt/tmp\n")


def prepare(evidence_path: Path, github_env: Path) -> None:
    evidence: dict[str, Any] = {
        "schemaVersion": "agora.hosted-runner-preparation.v1",
        "minimumFreeBytes": MIN_FREE_BYTES,
        "cleanupAllowlist": [str(path) for path in CLEANUP_ALLOWLIST],
        "deleted": [],
        "commands": [],
        "startedAtEpoch": int(time.time()),
        "status": "running",
    }
    _record(evidence_path, evidence, "identity_guard")
    try:
        _assert_safe_host()
        evidence["identity"] = {
            "githubActions": os.environ["GITHUB_ACTIONS"],
            "runnerEnvironment": os.environ["RUNNER_ENVIRONMENT"],
            "runnerOs": os.environ["RUNNER_OS"],
            "platform": platform.system(),
        }
        evidence["validatedMutationTargets"] = _preflight_layout(github_env)
        evidence["initialFilesystems"] = measure_filesystems((Path.cwd(), Path("/mnt")))
        current = evidence["initialFilesystems"]
        low = deficient_devices(current)
        root_device = os.stat("/").st_dev
        unreachable = [item for item in low if int(item["deviceId"]) != root_device]
        if unreachable:
            raise PreparationError(
                "capacity budget is low on a filesystem outside bounded hosted-root cleanup: "
                + ", ".join(str(item["mount"]) for item in unreachable)
            )
        if low:
            _record(evidence_path, evidence, "docker_prune")
            evidence["commands"].append("docker system prune -af")
            _run("docker", "system", "prune", "-af")
            current = measure_filesystems((Path.cwd(), Path("/mnt")))
            evidence.setdefault("cleanupMeasurements", []).append(
                {"after": "docker system prune -af", "filesystems": current}
            )
            _record(evidence_path, evidence, "measured-after:docker-prune")

        for candidate in cleanup_candidates(path for path in CLEANUP_ALLOWLIST if path.exists()):
            if not deficient_devices(current):
                break
            _record(evidence_path, evidence, f"delete:{candidate}")
            _safe_remove(candidate)
            evidence["deleted"].append(str(candidate))
            current = measure_filesystems((Path.cwd(), Path("/mnt")))
            evidence.setdefault("cleanupMeasurements", []).append(
                {"after": str(candidate), "filesystems": current}
            )
            _record(evidence_path, evidence, f"measured-after:{candidate}")

        _record(evidence_path, evidence, "relocate_docker")
        _prepare_mounts(github_env)
        evidence["postSetupFilesystems"] = measure_filesystems((Path.cwd(), *EXPECTED_PATHS[1:]))
        low = deficient_devices(evidence["postSetupFilesystems"])
        if low:
            raise PreparationError(
                "insufficient disk after bounded cleanup: "
                + ", ".join(f"{item['mount']}={item['freeBytes']}" for item in low)
            )
        evidence["status"] = "passed"
        evidence["finishedAtEpoch"] = int(time.time())
        _record(evidence_path, evidence, "complete")
    except BaseException as error:
        evidence["failedStage"] = evidence.get("stage")
        evidence["status"] = "failed"
        evidence["errorType"] = type(error).__name__
        evidence["error"] = str(error)
        evidence["exitStatus"] = (
            error.returncode if isinstance(error, subprocess.CalledProcessError) else 1
        )
        evidence["finishedAtEpoch"] = int(time.time())
        _record(evidence_path, evidence, evidence["failedStage"])
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--github-env", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        prepare(arguments.evidence, arguments.github_env)
    except (PreparationError, OSError, subprocess.SubprocessError) as error:
        print(f"hosted runner preparation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
