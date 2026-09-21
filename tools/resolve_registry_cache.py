#!/usr/bin/env python3
"""Resolve one optional public image tag to an immutable cache digest."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable


DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def resolve(
    image: str,
    timeout_seconds: float,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = runner(
            [
                "docker", "buildx", "imagetools", "inspect", f"{image}:latest",
                "--format", "{{.Manifest.Digest}}",
            ],
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "exitStatus": 124,
            "reference": "",
            "digest": "",
            "diagnostic": "public cache lookup exceeded its bounded timeout",
            "seconds": round(time.monotonic() - started, 3),
        }
    digest = result.stdout.strip()
    available = result.returncode == 0 and DIGEST.fullmatch(digest)
    return {
        "status": "available" if available else "unavailable",
        "exitStatus": result.returncode,
        "reference": f"type=registry,ref={image}@{digest}" if available else "",
        "digest": digest if available else "",
        "diagnostic": "" if available else result.stderr[-2000:],
        "seconds": round(time.monotonic() - started, 3),
    }


def _write(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    arguments = parser.parse_args()
    result = resolve(arguments.image, arguments.timeout_seconds)
    _write(arguments.output, result)
    with arguments.github_output.open("a", encoding="utf-8") as stream:
        for key in ("status", "digest", "reference", "exitStatus"):
            output_key = "cache_from" if key == "reference" else key
            stream.write(f"{output_key}={result[key]}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
