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
    def test_no_opt_in_stays_manual(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "status.json"
            with mock.patch.object(boot, "STATUS", status):
                self.assertEqual(boot.main({}), 0)
            value = json.loads(status.read_text(encoding="utf-8"))
        self.assertEqual(value["state"], "manual")

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
