from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prepare_process_fixture", ROOT / "tests" / "prepare_process_composition_fixture.py"
)
assert SPEC is not None and SPEC.loader is not None
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


class ProcessCompositionFixturePreparationTests(unittest.TestCase):
    def test_live_sources_and_runtime_identity_replace_base_capability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            repo = work / "repo"
            image_runtime = repo / "image-runtime"
            machine_runtime = repo / "machine-runtime"
            scripts = machine_runtime / "scripts"
            base = work / "base"
            output = work / "output"
            image_runtime.mkdir(parents=True)
            scripts.mkdir(parents=True)
            base.mkdir()
            sources = {
                "agora_image_bootstrap.py": b"bootstrap-live\n",
                "agora_boot_start.py": b"boot-live\n",
                "assignment_transition.py": b"assignment-live\n",
                "refresh_inspection.py": b"inspection-live\n",
            }
            for name, content in sources.items():
                (image_runtime / name).write_bytes(content)
                (base / name).write_bytes(b"base\n")
            heartbeat = scripts / "agora_heartbeat_agent.py"
            heartbeat.write_bytes(b"heartbeat-live\n")
            (base / "agora_heartbeat_agent.py").write_bytes(b"heartbeat-base\n")
            manifest = {
                "schemaVersion": "agora.machine-runtime-export.v1",
                "source": {"commit": "c" * 40, "tree": "d" * 40},
                "files": [
                    {
                        "path": "scripts/agora_heartbeat_agent.py",
                        "sha256": hashlib.sha256(heartbeat.read_bytes()).hexdigest(),
                    }
                ],
            }
            canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            manifest["artifactFingerprint"] = hashlib.sha256(canonical).hexdigest()
            (machine_runtime / "manifest.json").write_text(json.dumps(manifest))
            capability = {
                "schemaVersion": fixture.SCHEMA,
                **{
                    section: {"sha256": "base"}
                    for section in (
                        "bootstrap",
                        "bootStart",
                        "assignmentTransition",
                        "inspection",
                        "heartbeat",
                    )
                },
                "fleetSource": {"commit": "base"},
                "runtimeExport": {"artifactFingerprint": "base"},
                "px0": {"path": "/usr/local/bin/px0", "sha256": "unchanged"},
            }
            (base / "capability.json").write_text(json.dumps(capability))

            prepared = fixture.prepare(repo, base, output)

            self.assertEqual((output / "agora_boot_start.py").read_bytes(), b"boot-live\n")
            self.assertEqual(
                prepared["heartbeat"]["sha256"], hashlib.sha256(b"heartbeat-live\n").hexdigest()
            )
            self.assertEqual(prepared["fleetSource"], manifest["source"])
            self.assertEqual(
                prepared["runtimeExport"]["artifactFingerprint"],
                manifest["artifactFingerprint"],
            )
            self.assertEqual(prepared["px0"]["sha256"], "unchanged")


if __name__ == "__main__":
    unittest.main()
