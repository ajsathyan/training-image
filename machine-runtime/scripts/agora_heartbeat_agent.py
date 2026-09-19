#!/usr/bin/env python3
"""Send signed Agora fleet heartbeats to the Cloudflare recovery endpoint."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import random
import shlex
import socket
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

DEFAULT_INTERVAL_SECONDS = 3.0
DEFAULT_JITTER_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 5.0


def parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        if not key:
            continue
        try:
            value = shlex.split(raw_value.strip(), posix=True)[0]
        except (IndexError, ValueError):
            value = raw_value.strip().strip("'\"")
        env[key] = value
    return env


def load_boot_id() -> str:
    boot_id_path = Path("/proc/sys/kernel/random/boot_id")
    if boot_id_path.exists():
        text = boot_id_path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return str(uuid.uuid4())


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_next_seq(path: Path | None, boot_id: str) -> int:
    if path is None or not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if data.get("bootId") != boot_id:
        return 0
    try:
        return max(0, int(data.get("nextSeq", 0)))
    except (TypeError, ValueError):
        return 0


def write_next_seq(path: Path | None, boot_id: str, next_seq: int) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps({"bootId": boot_id, "nextSeq": next_seq}, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def build_payload(args: argparse.Namespace, *, boot_id: str, seq: int) -> dict[str, Any]:
    return {
        "machineId": args.machine_id or os.environ.get("AGORA_MACHINE_ID") or socket.gethostname(),
        "role": args.role or os.environ.get("AGORA_JOIN_ROLE") or os.environ.get("AGORA_ROLE") or "tail",
        "tokenLabel": args.token_label or os.environ.get("AGORA_TOKEN_LABEL"),
        "runpodPodId": args.runpod_pod_id or os.environ.get("RUNPOD_POD_ID") or os.environ.get("RUNPOD_PODID"),
        "runpodDcId": args.runpod_dc_id or os.environ.get("RUNPOD_DC_ID"),
        "bootId": boot_id,
        "seq": seq,
        "clientSentAt": iso_now(),
        "process": {
            "agent": "agora_heartbeat_agent.py",
            "version": 1,
        },
        "status": {
            "hostname": socket.gethostname(),
        },
    }


def sign_body(secret: str, body: bytes, timestamp: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), timestamp.encode("utf-8") + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def send_once(url: str, secret: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    timestamp = str(int(time.time()))
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "user-agent": "agora-heartbeat-agent/1",
            "x-agora-timestamp": timestamp,
            "x-agora-signature": sign_body(secret, body, timestamp),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read(4096).decode("utf-8", errors="replace")
            return {"ok": 200 <= response.status < 300, "status": response.status, "body": safe_json(response_body)}
    except urllib.error.HTTPError as exc:
        response_body = exc.read(4096).decode("utf-8", errors="replace")
        return {"ok": False, "status": exc.code, "body": safe_json(response_body)}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def next_seq_after_send(seq: int, result: dict[str, Any]) -> int:
    body = result.get("body") if isinstance(result, dict) else None
    if (
        isinstance(body, dict)
        and result.get("status") == 409
        and body.get("reason") == "non_monotonic_sequence"
    ):
        try:
            previous_seq = int(body.get("previousSeq"))
        except (TypeError, ValueError):
            previous_seq = -1
        if previous_seq >= seq:
            return previous_seq + 1
    return seq + 1


def safe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def load_secret(args: argparse.Namespace) -> str:
    if args.secret:
        return args.secret
    env = dict(os.environ)
    if args.secret_env_file:
        env.update(parse_env_file(Path(args.secret_env_file).expanduser()))
    secret = env.get(args.secret_env)
    if not secret:
        raise SystemExit(f"missing heartbeat secret in {args.secret_env}")
    return secret


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("AGORA_HEARTBEAT_URL"), help="Cloudflare Worker /v1/heartbeat URL.")
    parser.add_argument("--secret-env", default="AGORA_HEARTBEAT_SECRET")
    parser.add_argument("--secret-env-file", help="Optional env file containing AGORA_HEARTBEAT_SECRET.")
    parser.add_argument("--secret", help=argparse.SUPPRESS)
    parser.add_argument("--machine-id")
    parser.add_argument("--role", choices=["head", "body", "tail"])
    parser.add_argument("--token-label")
    parser.add_argument("--runpod-pod-id")
    parser.add_argument("--runpod-dc-id")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--jitter", type=float, default=DEFAULT_JITTER_SECONDS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--state-file", default=os.environ.get("AGORA_HEARTBEAT_STATE_FILE", "~/.agora-heartbeat-state.json"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if not args.url:
        raise SystemExit("missing heartbeat URL")
    if args.interval <= 0:
        raise SystemExit("--interval must be positive")
    if args.jitter < 0:
        raise SystemExit("--jitter cannot be negative")

    secret = load_secret(args)
    boot_id = load_boot_id()
    state_file = Path(args.state_file).expanduser() if args.state_file else None
    seq = read_next_seq(state_file, boot_id)
    while True:
        write_next_seq(state_file, boot_id, seq + 1)
        payload = build_payload(args, boot_id=boot_id, seq=seq)
        result = send_once(args.url, secret, payload, args.timeout)
        print(json.dumps({"sentAt": iso_now(), "seq": seq, "result": result}, sort_keys=True), flush=True)
        next_seq = next_seq_after_send(seq, result)
        if next_seq != seq + 1:
            write_next_seq(state_file, boot_id, next_seq)
        if args.once:
            return 0 if result.get("ok") else 1
        seq = next_seq
        delay = args.interval + random.uniform(0, args.jitter)
        time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(main())
