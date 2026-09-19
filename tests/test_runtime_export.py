from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from export_fleet_runtime import ExportError, export_runtime  # noqa: E402


def _fingerprint(value: dict) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class RuntimeExporterTests(unittest.TestCase):
    def test_checked_in_runtime_matches_its_generated_manifest(self) -> None:
        output = ROOT / "machine-runtime"
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        unsigned = dict(manifest)
        fingerprint = unsigned.pop("artifactFingerprint")

        self.assertEqual(manifest["schemaVersion"], "agora.machine-runtime-export.v1")
        self.assertEqual(_fingerprint(unsigned), fingerprint)
        inventory = {entry["path"] for entry in manifest["files"]}
        actual = {
            path.relative_to(output).as_posix()
            for path in output.rglob("*")
            if path.is_file()
            and path.name != "manifest.json"
            and "__pycache__" not in path.parts
            and path.suffix != ".pyc"
        }
        self.assertEqual(actual, inventory)
        for entry in manifest["files"]:
            artifact = output / entry["path"]
            self.assertEqual(
                hashlib.sha256(artifact.read_bytes()).hexdigest(), entry["sha256"]
            )

    def _source_repo(self, root: Path, *, invalid_source_hash: bool = False) -> str:
        files = {
            "scripts/agora_machine_sentinel_agent.py": b"# agent\n",
            "scripts/agora_control/execution/assets.py": b"# setup renderer\n",
            "scripts/agora_control/monitoring/remote_assets.py": b"# sentinel renderer\n",
            "scripts/agora_control/runtime_manifest.py": b"# source manifest generator\n",
            "scripts/machine_sentinel/__init__.py": b"# sentinel package\n",
            "scripts/machine_sentinel/client.py": b"# client\n",
            "scripts/machine_sentinel/event_spool.py": b"# spool\n",
            "scripts/machine_sentinel/evidence.py": b"# evidence\n",
            "scripts/machine_sentinel/observation_contract.py": b"# observation\n",
            "scripts/machine_sentinel/process_contract.py": b"# process\n",
            "scripts/machine_sentinel/sentinel.py": b"# sentinel\n",
            "scripts/machine_sentinel/state.py": b"# state\n",
            "scripts/machine_sentinel/telemetry.py": b"# telemetry\n",
            "scripts/agora_control/fleet_read_model_bootstrap.py": b"# renderer dependency\n",
            "scripts/agora_control/not-allowlisted.py": b"# must not export\n",
            ".agora/secrets/controller/fleet.json": b"secret-like state\n",
            "machine-sentinel/events.jsonl": b"runtime state\n",
        }
        for path, content in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

        sources = [
            {"path": path, "sha256": hashlib.sha256(content).hexdigest()}
            for path, content in files.items()
            if path.startswith("scripts/")
            and "/not-allowlisted.py" not in path
            and path != "scripts/agora_control/fleet_read_model_bootstrap.py"
        ]
        if invalid_source_hash:
            sources[0]["sha256"] = "0" * 64
        source_manifest = {
            "dependencyRuntimeContract": {"pythonVersion": "3.13"},
            "entrypoints": {"collector": "scripts/agora_machine_sentinel_agent.py"},
            "imageBuildStatus": "source_ready_not_built",
            "runtimeState": {"credentials": "launch-injected"},
            "schemaVersion": "agora.machine-runtime-source.v1",
            "setupRevision": "setup-test",
            "sources": sources,
        }
        source_manifest["artifactFingerprint"] = _fingerprint(source_manifest)
        manifest_path = root / "runtime/machine-runtime-source.v1.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(source_manifest), encoding="utf-8")

        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.name", "Runtime Export Test"], cwd=root, check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "runtime-export@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "source fixture"], cwd=root, check=True)
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    def test_exports_only_manifest_sources_and_renderer_dependency_deterministically(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            commit = self._source_repo(source)
            output = root / "machine-runtime"

            manifest = export_runtime(source, output, commit)
            first_export = {
                path.relative_to(output).as_posix(): path.read_bytes()
                for path in output.rglob("*")
                if path.is_file()
            }
            repeated = export_runtime(source, output, commit)
            second_export = {
                path.relative_to(output).as_posix(): path.read_bytes()
                for path in output.rglob("*")
                if path.is_file()
            }

            self.assertEqual(first_export, second_export)
            self.assertEqual(manifest, repeated)
            self.assertEqual(manifest["source"]["commit"], commit)
            self.assertRegex(manifest["source"]["tree"], r"^[0-9a-f]{40,64}$")
            self.assertEqual(
                manifest["source"]["manifestPath"],
                "runtime/machine-runtime-source.v1.json",
            )
            self.assertRegex(manifest["source"]["manifestSha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(
                manifest["source"]["artifactFingerprint"], r"^[0-9a-f]{64}$"
            )
            self.assertRegex(manifest["artifactFingerprint"], r"^[0-9a-f]{64}$")

            exported_paths = {entry["path"] for entry in manifest["files"]}
            expected_sources = {
                source_path
                for source_path in (
                    "scripts/agora_machine_sentinel_agent.py",
                    "scripts/agora_control/execution/assets.py",
                    "scripts/agora_control/monitoring/remote_assets.py",
                    "scripts/agora_control/runtime_manifest.py",
                    "scripts/machine_sentinel/__init__.py",
                    "scripts/machine_sentinel/client.py",
                    "scripts/machine_sentinel/event_spool.py",
                    "scripts/machine_sentinel/evidence.py",
                    "scripts/machine_sentinel/observation_contract.py",
                    "scripts/machine_sentinel/process_contract.py",
                    "scripts/machine_sentinel/sentinel.py",
                    "scripts/machine_sentinel/state.py",
                    "scripts/machine_sentinel/telemetry.py",
                )
            }
            expected_sources |= {
                "scripts/agora_control/fleet_read_model_bootstrap.py",
                "scripts/__init__.py",
                "scripts/agora_control/__init__.py",
                "scripts/agora_control/execution/__init__.py",
                "scripts/agora_control/monitoring/__init__.py",
            }
            self.assertEqual(exported_paths, expected_sources)
            self.assertNotIn(".agora/secrets/controller/fleet.json", first_export)
            self.assertNotIn("machine-sentinel/events.jsonl", first_export)
            self.assertNotIn("scripts/agora_control/not-allowlisted.py", first_export)

            for entry in manifest["files"]:
                artifact = output / entry["path"]
                self.assertEqual(
                    hashlib.sha256(artifact.read_bytes()).hexdigest(), entry["sha256"]
                )

    def test_rejects_a_source_hash_mismatch_before_writing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            commit = self._source_repo(source, invalid_source_hash=True)
            output = root / "machine-runtime"

            with self.assertRaisesRegex(ExportError, "source hash mismatch"):
                export_runtime(source, output, commit)

            self.assertFalse(output.exists())

    def test_refuses_to_overwrite_unowned_output_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            commit = self._source_repo(source)
            output = root / "machine-runtime"
            output.mkdir()
            (output / "operator-notes.txt").write_text("keep me", encoding="utf-8")

            with self.assertRaisesRegex(ExportError, "not owned"):
                export_runtime(source, output, commit)

            self.assertEqual(
                (output / "operator-notes.txt").read_text(encoding="utf-8"), "keep me"
            )


if __name__ == "__main__":
    unittest.main()
