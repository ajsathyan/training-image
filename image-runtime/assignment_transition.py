#!/usr/bin/env python3
"""Canonical assignment lock and compare-and-swap transition for baked images."""

from __future__ import annotations

import contextlib
import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 1
STATES = {"fenced", "staged", "ready"}
KINDS = {"stage", "ready", "rollback_prior"}
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
    if not isinstance(value.get("assignmentGeneration"), int) or isinstance(
        value.get("assignmentGeneration"), bool
    ):
        raise AssignmentTransitionError(f"{label} has invalid generation")
    for key in BINDING_KEYS:
        if key not in value:
            raise AssignmentTransitionError(f"{label} is missing {key}")
    return {key: value[key] for key in (*BINDING_KEYS, "state")}


def _read_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if (
        path.is_symlink()
        or not path.is_file()
        or stat.S_IMODE(path.stat().st_mode) != 0o600
    ):
        raise AssignmentTransitionError(
            "assignment manifest must be a regular 0600 file"
        )
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
        try:
            lock.mkdir(mode=0o700)
            (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
            break
        except FileExistsError:
            owner = ""
            try:
                owner = (lock / "pid").read_text(encoding="utf-8").strip()
            except OSError:
                pass
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
    try:
        yield
    finally:
        (lock / "pid").unlink(missing_ok=True)
        try:
            lock.rmdir()
        except OSError:
            pass


def _write_manifest(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.next")
    temporary.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    temporary.replace(path)


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
            raise AssignmentTransitionError("rollback must target a prior assignment")
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
    root: Path, config: dict[str, Any]
) -> Iterator[AssignmentTransition]:
    """Hold the canonical CAS lock through materialization; commit manifest last."""
    root.mkdir(parents=True, exist_ok=True)
    with _assignment_lock(root):
        current = _read_manifest(root / "assignment.json")
        decision = _transition_decision(config, current)
        decision.root = root
        try:
            yield decision
            if not decision._committed:
                raise AssignmentTransitionError(
                    "assignment transition exited without commit"
                )
        finally:
            # Child guards may start only after this context releases the lock.
            pass
