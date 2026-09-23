from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(
        name,
        path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


transition = load_module(
    "assignment_transition_test", ROOT / "image-runtime" / "assignment_transition.py"
)
image_runtime = load_module(
    "fleet_image_runtime_transition_test",
    ROOT / "machine-runtime" / "scripts" / "agora_control" / "execution" / "image_runtime.py",
)
RUNTIME_FINGERPRINT = json.loads(
    (ROOT / "machine-runtime" / "manifest.json").read_text(encoding="utf-8")
)["artifactFingerprint"]


def config(
    root: Path,
    *,
    generation: int = 1,
    operation: str = "operation-1",
    kind: str = "stage",
    expected: dict[str, object] | None = None,
):
    machine = {
        "remoteRoot": str(root),
        "assignmentOperationId": operation,
        "assignmentGeneration": generation,
        "tokenLabel": f"label-{generation}",
        "tokenInstance": generation,
        "id": "machine-1",
        "provider": "runpod",
        "accountScope": "account-1",
        "providerResourceId": "pod-1",
        "runId": "run-1",
        "gpuModel": "NVIDIA RTX PRO 6000 Blackwell Server Edition",
        "agoraJoinRole": "tail",
        "announcePort": 55001,
        "imageCapability": {
            "contractVersion": "agora.machine-image-capability.v1",
            "runtimeArtifactFingerprint": RUNTIME_FINGERPRINT,
        },
    }
    return image_runtime.build_machine_image_config(
        machine,
        token_sha256=str(generation) * 64,
        start_training=kind == "ready",
        sentinel={"url": "", "exportEnabled": False},
        assignment_transition={
            "kind": kind,
            "allowAbsent": expected is None and kind != "rollback_prior",
            "expectedManifest": expected,
        },
    )


def manifest(value: dict[str, object], state: str) -> dict[str, object]:
    return image_runtime.build_assignment_manifest(
        value, token_sha256=str(value["tokenSha256"]), state=state
    )


def write_manifest(root: Path, value: dict[str, object]) -> None:
    path = root / "assignment.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


class AssignmentTransitionTests(unittest.TestCase):
    def test_failed_ready_fence_stops_before_any_durable_publication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            value = config(root, kind="ready")
            write_manifest(root, manifest(value, "ready"))
            calls: list[str] = []

            def stop_owned() -> bool:
                calls.append("stop")
                return True

            with mock.patch.object(
                transition,
                "private_atomic_write",
                side_effect=transition.AssignmentTransitionError("disk full"),
            ):
                with self.assertRaisesRegex(
                    transition.AssignmentTransitionError, "disk full"
                ):
                    transition.fence_failed_ready_assignment(
                        root, value, stop_owned=stop_owned
                    )
            self.assertEqual(calls, ["stop"])

    def test_private_preflight_repairs_modes_before_first_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            root.mkdir(mode=0o777)
            transition.preflight_private_root(root)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            destination = root / "private.json"
            transition.private_atomic_write(destination, b"{}\n")
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)

    def test_private_writes_reject_symlinks_and_existing_temporary_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            actual = base / "actual"
            actual.mkdir(mode=0o700)
            linked = base / "linked"
            linked.symlink_to(actual, target_is_directory=True)
            with self.assertRaisesRegex(
                transition.AssignmentTransitionError, "ancestors"
            ):
                transition.private_atomic_write(linked / "secret", b"secret")

            root = base / "runtime"
            root.mkdir(mode=0o700)
            dangling = root / "secret"
            dangling.symlink_to(root / "missing")
            with self.assertRaisesRegex(
                transition.AssignmentTransitionError, "destination"
            ):
                transition.private_atomic_write(dangling, b"secret")

            dangling.unlink()
            temporary = root / f".secret.{os.getpid()}.next"
            temporary.write_text("occupied", encoding="utf-8")
            with self.assertRaisesRegex(
                transition.AssignmentTransitionError, "temporary"
            ):
                transition.private_atomic_write(root / "secret", b"secret")

    def test_private_preflight_fails_when_final_mode_cannot_be_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            with mock.patch.object(transition.os, "chmod", side_effect=OSError("denied")):
                with self.assertRaisesRegex(
                    transition.AssignmentTransitionError, "cannot enforce mode"
                ):
                    transition.preflight_private_root(root)

    def test_workspace_named_root_is_decided_by_real_mode_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace" / "agora-run"
            transition.preflight_private_root(root)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace" / "agora-run"
            with mock.patch.object(
                transition.os,
                "chmod",
                side_effect=OSError("provider filesystem refused private mode"),
            ):
                with self.assertRaisesRegex(
                    transition.AssignmentTransitionError, "cannot enforce mode"
                ):
                    transition.preflight_private_root(root)
            self.assertFalse((root / "assignment.json").exists())

    def test_stage_ready_and_same_binding_replay_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged_config = config(root)
            with transition.assignment_transition(root, staged_config) as decision:
                self.assertFalse(decision.idempotent)
                staged = decision.commit()
            self.assertEqual(staged["state"], "staged")

            ready_config = config(root, kind="ready", expected=staged)
            with transition.assignment_transition(root, ready_config) as decision:
                ready = decision.commit()
            self.assertEqual(ready["state"], "ready")

            with transition.assignment_transition(root, ready_config) as replay:
                self.assertTrue(replay.idempotent)
                replay.commit()
            self.assertEqual(
                stat.S_IMODE((root / "assignment.json").stat().st_mode), 0o600
            )
            self.assertFalse((root / "assignment.lock").exists())
            verified = transition.verify_assignment_postcondition(
                root, ready_config, required_state="ready"
            )
            self.assertEqual(verified["operationId"], "operation-1")

            delayed_stage_config = config(root, kind="stage", expected=ready)
            with transition.assignment_transition(
                root, delayed_stage_config
            ) as delayed_stage:
                self.assertTrue(delayed_stage.idempotent)
                self.assertEqual(delayed_stage.target_state, "ready")
                delayed_stage.commit()
            self.assertEqual(
                json.loads((root / "assignment.json").read_text())["state"],
                "ready",
            )

            newer = config(root, generation=2, operation="operation-2")
            write_manifest(root, manifest(newer, "fenced"))
            with self.assertRaisesRegex(
                transition.AssignmentTransitionError, "postcondition"
            ):
                transition.verify_assignment_postcondition(
                    root, ready_config, required_state="ready"
                )

    def test_concurrent_newer_fence_rejects_stale_absent_precondition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requested = config(root)
            lock = root / "assignment.lock"
            lock.mkdir()
            (lock / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
            (lock / "pid").chmod(0o600)
            outcome: list[BaseException | str] = []

            def run_transition() -> None:
                try:
                    with transition.assignment_transition(root, requested) as decision:
                        decision.commit()
                    outcome.append("committed")
                except BaseException as exc:  # captured for deterministic assertion
                    outcome.append(exc)

            worker = threading.Thread(target=run_transition)
            worker.start()
            time.sleep(0.1)
            newer = config(root, generation=2, operation="operation-2")
            write_manifest(root, manifest(newer, "fenced"))
            (lock / "pid").unlink()
            lock.rmdir()
            worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            self.assertEqual(len(outcome), 1)
            self.assertIsInstance(outcome[0], transition.AssignmentTransitionError)
            self.assertIn("precondition", str(outcome[0]))
            persisted = json.loads(
                (root / "assignment.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["assignmentGeneration"], 2)
            self.assertEqual(persisted["operationId"], "operation-2")
            self.assertEqual(persisted["state"], "fenced")

    def test_authorized_prior_generation_rollback_requires_exact_newer_fence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            newer = config(root, generation=2, operation="operation-2")
            newer_fence = manifest(newer, "fenced")
            write_manifest(root, newer_fence)
            prior = config(root, kind="rollback_prior", expected=newer_fence)
            with transition.assignment_transition(root, prior) as decision:
                self.assertTrue(decision.rollback_authorized)
                restored = decision.commit()
            self.assertEqual(restored["assignmentGeneration"], 1)
            self.assertEqual(restored["operationId"], "operation-1")
            self.assertEqual(restored["state"], "fenced")

            write_manifest(root, newer_fence)
            wrong_fence = {**newer_fence, "operationId": "wrong-operation"}
            prior = config(root, kind="rollback_prior", expected=wrong_fence)
            with self.assertRaisesRegex(
                transition.AssignmentTransitionError, "precondition"
            ):
                with transition.assignment_transition(root, prior) as decision:
                    decision.commit()


if __name__ == "__main__":
    unittest.main()
