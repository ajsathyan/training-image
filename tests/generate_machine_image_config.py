#!/usr/bin/env python3
"""Generate smoke-test configs through Fleet's exported production adapter."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = (
    ROOT
    / "machine-runtime"
    / "scripts"
    / "agora_control"
    / "execution"
    / "image_runtime.py"
)


def _load_adapter():
    spec = importlib.util.spec_from_file_location("fleet_image_runtime_smoke", ADAPTER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load exported Fleet adapter: {ADAPTER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json_file(path: Path | None, *, default: Any) -> Any:
    if path is None:
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    config = subparsers.add_parser("config")
    manifest = subparsers.add_parser("manifest")
    for command in (config, manifest):
        command.add_argument("--machine", required=True, type=Path)
        command.add_argument("--token-sha256", required=True)
        command.add_argument("--output", required=True, type=Path)
    config.add_argument("--start-training", action="store_true")
    config.add_argument("--authority", type=Path)
    config.add_argument("--sentinel", type=Path)
    config.add_argument("--heartbeat", type=Path)
    config.add_argument(
        "--transition-kind",
        choices=("stage", "ready", "rollback_prior"),
        required=True,
    )
    config.add_argument("--allow-absent", action="store_true")
    config.add_argument("--expected-machine", type=Path)
    config.add_argument("--expected-token-sha256")
    config.add_argument(
        "--expected-state", choices=("fenced", "staged", "ready")
    )
    manifest.add_argument(
        "--state", choices=("fenced", "staged", "ready"), required=True
    )
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    adapter = _load_adapter()
    machine = _json_file(args.machine, default={})
    if args.command == "manifest":
        payload = adapter.build_assignment_manifest(
            machine, token_sha256=args.token_sha256, state=args.state
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        args.output.chmod(0o600)
        return 0
    expected_parts = (
        args.expected_machine,
        args.expected_token_sha256,
        args.expected_state,
    )
    if any(value is not None for value in expected_parts) and not all(
        value is not None for value in expected_parts
    ):
        raise ValueError(
            "expected manifest requires --expected-machine, "
            "--expected-token-sha256, and --expected-state"
        )
    expected = None
    if args.expected_machine is not None:
        expected = adapter.build_assignment_manifest(
            _json_file(args.expected_machine, default={}),
            token_sha256=args.expected_token_sha256,
            state=args.expected_state,
        )
    config = adapter.build_machine_image_config(
        machine,
        token_sha256=args.token_sha256,
        start_training=args.start_training,
        authority=_json_file(args.authority, default={}),
        sentinel=_json_file(
            args.sentinel, default={"url": "", "exportEnabled": False}
        ),
        heartbeat=_json_file(args.heartbeat, default={}),
        assignment_transition={
            "kind": args.transition_kind,
            "allowAbsent": args.allow_absent,
            "expectedManifest": expected,
        },
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    args.output.chmod(0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
