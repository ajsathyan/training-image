from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "agora_boot_start_test", ROOT / "image-runtime" / "agora_boot_start.py"
)
assert SPEC and SPEC.loader
boot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(boot)


def launch(token: str = "fixture-token") -> str:
    value = {
        "schemaVersion": boot.SCHEMA,
        "tokenSha256": hashlib.sha256(token.encode()).hexdigest(),
        "config": {
            "schemaVersion": "agora.machine-image-config.v1",
            "machineId": "n0042-alpha-1",
            "provider": "runpod",
            "accountScope": "runpod-1",
            "assignmentGeneration": 1,
            "assignmentOperationId": "operation-a",
            "remoteRoot": "/workspace/agora-run",
        },
    }
    return base64.b64encode(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).decode()


class BootStartTests(unittest.TestCase):
    def test_entrypoint_keeps_pid1_lock_but_closes_it_for_bootstrap_child(self) -> None:
        entrypoint = (ROOT / "start.sh").read_text(encoding="utf-8")
        self.assertIn("exec 9>/run/agora-image-start.lock", entrypoint)
        self.assertIn("if ! flock -n 9; then", entrypoint)
        self.assertRegex(
            entrypoint,
            r"trap - ERR\nset \+e\nenv \\\n"
            r"(?:.*\\\n)+?"
            r"\s+/opt/agora-venv/bin/python /opt/agora-image-runtime/agora_boot_start\.py \\\n"
            r"\s+9>&- \\\n\s+>/var/log/agora-image-bootstrap\.log 2>&1\n"
            r"bootstrap_rc=\$\?\nset -e\ntrap start_failure ERR",
        )
        self.assertIn("exec sleep infinity", entrypoint)

    def test_active_root_pointer_is_private_idempotent_and_generation_fenced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            pointer = base / "var" / "lib" / "agora" / "active-root.json"
            config = {
                "remoteRoot": str(base / "runtime"),
                "machineId": "machine-a",
                "provider": "runpod",
                "accountScope": "runpod-1",
                "providerResourceId": "pod-a",
                "assignmentOperationId": "operation-a",
                "assignmentGeneration": 1,
            }
            with mock.patch.object(boot, "ACTIVE_ROOT_POINTER", pointer):
                boot._record_active_root(config)
                first = pointer.read_bytes()
                boot._record_active_root(config)
                self.assertEqual(pointer.read_bytes(), first)
                self.assertEqual(pointer.stat().st_mode & 0o777, 0o600)
                self.assertEqual(pointer.parent.stat().st_mode & 0o777, 0o700)

                conflicting = {**config, "remoteRoot": str(base / "other")}
                with self.assertRaisesRegex(boot.BootInputError, "conflicts"):
                    boot._record_active_root(conflicting)

                newer = {
                    **conflicting,
                    "assignmentOperationId": "operation-b",
                    "assignmentGeneration": 2,
                }
                boot._record_active_root(newer)
                self.assertEqual(
                    json.loads(pointer.read_text())["remoteRoot"], str(base / "other")
                )

    def test_manual_restart_uses_pointer_and_never_guesses_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "runtime"
            canonical = root / "controller-input"
            canonical.mkdir(parents=True)
            (canonical / "machine-config.json").write_text(
                json.dumps(
                    {
                        "assignmentGeneration": 1,
                        "assignmentOperationId": "operation-a",
                        "remoteRoot": str(root),
                    }
                ),
                encoding="utf-8",
            )
            (canonical / "machine-config.json").chmod(0o600)
            (canonical / "hf-token").write_text("fixture-token", encoding="utf-8")
            (canonical / "hf-token").chmod(0o600)
            canonical.chmod(0o700)
            pointer = base / "active-root.json"
            pointer.write_text(
                json.dumps(
                    {
                        "schemaVersion": "agora.active-runtime-root.v1",
                        "remoteRoot": str(root),
                        "assignmentGeneration": 1,
                        "assignmentOperationId": "operation-a",
                        "identitySha256": "a" * 64,
                    }
                ),
                encoding="utf-8",
            )
            pointer.chmod(0o600)
            pointer.parent.chmod(0o700)
            status = base / "status.json"
            with (
                mock.patch.object(boot, "ACTIVE_ROOT_POINTER", pointer),
                mock.patch.object(boot, "STATUS", status),
                mock.patch.object(boot, "_restore_saved_observation", return_value=0),
                mock.patch.object(boot, "_saved_selection", return_value="stopped"),
                mock.patch.object(boot, "_root_identity_digest", return_value="a" * 64),
            ):
                self.assertEqual(boot.main({}), 0)
            self.assertEqual(json.loads(status.read_text())["state"], "stopped")

    def test_manual_restart_rejects_pointer_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "runtime"
            canonical = root / "controller-input"
            canonical.mkdir(parents=True)
            config = {
                "remoteRoot": str(root),
                "machineId": "machine-a",
                "provider": "runpod",
                "accountScope": "runpod-1",
                "providerResourceId": "pod-a",
                "assignmentOperationId": "operation-a",
                "assignmentGeneration": 1,
            }
            config_path = canonical / "machine-config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            config_path.chmod(0o600)
            token_path = canonical / "hf-token"
            token_path.write_text("fixture-token", encoding="utf-8")
            token_path.chmod(0o600)
            canonical.chmod(0o700)
            pointer = base / "var" / "lib" / "agora" / "active-root.json"
            with mock.patch.object(boot, "ACTIVE_ROOT_POINTER", pointer):
                boot._record_active_root(config)
                config_path.write_text(
                    json.dumps({**config, "providerResourceId": "pod-other"}),
                    encoding="utf-8",
                )
                config_path.chmod(0o600)
                status = base / "status.json"
                with mock.patch.object(boot, "STATUS", status):
                    self.assertEqual(boot.main({}), 0)
            value = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual(value["state"], "stopped")
            self.assertIn("corrupt", value["reason"])

    def test_no_opt_in_without_pointer_waits_for_controller(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "status.json"
            pointer = Path(directory) / "active-root.json"
            with (
                mock.patch.object(boot, "STATUS", status),
                mock.patch.object(boot, "ACTIVE_ROOT_POINTER", pointer),
            ):
                self.assertEqual(boot.main({}), 0)
            value = json.loads(status.read_text(encoding="utf-8"))
        self.assertEqual(value["state"], "waiting_for_controller_config")

    def test_malformed_opt_in_is_nonfatal_and_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "status.json"
            with (
                mock.patch.object(boot, "STATUS", status),
                mock.patch.object(boot, "_verify_boot_capability"),
            ):
                self.assertEqual(
                    boot.main(
                        {
                            "AGORA_BOOT_AUTOSTART": "1",
                            "AGORA_BOOT_LAUNCH_B64": "not-base64",
                            "AGORA_BOOT_HF_TOKEN": "never-print-this",
                        }
                    ),
                    0,
                )
            raw = status.read_text(encoding="utf-8")
        self.assertIn("launch envelope", raw)
        self.assertNotIn("never-print-this", raw)

    def test_exact_runpod_and_vast_metadata(self) -> None:
        with mock.patch.object(boot, "_metadata_sources") as sources:
            sources.return_value = [
                {"RUNPOD_POD_ID": "pod-a", "RUNPOD_TCP_PORT_49200": "35001"}
            ]
            self.assertEqual(
                boot.resolve_provider_metadata("runpod", {}), ("pod-a", 35001)
            )
            sources.return_value = [
                {"VAST_CONTAINERLABEL": "C.800001", "VAST_TCP_PORT_49200": "34920"}
            ]
            self.assertEqual(
                boot.resolve_provider_metadata("vast", {}), ("800001", 34920)
            )
            sources.return_value = [
                {"CONTAINER_ID": "800001", "VAST_CONTAINERLABEL": "C.800002", "VAST_TCP_PORT_49200": "34920"}
            ]
            with self.assertRaisesRegex(boot.BootInputError, "conflicting"):
                boot.resolve_provider_metadata("vast", {})
            sources.return_value = [
                {"CONTAINER_ID": "not-a-numeric-id", "VAST_TCP_PORT_49200": "34920"}
            ]
            with self.assertRaisesRegex(boot.BootInputError, "CONTAINER_ID"):
                boot.resolve_provider_metadata("vast", {})

    def test_retry_reopens_provider_file_written_by_another_producer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory) / "provider.env"
            status = Path(directory) / "status.json"

            def producer() -> None:
                time.sleep(0.04)
                metadata.write_text(
                    "RUNPOD_POD_ID=pod-delayed\nRUNPOD_TCP_PORT_49200=35555\n",
                    encoding="utf-8",
                )

            thread = threading.Thread(target=producer)
            thread.start()
            with (
                mock.patch.object(boot, "PROVIDER_ENV_FILES", (metadata,)),
                mock.patch.object(boot, "STATUS", status),
            ):
                result = boot.resolve_provider_metadata_with_retry(
                    "runpod", {}, wait_seconds=1, interval_seconds=0.01
                )
            thread.join()
        self.assertEqual(result, ("pod-delayed", 35555))

    def test_unreadable_optional_provider_file_is_skipped(self) -> None:
        with mock.patch.object(
            Path, "is_file", side_effect=PermissionError("not readable")
        ):
            self.assertEqual(boot._parse_env_file(Path("/root/provider.env")), {})

    def test_public_training_port_is_never_inferred_from_ssh(self) -> None:
        with mock.patch.object(
            boot,
            "_metadata_sources",
            return_value=[{"RUNPOD_POD_ID": "pod-a", "RUNPOD_TCP_PORT_22": "35000"}],
        ), self.assertRaisesRegex(boot.BootInputError, "49200"):
            boot.resolve_provider_metadata("runpod", {})

    def test_supplied_provider_values_must_match_exact_metadata(self) -> None:
        config = {"providerResourceId": "pod-old", "announcePort": 35000}
        with self.assertRaisesRegex(boot.BootInputError, "resource id conflicts"):
            boot._apply_provider_metadata(config, "pod-new", 35000)
        config = {"providerResourceId": "pod-a", "announcePort": 35000}
        with self.assertRaisesRegex(boot.BootInputError, "port conflicts"):
            boot._apply_provider_metadata(config, "pod-a", 35001)

    def test_metadata_wait_must_be_finite_and_nonnegative(self) -> None:
        for value in ("nan", "inf", "-1"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(boot.BootInputError, "duration"):
                    boot._metadata_wait_seconds(
                        {"AGORA_BOOT_METADATA_WAIT_SECONDS": value}
                    )

    def test_pause_and_newer_saved_assignment_outrank_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {
                "state": "ready",
                "assignmentGeneration": 2,
            }
            (root / "assignment.json").write_text(json.dumps(manifest))
            (root / "training-intent.json").write_text(
                json.dumps(
                    {
                        "desiredState": "paused",
                        "assignmentGeneration": 2,
                        "operationId": "saved-op",
                    }
                )
            )
            self.assertEqual(
                boot._saved_selection(
                    root, {"config": {"assignmentGeneration": 1}}
                ),
                "stopped",
            )
            (root / "training-intent.json").write_text(
                json.dumps(
                    {
                        "desiredState": "running",
                        "assignmentGeneration": 2,
                        "operationId": "saved-op",
                    }
                )
            )
            self.assertEqual(
                boot._saved_selection(
                    root, {"config": {"assignmentGeneration": 1}}
                ),
                "saved",
            )
            self.assertEqual(
                boot._saved_selection(
                    root, {"config": {"assignmentGeneration": 2}}
                ),
                "saved",
            )

    def test_bootstrap_child_environment_excludes_launch_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agora-run"
            config = {"remoteRoot": str(root)}
            observed = {}

            def run(_command, **kwargs):
                observed.update(kwargs["env"])
                return mock.Mock(returncode=0)

            with (
                mock.patch.object(boot, "STAGING_ROOT", Path(directory)),
                mock.patch.dict(
                    boot.os.environ,
                    {
                        "AGORA_BOOT_HF_TOKEN": "secret-token",
                        "AGORA_BOOT_LAUNCH_B64": "encoded-envelope",
                    },
                ),
                mock.patch.object(boot.subprocess, "run", side_effect=run),
            ):
                self.assertEqual(
                    boot._run_bootstrap(config, "secret-token", persist=True), 0
                )

            self.assertNotIn("AGORA_BOOT_HF_TOKEN", observed)
            self.assertNotIn("AGORA_BOOT_LAUNCH_B64", observed)


if __name__ == "__main__":
    unittest.main()
