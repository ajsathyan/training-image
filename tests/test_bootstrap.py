from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "machine-sentinel" / "start-machine-sentinel.sh"


class MachineSentinelBootstrapTests(unittest.TestCase):
    def test_cloud_identity_starts_agent_with_private_persistent_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            workspace = temporary / "workspace"
            runtime = temporary / "runtime"
            result_path = temporary / "agent-result.json"
            shutil.copytree(ROOT / "machine-sentinel" / "actions", runtime / "actions")
            (runtime / "agora_machine_sentinel_agent.py").write_text(
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "bootstrap = os.environ.pop('AGORA_SENTINEL_BOOTSTRAP_TOKEN', '')\n"
                "Path(os.environ['AGORA_TEST_RESULT']).write_text(json.dumps({"
                "'argv': sys.argv[1:], "
                "'bootstrapReceived': bool(bootstrap), "
                "'bootstrapInEnvironment': 'AGORA_SENTINEL_BOOTSTRAP_TOKEN' in os.environ"
                "}), encoding='utf-8')\n",
                encoding="utf-8",
            )
            environment = {
                **os.environ,
                "AGORA_SENTINEL_BOOTSTRAP_TOKEN": "one-time-bootstrap",
                "AGORA_SENTINEL_URL": "https://sentinel.example/api/machine-sentinel/observe",
                "AGORA_FLEET_ID": "fleet-a",
                "AGORA_LAUNCH_ID": "launch-a",
                "AGORA_RESERVATION_ID": "reservation-a",
                "AGORA_SLOT_ID": "slot-a",
                "AGORA_SLOT_GENERATION": "3",
                "AGORA_MACHINE_GENERATION_ID": "machine-generation-a",
                "AGORA_MACHINE_ID": "machine-a",
                "AGORA_TOKEN_LABEL": "alpha",
                "AGORA_NODE_TYPE": "tail",
                "AGORA_GPU_MODEL": "NVIDIA RTX PRO 6000 Blackwell Server Edition",
                "AGORA_SETUP_REVISION": "setup-v2",
                "AGORA_AUTHORITY_EPOCH": "7",
                "RUNPOD_POD_ID": "pod-a",
                "RUNPOD_POD_NAME": "agora-alpha-1",
                "AGORA_WORKSPACE_ROOT": str(workspace),
                "AGORA_SENTINEL_RUNTIME_DIR": str(runtime),
                "AGORA_SENTINEL_PYTHON": sys.executable,
                "AGORA_TEST_RESULT": str(result_path),
            }

            subprocess.run(["bash", str(LAUNCHER)], env=environment, check=True)

            state_dir = workspace / ".agora" / "machine-sentinel"
            identity = json.loads((state_dir / "identity.json").read_text(encoding="utf-8"))
            agent_result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(identity["provider"], "runpod")
            self.assertEqual(identity["providerResourceId"], "pod-a")
            self.assertEqual(identity["providerMachineId"], "pod-a")
            self.assertEqual(identity["providerMachineName"], "agora-alpha-1")
            self.assertEqual(identity["providerIdentitySource"], "provider_api")
            self.assertEqual(identity["tokenLabel"], "alpha")
            self.assertTrue(agent_result["bootstrapReceived"])
            self.assertFalse(agent_result["bootstrapInEnvironment"])
            self.assertIn("--state-file", agent_result["argv"])
            self.assertEqual(stat.S_IMODE(state_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((state_dir / "identity.json").stat().st_mode), 0o600)
            self.assertFalse((state_dir / "credential.env").exists())

            changed_environment = {**environment, "AGORA_MACHINE_ID": "machine-other"}
            changed = subprocess.run(
                ["bash", str(LAUNCHER)],
                env=changed_environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(changed.returncode, 0)
            self.assertIn("durable identity does not match", changed.stderr)
            self.assertEqual(
                json.loads((state_dir / "identity.json").read_text(encoding="utf-8"))["machineId"],
                "machine-a",
            )

    def test_missing_identity_fails_before_agent_start(self) -> None:
        environment = {
            **os.environ,
            "AGORA_SENTINEL_BOOTSTRAP_TOKEN": "one-time-bootstrap",
            "AGORA_SENTINEL_URL": "https://sentinel.example/api/machine-sentinel/observe",
        }
        result = subprocess.run(
            ["bash", str(LAUNCHER)],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 64)
        self.assertIn("AGORA_FLEET_ID", result.stderr)


if __name__ == "__main__":
    unittest.main()
