#!/usr/bin/env python3
"""Canonical assignment lock and compare-and-swap transition for baked images."""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator


SCHEMA_VERSION = 1
STATES = {"fenced", "staged", "ready"}
KINDS = {"stage", "ready", "rollback_prior"}
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
BINDING_KEYS = (
    "schemaVersion",
    "operationId",
    "assignmentGeneration",
    "tokenLabel",
    "tokenInstance",
    "machineId",
    "provider",
    "accountScope",
    "providerResourceId",
    "tokenSha256",
)


class AssignmentTransitionError(RuntimeError):
    pass


def _verify_no_symlink_ancestors(path: Path, *, label: str) -> None:
    """Reject symlinks/non-directories in every existing path component."""

    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise AssignmentTransitionError(f"{label} ancestor is unavailable") from exc
        darwin_system_link = (
            stat.S_ISLNK(metadata.st_mode)
            and sys.platform == "darwin"
            and str(current) in {"/var", "/tmp"}
            and str(current.resolve()) in {"/private/var", "/private/tmp"}
        )
        if (stat.S_ISLNK(metadata.st_mode) and not darwin_system_link) or (
            not stat.S_ISDIR(metadata.st_mode) and not darwin_system_link
        ):
            raise AssignmentTransitionError(
                f"{label} ancestors must be non-symlink directories"
            )


def _regular_private_file(path: Path, mode: int, *, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AssignmentTransitionError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AssignmentTransitionError(f"{label} must be a regular non-symlink file")
    if stat.S_IMODE(metadata.st_mode) != mode:
        raise AssignmentTransitionError(f"{label} must be mode {mode:04o}")
    return metadata


def verify_private_file(
    path: Path, *, mode: int = 0o600, label: str = "private file"
) -> os.stat_result:
    """Verify ancestors plus one regular private file without resolving symlinks."""

    _verify_no_symlink_ancestors(path.parent, label=label)
    return _regular_private_file(path, mode, label=label)


def ensure_private_directory(path: Path, *, label: str = "private directory") -> None:
    """Create and verify one managed private directory without following a symlink."""

    _verify_no_symlink_ancestors(path.parent, label=label)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise AssignmentTransitionError(f"{label} could not be created") from exc
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise AssignmentTransitionError(f"{label} is unavailable") from exc
    except OSError as exc:
        raise AssignmentTransitionError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise AssignmentTransitionError(f"{label} must be a non-symlink directory")
    try:
        os.chmod(path, 0o700, follow_symlinks=False)
        metadata = path.lstat()
    except (NotImplementedError, OSError) as exc:
        raise AssignmentTransitionError(f"{label} cannot enforce mode 0700") from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise AssignmentTransitionError(f"{label} must remain mode 0700")


def preflight_private_root(root: Path) -> None:
    """Prove the selected root honors private POSIX file modes before secrets land."""

    _verify_no_symlink_ancestors(root.parent, label="private runtime root")
    if root.exists() or root.is_symlink():
        ensure_private_directory(root, label="private runtime root")
    else:
        try:
            root.mkdir(parents=True, mode=0o700)
        except OSError as exc:
            raise AssignmentTransitionError("private runtime root could not be created") from exc
        ensure_private_directory(root, label="private runtime root")
    probe = root / f".private-mode-probe.{os.getpid()}"
    private_atomic_write(probe, b"mode-probe\n")
    _regular_private_file(probe, 0o600, label="private mode probe")
    try:
        probe.unlink()
    except OSError as exc:
        raise AssignmentTransitionError("private mode probe could not be removed") from exc


def fence_failed_ready_assignment(
    root: Path,
    config: dict[str, Any],
    *,
    stop_owned: Callable[[], bool],
) -> bool:
    """Demote and stop only the exact ready assignment whose boot failed."""

    with _assignment_lock(root):
        current = _read_manifest(root / "assignment.json")
        target = assignment_binding(config)
        if (
            current is None
            or current.get("state") != "ready"
            or not _same_binding(current, target)
        ):
            return False
        private_atomic_write(
            root / "assignment.json",
            (
                json.dumps(
                    {**current, "state": "staged"},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8"),
        )
        private_atomic_write(
            root / "training-intent.json",
            (
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "assignmentGeneration": config["assignmentGeneration"],
                        "operationId": config["assignmentOperationId"],
                        "desiredState": "paused",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8"),
        )
        return bool(stop_owned())


def assignment_binding(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "operationId": config["assignmentOperationId"],
        "assignmentGeneration": config["assignmentGeneration"],
        "tokenLabel": config["tokenLabel"],
        "tokenInstance": config["tokenInstance"],
        "machineId": config["machineId"],
        "provider": config["provider"],
        "accountScope": config["accountScope"],
        "providerResourceId": config["providerResourceId"],
        "tokenSha256": config["tokenSha256"],
    }


def _validate_manifest(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("state") not in STATES:
        raise AssignmentTransitionError(f"{label} is invalid")
    if value.get("schemaVersion") != SCHEMA_VERSION:
        raise AssignmentTransitionError(f"{label} has unsupported schema")
    for key in BINDING_KEYS:
        if key not in value:
            raise AssignmentTransitionError(f"{label} is missing {key}")
    for key in ("assignmentGeneration", "tokenInstance"):
        if (
            not isinstance(value.get(key), int)
            or isinstance(value.get(key), bool)
            or value[key] < 1
        ):
            raise AssignmentTransitionError(f"{label} has invalid {key}")
    for key in (
        "operationId",
        "tokenLabel",
        "machineId",
        "provider",
        "accountScope",
        "providerResourceId",
    ):
        if not isinstance(value.get(key), str) or not IDENTIFIER.fullmatch(value[key]):
            raise AssignmentTransitionError(f"{label} has invalid {key}")
    if not isinstance(value.get("tokenSha256"), str) or not SHA256.fullmatch(
        value["tokenSha256"]
    ):
        raise AssignmentTransitionError(f"{label} has invalid tokenSha256")
    return {key: value[key] for key in (*BINDING_KEYS, "state")}


def _read_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    verify_private_file(path, label="assignment manifest")
    try:
        return _validate_manifest(
            json.loads(path.read_text(encoding="utf-8")), label="assignment manifest"
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise AssignmentTransitionError("assignment manifest is invalid") from exc


def _same_binding(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left.get(key) == right.get(key) for key in BINDING_KEYS)


@contextlib.contextmanager
def _assignment_lock(root: Path, *, timeout_seconds: float = 30.0) -> Iterator[None]:
    lock = root / "assignment.lock"
    deadline = time.monotonic() + timeout_seconds
    while True:
        created_lock = False
        try:
            lock.mkdir(mode=0o700)
            created_lock = True
            descriptor = os.open(
                lock / "pid",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(f"{os.getpid()}\n")
                handle.flush()
                os.fsync(handle.fileno())
            break
        except FileExistsError:
            if created_lock:
                try:
                    lock.rmdir()
                except OSError:
                    pass
                raise AssignmentTransitionError(
                    "assignment lock metadata path already exists"
                )
            ensure_private_directory(lock, label="assignment lock")
            owner = ""
            try:
                verify_private_file(lock / "pid", label="assignment lock owner")
                owner = (lock / "pid").read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                owner = ""
            if owner.isdigit():
                try:
                    os.kill(int(owner), 0)
                except ProcessLookupError:
                    (lock / "pid").unlink(missing_ok=True)
                    try:
                        lock.rmdir()
                    except OSError:
                        pass
                    continue
                except PermissionError:
                    pass
            if time.monotonic() >= deadline:
                raise AssignmentTransitionError("timed out acquiring assignment lock")
            time.sleep(0.25)
        except OSError as exc:
            if created_lock:
                try:
                    lock.rmdir()
                except OSError:
                    pass
            raise AssignmentTransitionError("assignment lock could not be created") from exc
    try:
        yield
    finally:
        (lock / "pid").unlink(missing_ok=True)
        try:
            lock.rmdir()
        except OSError:
            pass


def _write_manifest(path: Path, value: dict[str, Any]) -> None:
    private_atomic_write(
        path,
        (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        ),
    )


def private_atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write one private durable file with the assignment lock already held."""

    if mode not in {0o600, 0o700}:
        raise AssignmentTransitionError("private file mode must be 0600 or 0700")
    _verify_no_symlink_ancestors(path.parent, label="private file parent")
    if not path.parent.exists():
        try:
            path.parent.mkdir(parents=True, mode=0o700)
        except OSError as exc:
            raise AssignmentTransitionError("private file parent could not be created") from exc
    ensure_private_directory(path.parent, label="private file parent")
    if path.exists() or path.is_symlink():
        try:
            existing = path.lstat()
        except OSError as exc:
            raise AssignmentTransitionError("private destination is unavailable") from exc
        if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
            raise AssignmentTransitionError(
                "private destination must be a regular non-symlink file"
            )
    temporary = path.with_name(f".{path.name}.{os.getpid()}.next")
    if temporary.exists() or temporary.is_symlink():
        raise AssignmentTransitionError("private temporary path already exists")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(temporary, flags, mode)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _regular_private_file(temporary, mode, label="private temporary file")
        os.replace(temporary, path)
        os.chmod(path, mode, follow_symlinks=False)
        _regular_private_file(path, mode, label="private destination")
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise AssignmentTransitionError("private path cannot be a symlink") from exc
        raise AssignmentTransitionError("private atomic write failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists() and not temporary.is_symlink():
            with contextlib.suppress(OSError):
                temporary.unlink()
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


@contextlib.contextmanager
def saved_assignment_observation(
    root: Path, config: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    """Hold the assignment lock and prove config names the exact saved binding.

    Observation services may be restored while training is intentionally stopped,
    but only for the current durable assignment.  This deliberately performs no
    assignment transition and writes no assignment or training state.
    """

    expected = assignment_binding(config)
    with _assignment_lock(root):
        current = _read_manifest(root / "assignment.json")
        if current is None or not _same_binding(current, expected):
            raise AssignmentTransitionError(
                "saved observation configuration does not match the current assignment"
            )
        yield dict(current)


@dataclass
class AssignmentTransition:
    root: Path
    requested: dict[str, Any]
    kind: str
    current: dict[str, Any] | None
    target_state: str
    rollback_authorized: bool
    idempotent: bool
    _committed: bool = False

    def commit(self) -> dict[str, Any]:
        target = {**self.requested, "state": self.target_state}
        _write_manifest(self.root / "assignment.json", target)
        self._committed = True
        return target


def _transition_decision(
    config: dict[str, Any], current: dict[str, Any] | None
) -> AssignmentTransition:
    transition = config.get("assignmentTransition")
    if not isinstance(transition, dict) or transition.get("kind") not in KINDS:
        raise AssignmentTransitionError("assignmentTransition is missing or invalid")
    kind = str(transition["kind"])
    requested = assignment_binding(config)
    _validate_manifest({**requested, "state": "staged"}, label="requested assignment")
    expected_raw = transition.get("expectedManifest")
    expected = (
        None
        if expected_raw is None
        else _validate_manifest(expected_raw, label="expected assignment manifest")
    )
    allow_absent = transition.get("allowAbsent", False)
    if not isinstance(allow_absent, bool):
        raise AssignmentTransitionError("assignmentTransition allowAbsent is invalid")
    if current is None:
        if expected is not None or not allow_absent or kind == "rollback_prior":
            raise AssignmentTransitionError(
                "assignment precondition expected an existing manifest"
            )
        return AssignmentTransition(
            root=Path(config["remoteRoot"]),
            requested=requested,
            kind=kind,
            current=None,
            target_state="staged" if kind == "stage" else "ready",
            rollback_authorized=False,
            idempotent=False,
        )

    same = _same_binding(current, requested)
    if same:
        if kind == "rollback_prior":
            if current["state"] != "fenced":
                raise AssignmentTransitionError(
                    "rollback replay must remain exactly fenced"
                )
            return AssignmentTransition(
                root=Path(config["remoteRoot"]),
                requested=requested,
                kind=kind,
                current=current,
                target_state="fenced",
                rollback_authorized=True,
                idempotent=True,
            )
        allowed_states = (
            {"fenced", "staged", "ready"} if kind == "stage" else {"staged", "ready"}
        )
        if current["state"] not in allowed_states:
            raise AssignmentTransitionError(
                "assignment state cannot make requested transition"
            )
        target_state = (
            "ready" if current["state"] == "ready" or kind == "ready" else "staged"
        )
        return AssignmentTransition(
            root=Path(config["remoteRoot"]),
            requested=requested,
            kind=kind,
            current=current,
            target_state=target_state,
            rollback_authorized=False,
            idempotent=current["state"] == target_state,
        )

    if expected is None or current != expected:
        raise AssignmentTransitionError(
            "assignment manifest changed after controller precondition"
        )
    current_generation = current["assignmentGeneration"]
    requested_generation = requested["assignmentGeneration"]
    if current["state"] != "fenced":
        raise AssignmentTransitionError(
            "a different assignment must be exactly fenced before transition"
        )
    if kind == "rollback_prior":
        if requested_generation >= current_generation:
            raise AssignmentTransitionError(
                "rollback target must have a prior assignment generation"
            )
        return AssignmentTransition(
            root=Path(config["remoteRoot"]),
            requested=requested,
            kind=kind,
            current=current,
            target_state="fenced",
            rollback_authorized=True,
            idempotent=False,
        )
    if requested_generation <= current_generation:
        raise AssignmentTransitionError(
            "new assignment generation must advance the exact fence"
        )
    return AssignmentTransition(
        root=Path(config["remoteRoot"]),
        requested=requested,
        kind=kind,
        current=current,
        target_state="staged" if kind == "stage" else "ready",
        rollback_authorized=False,
        idempotent=False,
    )


@contextlib.contextmanager
def assignment_transition(
    root: Path,
    config: dict[str, Any],
    *,
    owned_process_guard: Callable[[AssignmentTransition], None] | None = None,
) -> Iterator[AssignmentTransition]:
    """Hold the canonical CAS lock through materialization; commit manifest last."""
    root.mkdir(parents=True, exist_ok=True)
    with _assignment_lock(root):
        current = _read_manifest(root / "assignment.json")
        decision = _transition_decision(config, current)
        decision.root = root
        if owned_process_guard is not None and decision.target_state in {
            "fenced",
            "staged",
        }:
            owned_process_guard(decision)
        try:
            yield decision
            if not decision._committed:
                raise AssignmentTransitionError(
                    "assignment transition exited without commit"
                )
        finally:
            # Child guards may start only after this context releases the lock.
            pass


def verify_assignment_postcondition(
    root: Path, config: dict[str, Any], *, required_state: str
) -> dict[str, Any]:
    """Recheck the exact committed binding under the canonical lock."""
    if required_state not in STATES:
        raise AssignmentTransitionError("assignment postcondition state is invalid")
    with _assignment_lock(root):
        current = _read_manifest(root / "assignment.json")
        requested = assignment_binding(config)
        if (
            current is None
            or current.get("state") != required_state
            or not _same_binding(current, requested)
        ):
            raise AssignmentTransitionError(
                "assignment postcondition was replaced or is incomplete"
            )
        return current
