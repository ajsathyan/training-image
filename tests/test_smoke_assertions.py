from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SmokeAssertionControlTests(unittest.TestCase):
    def test_shell_negative_controls(self) -> None:
        subprocess.run(
            ["bash", str(ROOT / "tests" / "test_smoke_assertions.sh")],
            cwd=ROOT,
            check=True,
        )

    def test_configured_fixture_respects_private_runtime_root_boundary(self) -> None:
        smoke = (ROOT / "tests" / "smoke-image-runtime.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "This does not prove Vast API environment injection.", smoke
        )
        post_boot = smoke.split('configured_port="$(host_port "$configured")"', 1)[1]
        configured_fixture = post_boot.split(
            "# A separate fresh container exercises", 1
        )[0]
        self.assertNotIn('"$state/', configured_fixture)
        self.assertIn('docker exec "$configured" cat', configured_fixture)
        self.assertIn('docker cp "$work/configured-newer-fence.json"', configured_fixture)
        self.assertIn('docker cp "$work/configured-rollback-config.json"', configured_fixture)
        self.assertIn('docker cp "$work/configured-stage-config.json"', configured_fixture)
        self.assertIn(
            'config_hash="$(docker exec "$configured" sha256sum '
            '/workspace/agora-run/controller-input/machine-config.json',
            configured_fixture,
        )

    def test_delayed_replay_waits_for_each_exact_child(self) -> None:
        smoke = (ROOT / "tests" / "smoke-image-runtime.sh").read_text(
            encoding="utf-8"
        )
        delayed_fixture = smoke.split(
            "# Exercise the image entrypoint timing", 1
        )[1].split('docker run -d --name "$training"', 1)[0]

        self.assertNotIn("\nwait\n", delayed_fixture)
        self.assertIn('delayed_replay_pids+=("$!")', delayed_fixture)
        self.assertIn(
            '"delayed Vast concurrent replay" "${delayed_replay_pids[@]}"',
            delayed_fixture,
        )
        self.assertIn("timeout -k 5s 120s docker exec", delayed_fixture)
        self.assertIn(
            '.assignmentOperationId == "assignment-operation-delayed-vast"',
            delayed_fixture,
        )
        self.assertIn(".assignmentGeneration == 1", delayed_fixture)


if __name__ == "__main__":
    unittest.main()
