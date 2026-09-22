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


if __name__ == "__main__":
    unittest.main()
