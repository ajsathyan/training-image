"""Real-Bash startup rehearsal with isolated paths and external-command stubs.

The PID1 test rewrites only the lifecycle predicate: it models that branch, not
real container PID1. The existing container smoke remains the real PID1 proof.
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
START = (ROOT / "start.sh").read_text(encoding="utf-8")
try:
    BASE = subprocess.check_output(
        ["git", "show", "c2ff2e013d3afabf608bf0f58be339885dcb1015:start.sh"],
        cwd=ROOT,
        text=True,
        stderr=subprocess.DEVNULL,
    )
except (OSError, subprocess.CalledProcessError):
    BASE = None  # A shallow CI checkout may omit the historical commit.
MARKER = re.compile(r"^stage=[a-z0-9_]+ rc=\d+ line=\d+ pid=\d+ ppid=\d+$")


def executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


class StartFixture:
    def __init__(self, base: Path, source: str = START, *, modeled_pid1: bool = False):
        self.base = base
        self.bin = base / "bin"
        self.bin.mkdir(parents=True)
        self.env = os.environ.copy()
        self.env.update(PATH=f"{self.bin}:{self.env['PATH']}", AGORA_TEST_BASE=str(base))
        paths = {
            "/run/sshd": base / "run" / "sshd",
            "/root/.ssh": base / "root" / ".ssh",
            "/root/.bashrc": base / "root" / ".bashrc",
            "/var/log/agora-vast-onstart.log": base / "log" / "onstart.log",
            "/var/log/agora-image-bootstrap.log": base / "log" / "bootstrap.log",
            "/run/agora-image-start.lock": base / "run" / "start.lock",
            "/run/agora-image-bootstrap.status.json": base / "run" / "status.json",
            "/run/agora-image-bootstrap.status": base / "run" / "status",
            "/etc/rp_environment": base / "etc" / "rp_environment",
            "/etc/agora_environment": base / "etc" / "agora_environment",
            "/etc/.agora_environment.XXXXXX": base / "etc" / ".agora_environment.XXXXXX",
            "/opt/agora-venv/bin/python": base / "bin" / "bootstrap",
            "/opt/agora-image-runtime/agora_boot_start.py": base / "runtime" / "agora_boot_start.py",
            "/usr/sbin/sshd": base / "bin" / "sshd",
        }
        for old, new in sorted(paths.items(), key=lambda item: len(item[0]), reverse=True):
            source = source.replace(old, str(new))
        if modeled_pid1:
            old = '"$$" -ne 1'
            assert source.count(old) == 1
            source = source.replace(old, '"${AGORA_TEST_PID1:-0}" -ne 1')
            self.env["AGORA_TEST_PID1"] = "1"
        self.script = base / "start.sh"
        executable(self.script, source)
        (base / "root").mkdir()
        (base / "etc").mkdir()
        (base / "log").mkdir()
        (base / "runtime").mkdir()
        (base / "run").mkdir()
        (base / "root" / ".bashrc").write_text("", encoding="utf-8")
        (base / "runtime" / "agora_boot_start.py").write_text("", encoding="utf-8")
        executable(self.bin / "install", '#!/bin/sh\nfor last do :; done\nmkdir -p "$last"\n')
        executable(self.bin / "chown", "#!/bin/sh\nexit 0\n")
        executable(self.bin / "ssh-keygen", "#!/bin/sh\nexit 0\n")
        executable(self.bin / "pgrep", "#!/bin/sh\nexit 1\n")
        executable(self.bin / "service", "#!/bin/sh\nexit 0\n")
        executable(self.bin / "cron", "#!/bin/sh\nexit 0\n")
        executable(
            self.bin / "sshd",
            '#!/bin/sh\nif [ "$1" = -t ]; then exit "${AGORA_TEST_SSHD_RC:-0}"; fi\nexit 0\n',
        )
        executable(
            self.bin / "bootstrap",
            '#!/bin/sh\n'
            'printf "%s\\n" "${AGORA_BOOT_AUTOSTART:-neutral}" >> "$AGORA_TEST_BASE/boot-modes"\n'
            'printf "%s\\n" "$AGORA_BOOT_HF_TOKEN"\n'
            'exit "${AGORA_TEST_BOOT_RC:-0}"\n',
        )
        executable(
            self.bin / "flock",
            "#!/usr/bin/env python3\n"
            "import fcntl, sys\n"
            "fd = int(sys.argv[-1])\n"
            "mode = fcntl.LOCK_UN if sys.argv[1] == '-u' else fcntl.LOCK_EX | fcntl.LOCK_NB\n"
            "fcntl.flock(fd, mode)\n",
        )
        executable(
            self.bin / "sleep",
            '#!/bin/sh\nprintf "%s\\n" "$$" > "$AGORA_TEST_BASE/keepalive-pid"\n'
            'exec /bin/sleep 30\n',
        )

    def run(self, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/bash", str(self.script)],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=8,
        )

    def status(self) -> str:
        return (self.base / "run" / "status").read_text(encoding="utf-8").strip()

    def stages(self, result: subprocess.CompletedProcess[str]) -> list[str]:
        lines = result.stderr.splitlines()
        assert all(MARKER.fullmatch(line) for line in lines), lines
        return [line.split()[0].removeprefix("stage=") for line in lines]


class StartDiagnosticsTests(unittest.TestCase):
    def test_pre_ssh_markers_new_only_and_neutral_helper_order(self) -> None:
        if BASE is not None:
            with tempfile.TemporaryDirectory() as directory:
                old = StartFixture(Path(directory) / "old", BASE)
                old_result = old.run(AGORA_BOOT_HF_TOKEN="old-secret")
                self.assertEqual(old_result.returncode, 0, old_result.stderr)
                self.assertNotIn("stage=", old_result.stderr)
        with tempfile.TemporaryDirectory() as directory:
            fixture = StartFixture(Path(directory) / "new")
            result = fixture.run(AGORA_BOOT_HF_TOKEN="new-secret")
            self.assertEqual(result.returncode, 0, result.stderr)
            stages = fixture.stages(result)
            self.assertEqual(
                stages,
                ["startup_entered", "key_setup_ready", "sshd_ready", "cron_ready",
                 "environment_ready", "bootstrap_lock_acquired", "bootstrap_started",
                 "bootstrap_complete", "bootstrap_lock_released", "helper_exit", "start_sh_exit"],
            )
            self.assertEqual(fixture.status(), "0")
            self.assertEqual((fixture.base / "boot-modes").read_text(), "neutral\n")
            self.assertIn("stage=sshd_ready", (fixture.base / "log" / "onstart.log").read_text())

    def test_assigned_replay_releases_lock_and_never_exposes_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = StartFixture(Path(directory))
            secrets = ["space secret", "quote'\"secret", "dollar$secret", "line1\nline2"]
            for secret in secrets:
                result = fixture.run(AGORA_BOOT_AUTOSTART="1", AGORA_BOOT_HF_TOKEN=secret)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(fixture.status(), "0")
                self.assertLess(
                    fixture.stages(result).index("bootstrap_complete"),
                    fixture.stages(result).index("bootstrap_lock_released"),
                )
                self.assertNotIn(secret, result.stderr)
                self.assertNotIn(secret, (fixture.base / "log" / "onstart.log").read_text())
                with (fixture.base / "run" / "start.lock").open() as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(lock, fcntl.LOCK_UN)
            self.assertEqual((fixture.base / "boot-modes").read_text(), "1\n" * 4)

    def test_err_and_explicit_safety_exit_preserve_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = StartFixture(Path(directory))
            result = fixture.run(AGORA_TEST_SSHD_RC="23")
            self.assertEqual(result.returncode, 23)
            self.assertEqual(fixture.stages(result)[-1], "start_sh_failed")
            self.assertIn("stage=start_sh_failed rc=23", result.stderr)
            fault_line = fixture.script.read_text().splitlines().index(str(fixture.bin / "sshd") + " -t") + 1
            self.assertIn(f"stage=start_sh_failed rc=23 line={fault_line} ", result.stderr)
            self.assertFalse((fixture.base / "run" / "status").exists())
        with tempfile.TemporaryDirectory() as directory:
            fixture = StartFixture(Path(directory))
            ssh = fixture.base / "root" / ".ssh"
            ssh.mkdir()
            (ssh / "authorized_keys").symlink_to(fixture.base / "unsafe")
            result = fixture.run()
            self.assertEqual(result.returncode, 76)
            self.assertEqual(fixture.stages(result)[-2:], ["authorized_keys_unsafe", "start_sh_exit"])
            self.assertIn("stage=start_sh_exit rc=76", result.stderr)

    def test_unavailable_file_log_and_closed_stderr_do_not_mask_bootstrap_rc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = StartFixture(Path(directory))
            (fixture.base / "log" / "onstart.log").mkdir()
            result = fixture.run(AGORA_TEST_BOOT_RC="29")
            self.assertEqual(result.returncode, 29, result.stderr)
            self.assertEqual(fixture.status(), "29")
            self.assertEqual(fixture.stages(result)[-2:], ["helper_exit", "start_sh_exit"])
            closed = subprocess.run(
                ["/bin/bash", str(fixture.script)],
                env={**fixture.env, "AGORA_TEST_BOOT_RC": "31"},
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                preexec_fn=lambda: os.close(2),
                timeout=8,
            )
            self.assertEqual(closed.returncode, 31)
            self.assertEqual(fixture.status(), "31")

    def test_bootstrap_failure_keeps_modeled_pid1_alive_after_status_and_unlock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = StartFixture(Path(directory), modeled_pid1=True)
            process = subprocess.Popen(
                ["/bin/bash", str(fixture.script)],
                env={**fixture.env, "AGORA_TEST_BOOT_RC": "29", "AGORA_BOOT_HF_TOKEN": "secret"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 5
                while not (fixture.base / "keepalive-pid").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue((fixture.base / "keepalive-pid").exists())
                self.assertIsNone(process.poll())
                self.assertEqual(fixture.status(), "29")
                with (fixture.base / "run" / "start.lock").open() as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                replay = fixture.run(AGORA_BOOT_AUTOSTART="1", AGORA_TEST_BOOT_RC="0", AGORA_TEST_PID1="0")
                self.assertEqual(replay.returncode, 0, replay.stderr)
                self.assertEqual(fixture.status(), "0")
            finally:
                process.terminate()
                _, stderr = process.communicate(timeout=3)
            self.assertIn("stage=bootstrap_complete rc=29", stderr)
            self.assertIn("stage=pid1_keepalive_exec_attempt rc=0", stderr)
            self.assertNotIn("stage=helper_exit", stderr)


if __name__ == "__main__":
    unittest.main()
