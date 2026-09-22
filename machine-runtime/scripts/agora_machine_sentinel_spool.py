#!/usr/bin/env python3
"""Bounded, lifecycle-checked export and acknowledgement for local Sentinel history."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from machine_sentinel.event_spool import EventSpool


def _identity(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "fleetId": args.fleet_id,
        "authorityEpoch": args.authority_epoch,
        "launchId": args.launch_id,
        "reservationId": args.reservation_id,
        "slotId": args.slot_id,
        "slotGeneration": args.slot_generation,
        "machineGenerationId": args.machine_generation_id,
        "machineId": args.machine_id,
        "provider": args.provider,
        "accountScope": args.account_scope,
        "providerResourceId": args.provider_resource_id,
        "bootId": args.boot_id,
        "setupRevision": args.setup_revision,
    }


def _page(path: str) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Machine Sentinel spool page must be an object")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "ack"))
    parser.add_argument("--spool", required=True)
    parser.add_argument("--cursor")
    parser.add_argument("--fleet-id")
    parser.add_argument("--authority-epoch", type=int)
    parser.add_argument("--launch-id")
    parser.add_argument("--reservation-id")
    parser.add_argument("--slot-id")
    parser.add_argument("--slot-generation", type=int)
    parser.add_argument("--machine-generation-id")
    parser.add_argument("--machine-id")
    parser.add_argument("--provider")
    parser.add_argument("--account-scope")
    parser.add_argument("--provider-resource-id")
    parser.add_argument("--boot-id")
    parser.add_argument("--setup-revision")
    parser.add_argument("--max-events", type=int, default=100)
    parser.add_argument("--max-bytes", type=int, default=256 * 1024)
    parser.add_argument("--page-file", default="-")
    parser.add_argument("--remote-cursor")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    spool = EventSpool(
        Path(args.spool), Path(args.cursor) if args.cursor else None
    )
    if args.action == "export":
        result = spool.export_page(
            expected_identity=_identity(args),
            max_events=args.max_events,
            max_bytes=args.max_bytes,
        )
    else:
        if not args.remote_cursor:
            raise ValueError("Machine Sentinel acknowledgement requires --remote-cursor")
        result = spool.acknowledge_page(
            _page(args.page_file), remote_cursor=args.remote_cursor
        )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
