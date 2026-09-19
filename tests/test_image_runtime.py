from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = load_module(
    "agora_image_bootstrap_test", ROOT / "image-runtime" / "agora_image_bootstrap.py"
)


def valid_config(root: Path) -> dict[str, object]:
    return {
        "schemaVersion": "agora.machine-image-config.v1",
        "machineId": "machine-a",
        "provider": "runpod",
        "accountScope": "account-a",
        "providerResourceId": "pod-a",
        "tokenLabel": "alice",
        "tokenInstance": 3,
        "assignmentGeneration": 4,
        "assignmentOperationId": "assignment-op-a",
        "trainingSessionId": "assignment-op-a",
        "runId": "run-a",
        "gpuModel": "NVIDIA RTX PRO 6000 Blackwell Server Edition",
        "nodeType": "tail",
        "provisioningOrigin": "existing_rental",
        "hostPort": 49200,
        "announcePort": 55001,
        "remoteRoot": str(root),
        "startTraining": False,
        "px0Enabled": True,
        "assignmentTransition": {
            "kind": "stage",
            "allowAbsent": True,
            "expectedManifest": None,
        },
        "sentinel": {"mode": "local", "url": ""},
        "heartbeat": {"mode": "disabled", "url": ""},
    }


class ImageBootstrapContractTests(unittest.TestCase):
    def test_configured_heartbeat_stages_baked_assets_from_separate_secret(
        self,
    ) -> None:
        class FakeAssets:
            class ScriptRenderers:
                def __init__(self, **_kwargs):
                    pass

            @staticmethod
            def render_baked_heartbeat_runtime_bundle(_machine, _heartbeat, *, render):
                del render
                return {
                    "start-agora-heartbeat.sh": "#!/bin/sh\n",
                    "watchdog-heartbeat-tmux.sh": "#!/bin/sh\n",
                    "watch-heartbeat-tmux-loop.sh": "#!/bin/sh\n",
                    "install-heartbeat.sh": "#!/bin/sh\n",
                }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agora-run"
            controller_input = root / "controller-input"
            controller_input.mkdir(parents=True)
            secret = controller_input / "heartbeat-machine-secret"
            secret.write_text("AGORA_HEARTBEAT_SECRET=fixture\n", encoding="utf-8")
            secret.chmod(0o600)
            config = valid_config(root)
            config["heartbeat"] = {
                "mode": "configured",
                "url": "https://heartbeat.example.invalid/observe",
                "secretFile": str(secret),
                "role": "tail",
                "tokenLabel": "alice",
                "runpodPodId": "pod-a",
                "runpodDcId": "dc-a",
                "intervalSeconds": 10,
                "jitterSeconds": 0,
                "timeoutSeconds": 5,
            }
            normalized, _ = bootstrap._validated(config, "token")
            result = bootstrap._heartbeat(root, normalized, FakeAssets)

            self.assertEqual(result, {"requested": True, "status": "staged"})
            self.assertTrue((root / "install-heartbeat.sh").is_file())
            self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)

    def test_declared_runtime_fingerprint_must_match_baked_capability(self) -> None:
        capability = {
            "runtimeExport": {"artifactFingerprint": "a" * 64},
        }
        config = {
            "imageCapability": {
                "contractVersion": "agora.machine-image-capability.v1",
                "runtimeArtifactFingerprint": "a" * 64,
            }
        }
        bootstrap._verify_declared_capability(config, capability)
        config["imageCapability"]["runtimeArtifactFingerprint"] = "b" * 64
        with self.assertRaisesRegex(bootstrap.BootstrapError, "different runtime"):
            bootstrap._verify_declared_capability(config, capability)

    def test_existing_private_identity_requires_controller_digest_or_same_assignment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = valid_config(root)
            identity = root / "private_gpu0.key"
            identity.write_text("private-identity", encoding="utf-8")
            identity.chmod(0o600)
            with self.assertRaisesRegex(bootstrap.BootstrapError, "not owned"):
                bootstrap._check_private_identity(root, config)
            (root / "machine.json").write_text(json.dumps(config), encoding="utf-8")
            adopted = bootstrap._check_private_identity(root, config)
            self.assertEqual(adopted["status"], "adopted_same_assignment")
            self.assertTrue((root / "private-identity.json").is_file())
            self.assertEqual(
                stat.S_IMODE((root / "private-identity.json").stat().st_mode), 0o600
            )
            digest = hashlib.sha256(identity.read_bytes()).hexdigest()
            result = bootstrap._check_private_identity(
                root, {**config, "identityBackupSha256": digest}
            )
            self.assertEqual(result, {"status": "verified_existing", "sha256": digest})

            reassigned = {
                **config,
                "assignmentGeneration": 5,
                "assignmentOperationId": "assignment-op-b",
                "identityBackupSha256": digest,
            }
            bootstrap._check_existing_identity(root, reassigned)
            retained = bootstrap._check_private_identity(root, reassigned)
            marker = json.loads(
                (root / "private-identity.json").read_text(encoding="utf-8")
            )
            self.assertEqual(retained["status"], "verified_existing")
            self.assertEqual(marker["assignmentGeneration"], 5)
            self.assertEqual(marker["assignmentOperationId"], "assignment-op-b")

    def test_real_gpu_name_and_independent_assignment_operation_are_preserved(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = valid_config(Path(directory) / "agora run")
            normalized, _root = bootstrap._validated(config, "hf token with spaces '$")

        self.assertEqual(normalized["gpuModel"], config["gpuModel"])
        self.assertEqual(normalized["assignmentOperationId"], "assignment-op-a")
        self.assertEqual(normalized["trainingSessionId"], "assignment-op-a")

    def test_runtime_source_drift_requires_assignment_bound_repair_provenance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            source = Path(directory) / "source"
            root.mkdir()
            source.mkdir()
            config = valid_config(root)
            build_commit = "1" * 40
            repaired_commit = "2" * 40
            capability = {"trainingSource": {"commit": build_commit}}
            original_source = bootstrap.TRAINING_SOURCE
            bootstrap.TRAINING_SOURCE = source
            try:
                with self.assertRaisesRegex(bootstrap.BootstrapError, "no approved"):
                    bootstrap._runtime_training_provenance(
                        root, config, capability, repaired_commit
                    )
                machine = {**config, "agoraCommit": repaired_commit}
                (root / "machine.json").write_text(
                    json.dumps(machine), encoding="utf-8"
                )
                marker = {
                    "schemaVersion": "agora.client-repair-provenance.v1",
                    "beforeCommit": build_commit,
                    "afterCommit": repaired_commit,
                    "machineId": config["machineId"],
                    "provider": config["provider"],
                    "accountScope": config["accountScope"],
                    "providerResourceId": config["providerResourceId"],
                    "assignmentGeneration": config["assignmentGeneration"],
                    "assignmentOperationId": config["assignmentOperationId"],
                    "sourcePath": str(source.resolve()),
                }
                marker_path = root / "agora-client-repair-provenance.json"
                marker_path.write_text(json.dumps(marker), encoding="utf-8")
                marker_path.chmod(0o600)
                provenance = bootstrap._runtime_training_provenance(
                    root, config, capability, repaired_commit
                )
                self.assertEqual(provenance["status"], "approved_repair")
                self.assertEqual(provenance["runtimeCommit"], repaired_commit)

                marker["assignmentOperationId"] = "wrong-assignment"
                marker_path.write_text(json.dumps(marker), encoding="utf-8")
                with self.assertRaisesRegex(bootstrap.BootstrapError, "does not bind"):
                    bootstrap._runtime_training_provenance(
                        root, config, capability, repaired_commit
                    )
            finally:
                bootstrap.TRAINING_SOURCE = original_source

    def test_newer_assignment_is_allowed_but_stale_or_conflicting_is_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = valid_config(root)
            (root / "machine.json").write_text(json.dumps(current), encoding="utf-8")

            newer = {
                **current,
                "assignmentGeneration": 5,
                "assignmentOperationId": "assignment-op-b",
            }
            bootstrap._check_existing_identity(root, newer)

            stale = {**current, "assignmentGeneration": 3}
            with self.assertRaisesRegex(bootstrap.BootstrapError, "older"):
                bootstrap._check_existing_identity(root, stale)

            conflict = {**current, "tokenLabel": "bob"}
            with self.assertRaisesRegex(bootstrap.BootstrapError, "conflicts"):
                bootstrap._check_existing_identity(root, conflict)

    def test_exact_newer_fence_authorizes_prior_identity_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prior = valid_config(root)
            newer = {
                **prior,
                "assignmentGeneration": 5,
                "assignmentOperationId": "assignment-op-newer",
                "tokenLabel": "newer-label",
                "tokenInstance": 4,
            }
            newer_binding = {
                "schemaVersion": 1,
                "state": "fenced",
                "operationId": newer["assignmentOperationId"],
                "assignmentGeneration": newer["assignmentGeneration"],
                "tokenLabel": newer["tokenLabel"],
                "tokenInstance": newer["tokenInstance"],
                "machineId": newer["machineId"],
                "provider": newer["provider"],
                "accountScope": newer["accountScope"],
                "providerResourceId": newer["providerResourceId"],
                "tokenSha256": "5" * 64,
            }
            (root / "assignment.json").write_text(
                json.dumps(newer_binding), encoding="utf-8"
            )
            (root / "assignment.json").chmod(0o600)
            (root / "machine.json").write_text(json.dumps(newer), encoding="utf-8")
            identity = root / "private_gpu0.key"
            identity.write_text("identity", encoding="utf-8")
            identity.chmod(0o600)
            digest = hashlib.sha256(identity.read_bytes()).hexdigest()
            marker = {
                "schemaVersion": "agora.private-identity-ownership.v1",
                "sha256": digest,
                "machineId": newer["machineId"],
                "provider": newer["provider"],
                "accountScope": newer["accountScope"],
                "providerResourceId": newer["providerResourceId"],
                "assignmentGeneration": newer["assignmentGeneration"],
                "assignmentOperationId": newer["assignmentOperationId"],
            }
            (root / "private-identity.json").write_text(
                json.dumps(marker), encoding="utf-8"
            )
            (root / "private-identity.json").chmod(0o600)
            prior.update(
                tokenSha256="4" * 64,
                identityBackupSha256=digest,
                assignmentTransition={
                    "kind": "rollback_prior",
                    "allowAbsent": False,
                    "expectedManifest": newer_binding,
                },
            )
            with bootstrap.assignment_transition(root, prior) as decision:
                self.assertTrue(decision.rollback_authorized)
                bootstrap._check_existing_identity(
                    root, prior, rollback_authorized=decision.rollback_authorized
                )
                retained = bootstrap._check_private_identity(root, prior)
                source = root / "source"
                source.mkdir()
                original_source = bootstrap.TRAINING_SOURCE
                bootstrap.TRAINING_SOURCE = source
                repaired_commit = "2" * 40
                build_commit = "1" * 40
                machine = json.loads(
                    (root / "machine.json").read_text(encoding="utf-8")
                )
                machine["agoraCommit"] = repaired_commit
                (root / "machine.json").write_text(
                    json.dumps(machine), encoding="utf-8"
                )
                repair_marker = {
                    "schemaVersion": "agora.client-repair-provenance.v1",
                    "beforeCommit": build_commit,
                    "afterCommit": repaired_commit,
                    "machineId": prior["machineId"],
                    "provider": prior["provider"],
                    "accountScope": prior["accountScope"],
                    "providerResourceId": prior["providerResourceId"],
                    "assignmentGeneration": prior["assignmentGeneration"],
                    "assignmentOperationId": prior["assignmentOperationId"],
                    "sourcePath": str(source.resolve()),
                }
                repair_path = root / "agora-client-repair-provenance.json"
                repair_path.write_text(json.dumps(repair_marker), encoding="utf-8")
                repair_path.chmod(0o600)
                try:
                    provenance = bootstrap._runtime_training_provenance(
                        root,
                        prior,
                        {"trainingSource": {"commit": build_commit}},
                        repaired_commit,
                        current_assignment=decision.current,
                        rollback_authorized=decision.rollback_authorized,
                    )
                finally:
                    bootstrap.TRAINING_SOURCE = original_source
                restored = decision.commit()

            self.assertEqual(retained["status"], "verified_existing")
            self.assertEqual(provenance["status"], "approved_repair")
            self.assertEqual(restored["assignmentGeneration"], 4)
            self.assertEqual(restored["state"], "fenced")

    def test_materialized_environment_shell_quotes_secret_and_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agora image ") as directory:
            root = Path(directory) / "agora run"
            config, _ = bootstrap._validated(
                valid_config(root), "hf token with spaces '$"
            )
            bootstrap._materialize_state(
                root, config, "hf token with spaces '$", "1" * 40
            )
            mode = stat.S_IMODE((root / "agora.env").stat().st_mode)
            command = (
                'set -a; source "$0"; '
                f'{sys.executable} -c \'import json,os; print(json.dumps([os.environ["HF_TOKEN"],os.environ["HF_HOME"]]))\''
            )
            result = subprocess.run(
                ["bash", "-c", command, str(root / "agora.env")],
                check=True,
                capture_output=True,
                text=True,
            )
            values = json.loads(result.stdout)

        self.assertEqual(mode, 0o600)
        self.assertEqual(values[0], "hf token with spaces '$")
        self.assertIn("agora image ", values[1])

    def test_exported_renderer_produces_complete_baked_source_bundle(self) -> None:
        original_runtime = bootstrap.RUNTIME_DIR
        original_source = bootstrap.TRAINING_SOURCE
        try:
            bootstrap.RUNTIME_DIR = ROOT / "machine-runtime"
            bootstrap.TRAINING_SOURCE = Path("/opt/agora-source")
            assets, _remote_assets = bootstrap._load_runtime_modules()
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "agora-run"
                controller_input = root / "controller-input"
                controller_input.mkdir(parents=True)
                heartbeat_secret = controller_input / "heartbeat-machine-secret"
                heartbeat_secret.write_text(
                    "AGORA_HEARTBEAT_SECRET=fixture\n", encoding="utf-8"
                )
                heartbeat_secret.chmod(0o600)
                raw_config = valid_config(root)
                raw_config["heartbeat"] = {
                    "mode": "configured",
                    "url": "https://heartbeat.example.invalid/observe",
                    "secretFile": str(heartbeat_secret),
                    "role": "tail",
                    "tokenLabel": "alice",
                    "runpodPodId": "pod-a",
                    "runpodDcId": "dc-a",
                    "intervalSeconds": 10,
                    "jitterSeconds": 0,
                    "timeoutSeconds": 5,
                }
                config, _ = bootstrap._validated(raw_config, "hf-fixture")
                bootstrap._render_training_assets(root, config, assets)
                training_expected = {
                    "assignment-start-guard.sh",
                    "launch-agora-gpu0.sh",
                    "repair-agora-client.sh",
                    "supervise-agora-gpu0.sh",
                    "install-watchdog.sh",
                }
                heartbeat_result = bootstrap._heartbeat(root, config, assets)
                heartbeat_expected = {
                    "start-agora-heartbeat.sh",
                    "watchdog-heartbeat-tmux.sh",
                    "watch-heartbeat-tmux-loop.sh",
                    "install-heartbeat.sh",
                }
                expected = training_expected | heartbeat_expected | {"controller-input"}
                rendered = {path.name for path in root.iterdir()}
                self.assertEqual(rendered, expected)
                self.assertEqual(
                    heartbeat_result, {"requested": True, "status": "staged"}
                )
                for name in training_expected | heartbeat_expected:
                    content = (root / name).read_text(encoding="utf-8")
                    if name in training_expected:
                        self.assertIn("/opt/agora-source", content)
                    syntax = subprocess.run(
                        ["bash", "-n"], input=content, text=True, capture_output=True
                    )
                    self.assertEqual(syntax.returncode, 0, syntax.stderr)
        finally:
            bootstrap.RUNTIME_DIR = original_runtime
            bootstrap.TRAINING_SOURCE = original_source


class InspectionRootTests(unittest.TestCase):
    def test_refresh_builds_regular_file_only_non_secret_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "agora-run"
            logs = root / "logs"
            inspection = base / "inspection"
            logs.mkdir(parents=True)
            inspection.mkdir()
            machine = valid_config(root)
            (root / "machine.json").write_text(json.dumps(machine), encoding="utf-8")
            (root / "progress.log").write_text("progress\n", encoding="utf-8")
            os.mkfifo(root / "setup.log", mode=0o600)
            server_log = logs / "server_gpu0.log"
            server_log.write_text("server\n" * 1000, encoding="utf-8")
            source_mtime_ns = 978307200_123_000_000
            os.utime(server_log, ns=(source_mtime_ns, source_mtime_ns))
            (root / "agora.env").write_text("HF_TOKEN=secret\n", encoding="utf-8")
            (inspection / "secret-link").symlink_to(root / "agora.env")

            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "image-runtime" / "refresh_inspection.py"),
                    "--root",
                    str(root),
                    "--inspection-root",
                    str(inspection),
                    "--owner",
                    f"{os.getuid()}:{os.getgid()}",
                    "--max-bytes",
                    "4096",
                ],
                check=True,
            )
            metadata = json.loads(
                (inspection / "inspection-metadata.json").read_text(encoding="utf-8")
            )
            server_row = next(
                row for row in metadata["logs"] if row["name"] == "server_gpu0.log"
            )
            server_log.unlink()
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "image-runtime" / "refresh_inspection.py"),
                    "--root",
                    str(root),
                    "--inspection-root",
                    str(inspection),
                    "--owner",
                    f"{os.getuid()}:{os.getgid()}",
                    "--max-bytes",
                    "4096",
                ],
                check=True,
            )
            names = {path.name for path in inspection.iterdir()}
            inspection_mode = stat.S_IMODE(inspection.stat().st_mode)

        self.assertEqual(inspection_mode, 0o750)
        self.assertNotIn("secret-link", names)
        self.assertNotIn("agora.env", names)
        self.assertEqual(names, {"progress.log", "inspection-metadata.json"})
        self.assertEqual(metadata["machineId"], "machine-a")
        self.assertEqual(metadata["mtimeTimezone"], "UTC")
        self.assertEqual(server_row["modifiedAt"], "2001-01-01T00:00:00.123Z")
        self.assertTrue(server_row["truncated"])
        self.assertEqual(server_row["copiedSizeBytes"], 4096)


if __name__ == "__main__":
    unittest.main()
