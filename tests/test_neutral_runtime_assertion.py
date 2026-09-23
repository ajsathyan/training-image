from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSERTION = ROOT / "tests" / "assert-neutral-image-runtime.sh"


class NeutralRuntimeAssertionTests(unittest.TestCase):
    def test_default_receipt_tracks_managed_runtime_root(self) -> None:
        assertion = ASSERTION.read_text(encoding="utf-8")
        self.assertIn(
            "AGORA_NEUTRAL_RECEIPT_FILE:-/var/lib/agora-runtime/"
            "bootstrap-receipt.json",
            assertion,
        )

    def _run(
        self,
        *,
        state: str = "waiting_for_controller_config",
        reason: str = "no active runtime root is configured",
        unexpected_session: str = "",
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            (fake_bin / "tmux").write_text(
                "#!/usr/bin/env bash\n"
                "if [ \"${1:-}\" = has-session ] && [ \"${2:-}\" = -t ] "
                "&& [ \"${3:-}\" = \"${MOCK_TMUX_SESSION:-}\" ]; then exit 0; fi\n"
                "exit 1\n",
                encoding="utf-8",
            )
            (fake_bin / "pgrep").write_text(
                "#!/usr/bin/env bash\n"
                "case \"${3:-${2:-}}\" in sshd|cron) exit 0;; *) exit 1;; esac\n",
                encoding="utf-8",
            )
            (fake_bin / "tmux").chmod(0o755)
            (fake_bin / "pgrep").chmod(0o755)

            status = root / "status"
            status.write_text("0\n", encoding="utf-8")
            status_json = root / "status.json"
            status_json.write_text(
                json.dumps({"state": state, "reason": reason}) + "\n",
                encoding="utf-8",
            )
            bootstrap_log = root / "bootstrap.log"
            bootstrap_log.write_text(
                "agora image runtime: no active runtime root; "
                "training and inspection remain disabled\n",
                encoding="utf-8",
            )
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "MOCK_TMUX_SESSION": unexpected_session,
                "AGORA_NEUTRAL_STATUS_FILE": str(status),
                "AGORA_NEUTRAL_STATUS_JSON": str(status_json),
                "AGORA_NEUTRAL_BOOTSTRAP_LOG": str(bootstrap_log),
                "AGORA_NEUTRAL_RECEIPT_FILE": str(root / "absent-receipt.json"),
            }
            return subprocess.run(
                ["bash", str(ASSERTION)],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_exact_waiting_state_with_no_sessions_passes(self) -> None:
        self.assertEqual(self._run().returncode, 0)

    def test_each_unexpected_owned_session_fails(self) -> None:
        for session in ("agora_gpu", "agora_sentinel", "agora_px0"):
            with self.subTest(session=session):
                result = self._run(unexpected_session=session)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(session, result.stderr)

    def test_wrong_state_or_reason_fails(self) -> None:
        self.assertNotEqual(self._run(state="ready").returncode, 0)
        self.assertNotEqual(self._run(reason="different reason").returncode, 0)


if __name__ == "__main__":
    unittest.main()
