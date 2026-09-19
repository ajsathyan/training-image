#!/usr/bin/env python3
"""Export the reviewed Agora machine runtime from an exact fleet-repo commit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


DEFAULT_SOURCE_COMMIT = "7ff63e5ed211804d92aa94a17251625f90519061"
SOURCE_MANIFEST_PATH = "runtime/machine-runtime-source.v1.json"
RENDERER_DEPENDENCY = "scripts/agora_control/fleet_read_model_bootstrap.py"
PACKAGE_MARKERS = (
    "scripts/__init__.py",
    "scripts/agora_control/__init__.py",
    "scripts/agora_control/execution/__init__.py",
    "scripts/agora_control/monitoring/__init__.py",
)
OUTPUT_MANIFEST = "manifest.json"
EXPORT_SCHEMA = "agora.machine-runtime-export.v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ExportError(RuntimeError):
    """A source or destination cannot be exported safely."""


@dataclass(frozen=True)
class Artifact:
    path: str
    content: bytes
    kind: str
    source_path: str | None = None
    source_sha256: str | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_relative_path(raw: Any, *, label: str) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise ExportError(f"{label} must be a non-empty POSIX relative path")
    path = PurePosixPath(raw)
    if (
        not path.parts
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ExportError(f"{label} is not a safe relative path: {raw!r}")
    normalized = path.as_posix()
    if normalized != raw:
        raise ExportError(f"{label} is not normalized: {raw!r}")
    return normalized


def _git(repo: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", b"")
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace").strip()
        raise ExportError(
            f"git {' '.join(arguments)} failed: {detail or error}"
        ) from error
    return result.stdout


def _resolve_commit(repo: Path, requested: str) -> tuple[str, str]:
    commit = (
        _git(repo, "rev-parse", "--verify", f"{requested}^{{commit}}").decode().strip()
    )
    tree = _git(repo, "rev-parse", "--verify", f"{commit}^{{tree}}").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit) or not re.fullmatch(
        r"[0-9a-f]{40,64}", tree
    ):
        raise ExportError("git returned an invalid commit or tree id")
    return commit, tree


def _git_file(repo: Path, commit: str, path: str) -> bytes:
    safe_path = _safe_relative_path(path, label="source path")
    entry = _git(repo, "ls-tree", "-z", commit, "--", safe_path)
    if not entry:
        raise ExportError(f"source file does not exist at {commit}: {safe_path}")
    try:
        metadata, listed_path = entry.rstrip(b"\0").split(b"\t", 1)
        mode, object_type, _object_id = metadata.decode("ascii").split(" ")
    except (ValueError, UnicodeDecodeError) as error:
        raise ExportError(f"could not inspect source entry {safe_path}") from error
    if listed_path.decode("utf-8", errors="strict") != safe_path:
        raise ExportError(f"git returned an unexpected source path for {safe_path}")
    if object_type != "blob" or mode not in {"100644", "100755"}:
        raise ExportError(f"source must be a regular tracked file: {safe_path}")
    return _git(repo, "show", f"{commit}:{safe_path}")


def _parse_source_manifest(content: bytes) -> dict[str, Any]:
    try:
        manifest = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExportError(f"{SOURCE_MANIFEST_PATH} is not valid JSON") from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("schemaVersion") != "agora.machine-runtime-source.v1"
    ):
        raise ExportError(f"{SOURCE_MANIFEST_PATH} has an unsupported schema")

    fingerprint = manifest.get("artifactFingerprint")
    if not isinstance(fingerprint, str) or not SHA256_RE.fullmatch(fingerprint):
        raise ExportError("source manifest artifactFingerprint is missing or invalid")
    unsigned = dict(manifest)
    del unsigned["artifactFingerprint"]
    if _sha256(_canonical_json(unsigned)) != fingerprint:
        raise ExportError(
            "source manifest artifactFingerprint does not match its contents"
        )

    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ExportError("source manifest must list at least one source file")
    seen: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ExportError("source manifest entries must be objects")
        path = _safe_relative_path(source.get("path"), label="source manifest path")
        digest = source.get("sha256")
        if not path.startswith("scripts/"):
            raise ExportError(f"runtime source is outside scripts/: {path}")
        if (
            ".agora" in PurePosixPath(path).parts
            or "controller" in PurePosixPath(path).parts
        ):
            raise ExportError(f"runtime source includes excluded state: {path}")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise ExportError(f"source hash is missing or invalid for {path}")
        if path in seen:
            raise ExportError(f"duplicate source path: {path}")
        seen.add(path)
    return manifest


def _source_artifacts(
    repo: Path,
    commit: str,
    source_manifest: dict[str, Any],
) -> list[Artifact]:
    artifacts: dict[str, Artifact] = {}
    for source in source_manifest["sources"]:
        path = source["path"]
        content = _git_file(repo, commit, path)
        actual_hash = _sha256(content)
        if actual_hash != source["sha256"]:
            raise ExportError(
                f"source hash mismatch for {path}: expected {source['sha256']}, got {actual_hash}"
            )
        artifacts[path] = Artifact(
            path=path,
            content=content,
            kind="source",
            source_path=path,
            source_sha256=actual_hash,
        )

    if RENDERER_DEPENDENCY not in artifacts:
        content = _git_file(repo, commit, RENDERER_DEPENDENCY)
        artifacts[RENDERER_DEPENDENCY] = Artifact(
            path=RENDERER_DEPENDENCY,
            content=content,
            kind="renderer_dependency",
            source_path=RENDERER_DEPENDENCY,
            source_sha256=_sha256(content),
        )

    for path in PACKAGE_MARKERS:
        if path not in artifacts:
            artifacts[path] = Artifact(
                path=path, content=b"", kind="generated_package_marker"
            )
    return [artifacts[path] for path in sorted(artifacts)]


def _build_manifest(
    artifacts: list[Artifact],
    *,
    commit: str,
    tree: str,
    source_manifest_hash: str,
    source_artifact_fingerprint: str,
) -> dict[str, Any]:
    files: list[dict[str, str]] = []
    for artifact in artifacts:
        entry = {
            "kind": artifact.kind,
            "path": artifact.path,
            "sha256": artifact.sha256,
        }
        if artifact.source_path is not None:
            entry["sourcePath"] = artifact.source_path
            entry["sourceSha256"] = artifact.source_sha256 or artifact.sha256
        files.append(entry)
    unsigned: dict[str, Any] = {
        "files": files,
        "schemaVersion": EXPORT_SCHEMA,
        "source": {
            "artifactFingerprint": source_artifact_fingerprint,
            "commit": commit,
            "manifestPath": SOURCE_MANIFEST_PATH,
            "manifestSha256": source_manifest_hash,
            "tree": tree,
        },
    }
    return {
        **unsigned,
        "artifactFingerprint": _sha256(_canonical_json(unsigned)),
    }


def _manifest_artifact_paths(manifest_bytes: bytes) -> dict[str, str]:
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExportError(
            "existing export manifest is invalid; refusing to overwrite output"
        ) from error
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != EXPORT_SCHEMA:
        raise ExportError(
            "existing output is not a recognized generated runtime; refusing to overwrite"
        )
    fingerprint = manifest.get("artifactFingerprint")
    unsigned = dict(manifest)
    unsigned.pop("artifactFingerprint", None)
    if (
        not isinstance(fingerprint, str)
        or _sha256(_canonical_json(unsigned)) != fingerprint
    ):
        raise ExportError(
            "existing export manifest fingerprint is invalid; refusing to overwrite"
        )
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ExportError(
            "existing export manifest has no file inventory; refusing to overwrite"
        )
    result: dict[str, str] = {}
    for entry in files:
        if not isinstance(entry, dict):
            raise ExportError("existing export manifest has an invalid file entry")
        path = _safe_relative_path(entry.get("path"), label="existing artifact path")
        digest = entry.get("sha256")
        if (
            path == OUTPUT_MANIFEST
            or path in result
            or not isinstance(digest, str)
            or not SHA256_RE.fullmatch(digest)
        ):
            raise ExportError(
                "existing export manifest has an invalid artifact inventory"
            )
        result[path] = digest
    return result


def _existing_files(output_dir: Path) -> set[str]:
    if not output_dir.exists():
        return set()
    if not output_dir.is_dir() or output_dir.is_symlink():
        raise ExportError(f"output path is not a regular directory: {output_dir}")
    result: set[str] = set()
    for path in output_dir.rglob("*"):
        if path.is_symlink():
            raise ExportError(
                f"output contains a symlink; refusing to overwrite: {path}"
            )
        if path.is_file():
            result.add(path.relative_to(output_dir).as_posix())
    return result


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def export_runtime(
    source_repo: Path, output_dir: Path, source_commit: str
) -> dict[str, Any]:
    source_repo = source_repo.resolve()
    output_dir = output_dir.resolve()
    commit, tree = _resolve_commit(source_repo, source_commit)
    source_manifest_bytes = _git_file(source_repo, commit, SOURCE_MANIFEST_PATH)
    source_manifest = _parse_source_manifest(source_manifest_bytes)
    artifacts = _source_artifacts(source_repo, commit, source_manifest)
    generated_manifest = _build_manifest(
        artifacts,
        commit=commit,
        tree=tree,
        source_manifest_hash=_sha256(source_manifest_bytes),
        source_artifact_fingerprint=source_manifest["artifactFingerprint"],
    )

    prior: dict[str, str] = {}
    manifest_path = output_dir / OUTPUT_MANIFEST
    if manifest_path.exists():
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ExportError("existing output manifest is not a regular file")
        prior = _manifest_artifact_paths(manifest_path.read_bytes())
    existing = _existing_files(output_dir)
    managed_or_manifest = set(prior) | {OUTPUT_MANIFEST}
    unexpected = sorted(existing - managed_or_manifest)
    if unexpected:
        raise ExportError(
            "output contains files not owned by a prior generated manifest: "
            + ", ".join(unexpected)
        )

    for path, expected_hash in prior.items():
        artifact_path = output_dir / path
        if (
            artifact_path.exists()
            and _sha256(artifact_path.read_bytes()) != expected_hash
        ):
            raise ExportError(
                f"previous generated artifact was modified; refusing to overwrite: {path}"
            )

    wanted = {artifact.path for artifact in artifacts}
    for stale in sorted(set(prior) - wanted):
        stale_path = output_dir / stale
        if stale_path.exists():
            stale_path.unlink()

    output_dir.mkdir(parents=True, exist_ok=True)
    for artifact in artifacts:
        _atomic_write(output_dir / artifact.path, artifact.content)
    manifest_bytes = (
        json.dumps(generated_manifest, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _atomic_write(manifest_path, manifest_bytes)
    return generated_manifest


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-repo",
        required=True,
        type=Path,
        help="local clone of the fleet repository containing the pinned commit",
    )
    parser.add_argument(
        "--source-commit",
        default=DEFAULT_SOURCE_COMMIT,
        help=f"exact fleet repository commit (default: {DEFAULT_SOURCE_COMMIT})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "machine-runtime",
        help="generated artifact directory (default: repository machine-runtime/)",
    )
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    try:
        manifest = export_runtime(args.source_repo, args.output_dir, args.source_commit)
    except ExportError as error:
        print(f"runtime export failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "artifactFingerprint": manifest["artifactFingerprint"],
                "fileCount": len(manifest["files"]),
                "sourceCommit": manifest["source"]["commit"],
                "sourceTree": manifest["source"]["tree"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
