"""Controller adapter for the versioned Agora machine image bootstrap."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import posixpath
import re
import shlex
from collections.abc import Mapping
from typing import Any

CAPABILITY_SCHEMA = "agora.machine-image-capability.v1"
CONFIG_SCHEMA = "agora.machine-image-config.v1"
RECEIPT_SCHEMA = "agora.machine-image-bootstrap-receipt.v1"
BOOT_LAUNCH_SCHEMA = "agora.machine-boot-launch.v1"
BOOT_AUTOSTART_ENV = "AGORA_BOOT_AUTOSTART"
BOOT_LAUNCH_ENV = "AGORA_BOOT_LAUNCH_B64"
BOOT_TOKEN_ENV = "AGORA_BOOT_HF_TOKEN"
BOOT_RESERVED_ENV = frozenset(
    {BOOT_AUTOSTART_ENV, BOOT_LAUNCH_ENV, BOOT_TOKEN_ENV}
)
CAPABILITY_PATH = "/opt/agora-image-runtime/capability.json"
BOOTSTRAP_PATH = "/opt/agora-image-runtime/agora_image_bootstrap.py"
PYTHON_PATH = "/opt/agora-venv/bin/python"
TRAINING_SOURCE_ROOT = "/opt/agora-source"
DEFAULT_REMOTE_ROOT = "/workspace/agora-run"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ASSIGNMENT_STATES = {"fenced", "staged", "ready"}
_ASSIGNMENT_TRANSITIONS = {"stage", "ready", "rollback_prior"}


def build_machine_boot_launch(
    machine: Mapping[str, Any],
    *,
    token: str,
    error: type[Exception] = ValueError,
) -> dict[str, Any]:
    """Build one provider-deferred, machine-scoped boot launch envelope.

    Provider resource identity and the public mapping for 49200 are deliberately
    absent. The image resolves both from provider-owned metadata before it can
    call the canonical v1 bootstrap.
    """

    if not token or any(character in token for character in "\r\n\x00"):
        raise error("boot launch requires one machine-scoped HF token")
    capability = declared_image_capability(machine, error=error)
    if capability is None:
        raise error("boot launch requires a declared image capability")
    provider = _text(machine, "provider", error=error).lower()
    if provider not in {"runpod", "vast"}:
        raise error("boot launch provider must be runpod or vast")
    node_type = str(
        machine.get("agoraJoinRole") or machine.get("provisioningRole") or "tail"
    ).strip().lower()
    if node_type not in {"head", "body", "tail"}:
        raise error("boot launch requires a canonical node role")
    operation_id = _text(machine, "assignmentOperationId", error=error)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    config: dict[str, Any] = {
        "schemaVersion": CONFIG_SCHEMA,
        "machineId": _text(machine, "id", "machineId", error=error),
        "provider": provider,
        "accountScope": _text(
            machine, "accountScope", "providerAccount", error=error
        ).lower(),
        "tokenLabel": _text(machine, "tokenLabel", error=error),
        "tokenInstance": _positive_int(machine, "tokenInstance", error=error),
        "assignmentGeneration": _positive_int(
            machine, "assignmentGeneration", error=error
        ),
        "assignmentOperationId": operation_id,
        "runId": _text(machine, "runId", error=error),
        "trainingSessionId": str(
            machine.get("trainingSessionId") or operation_id
        ).strip(),
        "gpuModel": _text(machine, "gpuModel", error=error),
        "nodeType": node_type,
        "provisioningOrigin": str(
            machine.get("provisioningOrigin") or "provider_boot"
        ).strip(),
        "hostPort": 49200,
        "remoteRoot": _remote_root(machine, error=error),
        "startTraining": True,
        "px0Enabled": bool(machine.get("px0Enabled", True)),
        "tokenSha256": digest,
        "assignmentTransition": {
            "kind": "ready",
            "allowAbsent": True,
            "expectedManifest": None,
        },
        "imageCapability": capability,
        "sentinel": {"mode": "local", "url": "", "timeoutSeconds": 10.0},
        "heartbeat": {"mode": "disabled", "url": ""},
    }
    for name in (
        "fleetId",
        "authorityEpoch",
        "launchId",
        "reservationId",
        "slotId",
        "slotGeneration",
        "machineGenerationId",
    ):
        if machine.get(name) is not None:
            config[name] = machine[name]
    return {
        "schemaVersion": BOOT_LAUNCH_SCHEMA,
        "tokenSha256": digest,
        "config": config,
    }


def encode_machine_boot_launch(
    launch: Mapping[str, Any], *, error: type[Exception] = ValueError
) -> str:
    if launch.get("schemaVersion") != BOOT_LAUNCH_SCHEMA:
        raise error("boot launch has unsupported schemaVersion")
    try:
        payload = json.dumps(
            dict(launch), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise error("boot launch is not canonical JSON") from exc
    return base64.b64encode(payload).decode("ascii")


def merge_runpod_boot_launch(
    payload: Mapping[str, Any],
    *,
    launch: Mapping[str, Any],
    token: str,
    starter_path: str = "/start.sh",
    starter_log: str = "/var/log/agora-image-start.log",
    error: type[Exception] = ValueError,
) -> dict[str, Any]:
    """Merge opt-in boot inputs while preserving the user's argv and env."""

    result = dict(payload)
    env = dict(result.get("env") or {})
    collisions = BOOT_RESERVED_ENV.intersection(str(key) for key in env)
    if collisions:
        raise error(
            "RunPod launch environment overrides reserved boot keys: "
            + ", ".join(sorted(collisions))
        )
    env.update(
        {
            BOOT_AUTOSTART_ENV: "1",
            BOOT_LAUNCH_ENV: encode_machine_boot_launch(launch, error=error),
            BOOT_TOKEN_ENV: token,
        }
    )
    original = list(result.get("dockerStartCmd") or [])
    if original:
        result["dockerStartCmd"] = [
            "/bin/bash",
            "-lc",
            f"nohup {shlex.quote(starter_path)} >{shlex.quote(starter_log)} 2>&1 & exec \"$@\"",
            "agora-user-command",
            *original,
        ]
    result["env"] = env
    return result


def merge_vast_boot_launch(
    payload: Mapping[str, Any],
    *,
    launch: Mapping[str, Any],
    token: str,
    starter_path: str = "/start.sh",
    starter_log: str = "/var/log/agora-image-start.log",
    error: type[Exception] = ValueError,
) -> dict[str, Any]:
    """Merge Vast docker flags and append the shared starter to ssh_direct."""

    result = dict(payload)
    env = dict(result.get("env") or {})
    reserved_flags = {f"-e {name}" for name in BOOT_RESERVED_ENV}
    collisions = reserved_flags.intersection(str(key) for key in env)
    if collisions:
        raise error(
            "Vast launch environment overrides reserved boot keys: "
            + ", ".join(sorted(collisions))
        )
    env.update(
        {
            f"-e {BOOT_AUTOSTART_ENV}": "1",
            f"-e {BOOT_LAUNCH_ENV}": encode_machine_boot_launch(
                launch, error=error
            ),
            f"-e {BOOT_TOKEN_ENV}": token,
        }
    )
    onstart = str(result.get("onstart") or "").rstrip()
    preserved = ""
    if onstart:
        preserved = (
            "AGORA_PRIOR_ONSTART_RC=0\n"
            f"bash -c {shlex.quote(onstart)} || AGORA_PRIOR_ONSTART_RC=$?\n"
            'if [ "$AGORA_PRIOR_ONSTART_RC" -ne 0 ]; then exit "$AGORA_PRIOR_ONSTART_RC"; fi\n'
        )
    result["onstart"] = (
        preserved
        + f"nohup {shlex.quote(starter_path)} >{shlex.quote(starter_log)} 2>&1 &\n"
        + "true"
    )
    result["env"] = env
    return result


def boot_launch_summary(launch: Mapping[str, Any]) -> dict[str, Any]:
    """Return the persistable secret-free binding for a launch envelope."""

    config = launch.get("config")
    if not isinstance(config, Mapping):
        return {"schemaVersion": launch.get("schemaVersion"), "valid": False}
    return {
        "schemaVersion": launch.get("schemaVersion"),
        "valid": True,
        "machineId": config.get("machineId"),
        "provider": config.get("provider"),
        "accountScope": config.get("accountScope"),
        "assignmentOperationId": config.get("assignmentOperationId"),
        "assignmentGeneration": config.get("assignmentGeneration"),
        "tokenLabel": config.get("tokenLabel"),
        "tokenInstance": config.get("tokenInstance"),
        "tokenSha256": launch.get("tokenSha256"),
    }


def render_registered_sentinel_start_script(
    machine: Mapping[str, Any],
    *,
    fleet_id: str,
    authority_epoch: int,
    error: type[Exception] = ValueError,
) -> str:
    """Hydrate local Sentinel identity and start only the image-baked runtime."""

    if not fleet_id or not isinstance(authority_epoch, int) or authority_epoch < 1:
        raise error("registered Sentinel start requires canonical local authority")
    lifecycle: dict[str, Any] = {
        "fleetId": fleet_id,
        "authorityEpoch": authority_epoch,
    }
    for name in (
        "launchId",
        "reservationId",
        "slotId",
        "machineGenerationId",
    ):
        lifecycle[name] = _text(machine, name, error=error)
    lifecycle["slotGeneration"] = _positive_int(
        machine, "slotGeneration", error=error
    )
    binding = {
        "machineId": _text(machine, "id", "machineId", error=error),
        "provider": _text(machine, "provider", error=error).lower(),
        "accountScope": _text(
            machine, "accountScope", "providerAccount", error=error
        ).lower(),
        "providerResourceId": _text(
            machine,
            "providerResourceId",
            "runpodId",
            "vastId",
            "vastInstanceId",
            error=error,
        ),
        "assignmentOperationId": _text(
            machine, "assignmentOperationId", error=error
        ),
        "assignmentGeneration": _positive_int(
            machine, "assignmentGeneration", error=error
        ),
    }
    payload = base64.b64encode(
        json.dumps(
            {"binding": binding, "lifecycle": lifecycle},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).decode("ascii")
    root = _remote_root(machine, error=error)
    q = shlex.quote
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={q(root)}
CONFIG="$ROOT/controller-input/machine-config.json"
TOKEN="$ROOT/controller-input/hf-token"
RECEIPT="$ROOT/bootstrap-receipt.json"
test -x /opt/agora-venv/bin/python
test -f /opt/agora-image-runtime/agora_image_bootstrap.py
/opt/agora-venv/bin/python - "$CONFIG" {q(payload)} <<'PY'
import base64,json,pathlib,stat,sys
runtime = pathlib.Path('/opt/agora-image-runtime')
sys.path.insert(0, str(runtime))
from assignment_transition import private_atomic_write
path = pathlib.Path(sys.argv[1])
metadata = path.lstat()
if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
    raise SystemExit('registered Sentinel config is not a regular 0600 file')
config = json.loads(path.read_text(encoding='utf-8'))
patch = json.loads(base64.b64decode(sys.argv[2], validate=True))
binding = patch['binding']
for key, expected in binding.items():
    if config.get(key) != expected:
        raise SystemExit(f'registered Sentinel binding mismatch: {{key}}')
config.update(patch['lifecycle'])
config['sentinel'] = {{
    'mode': 'local',
    'url': '',
    'timeoutSeconds': 10.0,
    'fleetId': patch['lifecycle']['fleetId'],
    'authorityEpoch': patch['lifecycle']['authorityEpoch'],
}}
private_atomic_write(path, (json.dumps(config, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8'))
PY
/opt/agora-venv/bin/python /opt/agora-image-runtime/agora_image_bootstrap.py \
  --config "$CONFIG" --token-file "$TOKEN" --receipt "$RECEIPT" --observation-resume
/opt/agora-venv/bin/python - "$ROOT/machine-sentinel/state.json" {q(payload)} <<'PY'
import base64,json,pathlib,stat,sys
path = pathlib.Path(sys.argv[1])
metadata = path.lstat()
if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
    raise SystemExit('registered Sentinel state is not a regular 0600 file')
state = json.loads(path.read_text(encoding='utf-8'))
identity = state.get('identity') if isinstance(state, dict) else None
if not isinstance(identity, dict):
    raise SystemExit('registered Sentinel state has no identity proof')
patch = json.loads(base64.b64decode(sys.argv[2], validate=True))
expected = {{**patch['binding'], **patch['lifecycle']}}
for key, value in expected.items():
    if identity.get(key) != value:
        raise SystemExit(f'registered Sentinel state mismatch: {{key}}')
for key in ('bootId', 'setupRevision'):
    if not isinstance(identity.get(key), str) or not identity[key].strip():
        raise SystemExit(f'registered Sentinel state is missing {{key}}')
proof = {{key: identity[key] for key in (*expected, 'bootId', 'setupRevision')}}
encoded = base64.b64encode(json.dumps(proof, sort_keys=True, separators=(',', ':')).encode('utf-8')).decode('ascii')
print('__AGORA_SENTINEL_LOCAL_READY__ proof=' + encoded)
PY
"""


def parse_registered_sentinel_ready(
    output: Any,
    machine: Mapping[str, Any],
    *,
    fleet_id: str,
    authority_epoch: int,
    error: type[Exception] = ValueError,
) -> dict[str, Any]:
    """Validate the authenticated identity proof emitted by image observation resume."""

    matches = re.findall(
        r"^__AGORA_SENTINEL_LOCAL_READY__ proof=([A-Za-z0-9+/=]+)$",
        str(output or ""),
        flags=re.MULTILINE,
    )
    if len(matches) != 1:
        raise error("registered Sentinel returned no unique identity proof")
    try:
        proof = json.loads(base64.b64decode(matches[0], validate=True))
    except (ValueError, json.JSONDecodeError) as exc:
        raise error("registered Sentinel returned invalid identity proof") from exc
    if not isinstance(proof, dict):
        raise error("registered Sentinel identity proof must be an object")
    expected: dict[str, Any] = {
        "fleetId": fleet_id,
        "authorityEpoch": authority_epoch,
        "machineId": _text(machine, "id", "machineId", error=error),
        "provider": _text(machine, "provider", error=error).lower(),
        "accountScope": _text(
            machine, "accountScope", "providerAccount", error=error
        ).lower(),
        "providerResourceId": _text(
            machine,
            "providerResourceId",
            "runpodId",
            "vastId",
            "vastInstanceId",
            error=error,
        ),
        "assignmentOperationId": _text(
            machine, "assignmentOperationId", error=error
        ),
        "assignmentGeneration": _positive_int(
            machine, "assignmentGeneration", error=error
        ),
        "slotGeneration": _positive_int(machine, "slotGeneration", error=error),
    }
    for name in ("launchId", "reservationId", "slotId", "machineGenerationId"):
        expected[name] = _text(machine, name, error=error)
    for key, value in expected.items():
        if proof.get(key) != value:
            raise error(f"registered Sentinel identity proof mismatch: {key}")
    for key in ("bootId", "setupRevision"):
        if not isinstance(proof.get(key), str) or not proof[key].strip():
            raise error(f"registered Sentinel identity proof is missing {key}")
    return {key: proof[key] for key in (*expected, "bootId", "setupRevision")}


def configured_image_capability(
    contract: Any,
    fingerprint: Any,
    *,
    error: type[Exception] = ValueError,
) -> dict[str, str] | None:
    """Validate one explicit operator image declaration.

    The declaration is intentionally independent of provider image names and
    tags. The remote marker remains the authority for whether the claim is
    true on a particular machine.
    """

    version = str(contract or "").strip()
    artifact = str(fingerprint or "").strip().lower()
    if not version and not artifact:
        return None
    if version != CAPABILITY_SCHEMA:
        raise error(f"image capability contract must be {CAPABILITY_SCHEMA}")
    if not _SHA256.fullmatch(artifact):
        raise error("image capability fingerprint must be a lowercase SHA-256")
    return {
        "contractVersion": version,
        "runtimeArtifactFingerprint": artifact,
    }


def declared_image_capability(
    machine: Mapping[str, Any], *, error: type[Exception] = ValueError
) -> dict[str, str] | None:
    if "imageCapability" not in machine or machine.get("imageCapability") is None:
        return None
    raw = machine.get("imageCapability")
    if not isinstance(raw, Mapping):
        raise error("stored imageCapability must be an object")
    capability = configured_image_capability(
        raw.get("contractVersion"),
        raw.get("runtimeArtifactFingerprint"),
        error=error,
    )
    if capability is None:
        raise error("stored imageCapability must declare its contract and fingerprint")
    return capability


def _text(
    machine: Mapping[str, Any],
    name: str,
    *aliases: str,
    error: type[Exception],
) -> str:
    for key in (name, *aliases):
        value = str(machine.get(key) or "").strip()
        if value:
            return value
    raise error(f"declared image setup requires canonical machine field {name}")


def _positive_int(
    machine: Mapping[str, Any], name: str, *, error: type[Exception]
) -> int:
    value = machine.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise error(f"declared image setup requires positive canonical field {name}")
    return value


def _optional_text(machine: Mapping[str, Any], name: str) -> str | None:
    value = machine.get(name)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _optional_positive_int(machine: Mapping[str, Any], name: str) -> int | None:
    value = machine.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return None
    return value


def _remote_root(machine: Mapping[str, Any], *, error: type[Exception]) -> str:
    value = str(machine.get("remoteRoot") or DEFAULT_REMOTE_ROOT).strip()
    components = [part for part in value.split("/") if part]
    if (
        not value.startswith("/")
        or posixpath.normpath(value) != value
        or any(character in value for character in "\r\n\x00")
        or len(components) < 2
        or value in {"/", "/workspace", "/root", "/opt"}
    ):
        raise error("declared image setup requires a safe normalized remoteRoot")
    return value


def build_assignment_manifest(
    machine: Mapping[str, Any],
    *,
    token_sha256: str,
    state: str,
    error: type[Exception] = ValueError,
) -> dict[str, Any]:
    """Build the exact schema-v1 binding consumed by the image CAS."""

    digest = str(token_sha256 or "").strip().lower()
    if not _SHA256.fullmatch(digest):
        raise error("assignment manifest requires tokenSha256")
    normalized_state = str(state or "").strip().lower()
    if normalized_state not in _ASSIGNMENT_STATES:
        raise error("assignment manifest has invalid state")
    return {
        "schemaVersion": 1,
        "operationId": _text(
            machine, "assignmentOperationId", "operationId", error=error
        ),
        "assignmentGeneration": _positive_int(
            machine, "assignmentGeneration", error=error
        ),
        "tokenLabel": _text(machine, "tokenLabel", error=error),
        "tokenInstance": _positive_int(machine, "tokenInstance", error=error),
        "machineId": _text(machine, "id", "machineId", error=error),
        "provider": _text(machine, "provider", error=error).lower(),
        "accountScope": _text(
            machine, "accountScope", "providerAccount", error=error
        ).lower(),
        "providerResourceId": _text(
            machine,
            "providerResourceId",
            "runpodId",
            "vastInstanceId",
            "vastId",
            error=error,
        ),
        "tokenSha256": digest,
        "state": normalized_state,
    }


def _validated_assignment_transition(
    value: Mapping[str, Any] | None,
    *,
    start_training: bool,
    error: type[Exception],
) -> dict[str, Any]:
    if value is None:
        return {
            "kind": "ready" if start_training else "stage",
            "allowAbsent": True,
            "expectedManifest": None,
        }
    kind = str(value.get("kind") or "").strip().lower()
    if kind not in _ASSIGNMENT_TRANSITIONS:
        raise error("declared image assignment transition has invalid kind")
    if (kind == "ready") is not bool(start_training):
        raise error("declared image assignment transition conflicts with startTraining")
    allow_absent = value.get("allowAbsent")
    if not isinstance(allow_absent, bool):
        raise error("declared image assignment transition has invalid allowAbsent")
    expected_raw = value.get("expectedManifest")
    if expected_raw is None:
        expected = None
    elif isinstance(expected_raw, Mapping):
        expected = build_assignment_manifest(
            expected_raw,
            token_sha256=str(expected_raw.get("tokenSha256") or ""),
            state=str(expected_raw.get("state") or ""),
            error=error,
        )
    else:
        raise error("declared image assignment transition expectedManifest is invalid")
    if allow_absent and expected is not None:
        raise error("declared image assignment transition cannot both allow absence and expect a manifest")
    if kind == "rollback_prior" and (allow_absent or expected is None):
        raise error("declared image rollback requires an exact current manifest")
    return {
        "kind": kind,
        "allowAbsent": allow_absent,
        "expectedManifest": expected,
    }


def build_machine_image_config(
    machine: Mapping[str, Any],
    *,
    token_sha256: str,
    start_training: bool,
    authority: Mapping[str, Any] | None = None,
    sentinel: Mapping[str, Any] | None = None,
    heartbeat: Mapping[str, Any] | None = None,
    identity_backup_sha256: str | None = None,
    assignment_transition: Mapping[str, Any] | None = None,
    error: type[Exception] = ValueError,
) -> dict[str, Any]:
    """Adapt the canonical Fleet row to the image launch contract.

    Assignment ownership remains with Fleet. This function only validates and
    serializes its current durable projection.
    """

    capability = declared_image_capability(machine, error=error)
    if capability is None:
        raise error("machine does not declare a baked image capability")
    authority = authority or {}
    sentinel = sentinel or {}
    heartbeat = heartbeat or {}
    fleet_ids = {
        str(value).strip()
        for value in (
            machine.get("fleetId"),
            authority.get("fleetId"),
            sentinel.get("fleetId"),
        )
        if str(value or "").strip()
    }
    if len(fleet_ids) > 1:
        raise error("declared image setup has conflicting fleetId values")
    fleet_id = next(iter(fleet_ids), "")
    authority_epochs = {
        value
        for value in (
            machine.get("authorityEpoch"),
            authority.get("authorityEpoch"),
            sentinel.get("authorityEpoch"),
        )
        if value is not None
    }
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in authority_epochs):
        raise error("declared image setup has invalid authorityEpoch")
    if len(authority_epochs) > 1:
        raise error("declared image setup has conflicting authorityEpoch values")
    authority_epoch = next(iter(authority_epochs), None)
    digest = str(token_sha256 or "").strip().lower()
    if not _SHA256.fullmatch(digest):
        raise error("declared image setup requires tokenSha256")
    host_port = machine.get("hostPort", 49200)
    if host_port != 49200:
        raise error("declared image setup requires hostPort 49200")
    announce_port = machine.get("announcePort")
    if announce_port is not None and (
        not isinstance(announce_port, int)
        or isinstance(announce_port, bool)
        or not 1 <= announce_port <= 65535
    ):
        raise error("declared image setup has invalid announcePort")
    if start_training and announce_port is None:
        raise error("declared image setup requires announcePort before training starts")
    node_type = str(
        machine.get("agoraJoinRole") or machine.get("provisioningRole") or ""
    ).strip().lower()
    if node_type not in {"head", "body", "tail"}:
        raise error("declared image setup requires canonical node role")
    operation_id = _text(machine, "assignmentOperationId", error=error)
    remote_root = _remote_root(machine, error=error)
    sentinel_url = str(sentinel.get("url") or "").strip()
    sentinel_remote = bool(sentinel.get("exportEnabled", True)) and bool(
        sentinel_url
    )
    try:
        sentinel_timeout = float(sentinel.get("timeoutSeconds", 10))
    except (TypeError, ValueError) as exc:
        raise error("declared image setup has invalid Sentinel timeoutSeconds") from exc
    if not math.isfinite(sentinel_timeout) or sentinel_timeout <= 0:
        raise error("declared image setup has invalid Sentinel timeoutSeconds")
    sentinel_config: dict[str, Any] = {
        "mode": "remote" if sentinel_remote else "local",
        "url": sentinel_url if sentinel_remote else "",
        "timeoutSeconds": sentinel_timeout,
    }
    if fleet_id:
        sentinel_config["fleetId"] = fleet_id
    if sentinel_remote:
        if not fleet_id:
            raise error("declared image remote Sentinel setup requires fleetId")
        if (
            not isinstance(authority_epoch, int)
            or isinstance(authority_epoch, bool)
            or authority_epoch < 1
        ):
            raise error(
                "declared image remote Sentinel setup requires authorityEpoch"
            )
        sentinel_config["authorityEpoch"] = authority_epoch
        bootstrap_token = str(sentinel.get("bootstrapToken") or "")
        machine_token = str(sentinel.get("machineToken") or "")
        if not bootstrap_token and not machine_token:
            raise error("declared image remote Sentinel setup requires a credential")
        input_root = f"{remote_root}/controller-input"
        if bootstrap_token:
            sentinel_config["bootstrapTokenFile"] = (
                f"{input_root}/sentinel-bootstrap-token"
            )
        if machine_token:
            sentinel_config["machineTokenFile"] = f"{input_root}/sentinel-machine-token"
    heartbeat_url = str(heartbeat.get("url") or "").strip()
    heartbeat_config: dict[str, Any] = {
        "mode": "configured" if heartbeat_url else "disabled",
        "url": heartbeat_url,
    }
    if heartbeat_url:
        heartbeat_secret = str(heartbeat.get("machineSecret") or "")
        if not heartbeat_secret or any(
            character in heartbeat_secret for character in "\r\n\x00"
        ):
            raise error("declared image heartbeat requires a one-line machine secret")
        try:
            heartbeat_timing = {
                "intervalSeconds": float(heartbeat.get("intervalSeconds", 10)),
                "jitterSeconds": float(heartbeat.get("jitterSeconds", 3)),
                "timeoutSeconds": float(heartbeat.get("timeoutSeconds", 5)),
            }
        except (TypeError, ValueError) as exc:
            raise error("declared image heartbeat timing is invalid") from exc
        heartbeat_config.update(
            {
                "secretFile": f"{remote_root}/controller-input/heartbeat-machine-secret",
                "role": str(heartbeat.get("role") or "").strip().lower(),
                "tokenLabel": _text(heartbeat, "tokenLabel", error=error),
                "runpodPodId": str(heartbeat.get("runpodPodId") or ""),
                "runpodDcId": str(heartbeat.get("runpodDcId") or ""),
                **heartbeat_timing,
            }
        )
        if heartbeat_config["role"] not in {"head", "body", "tail"}:
            raise error("declared image heartbeat requires a canonical role")
        if any(
            not math.isfinite(heartbeat_config[key])
            for key in ("intervalSeconds", "jitterSeconds", "timeoutSeconds")
        ) or any(
            heartbeat_config[key] <= 0
            for key in ("intervalSeconds", "timeoutSeconds")
        ) or heartbeat_config["jitterSeconds"] < 0:
            raise error("declared image heartbeat timing must be non-negative")
    config: dict[str, Any] = {
        "schemaVersion": CONFIG_SCHEMA,
        "machineId": _text(machine, "id", "machineId", error=error),
        "launchId": _optional_text(machine, "launchId"),
        "reservationId": _optional_text(machine, "reservationId"),
        "slotId": _optional_text(machine, "slotId"),
        "slotGeneration": _optional_positive_int(machine, "slotGeneration"),
        "machineGenerationId": _optional_text(machine, "machineGenerationId"),
        "provider": _text(machine, "provider", error=error).lower(),
        "accountScope": _text(machine, "accountScope", "providerAccount", error=error).lower(),
        "providerResourceId": _text(
            machine,
            "providerResourceId",
            "runpodId",
            "vastInstanceId",
            "vastId",
            error=error,
        ),
        "tokenLabel": _text(machine, "tokenLabel", error=error),
        "tokenInstance": _positive_int(machine, "tokenInstance", error=error),
        "assignmentGeneration": _positive_int(
            machine, "assignmentGeneration", error=error
        ),
        "assignmentOperationId": operation_id,
        "runId": _text(machine, "runId", error=error),
        "trainingSessionId": str(
            machine.get("trainingSessionId") or operation_id
        ).strip(),
        "gpuModel": _text(machine, "gpuModel", error=error),
        "nodeType": node_type,
        "provisioningOrigin": str(
            machine.get("provisioningOrigin") or "other_non_retiring"
        ).strip(),
        "hostPort": 49200,
        "announcePort": announce_port,
        "remoteRoot": remote_root,
        "startTraining": bool(start_training),
        "px0Enabled": bool(machine.get("px0Enabled", True)),
        "tokenSha256": digest,
        "assignmentTransition": _validated_assignment_transition(
            assignment_transition,
            start_training=start_training,
            error=error,
        ),
        "imageCapability": capability,
        "sentinel": sentinel_config,
        "heartbeat": heartbeat_config,
    }
    if fleet_id:
        config["fleetId"] = fleet_id
    if (
        isinstance(authority_epoch, int)
        and not isinstance(authority_epoch, bool)
        and authority_epoch > 0
    ):
        config["authorityEpoch"] = authority_epoch
    backup_digest = str(identity_backup_sha256 or "").strip().lower()
    if backup_digest and not _SHA256.fullmatch(backup_digest):
        raise error("declared image setup has invalid identityBackupSha256")
    if backup_digest:
        config["identityBackupSha256"] = backup_digest
    return config


def render_image_bootstrap_script(
    config: Mapping[str, Any],
    token: str,
    *,
    error: type[Exception] = ValueError,
    register_secret: Any = None,
    sentinel_secrets: Mapping[str, Any] | None = None,
    heartbeat_secrets: Mapping[str, Any] | None = None,
) -> str:
    """Render one scoped SSH stdin program for config + credential bootstrap."""

    capability = config.get("imageCapability")
    if not isinstance(capability, Mapping):
        raise error("image bootstrap config has no capability declaration")
    expected_fingerprint = str(
        capability.get("runtimeArtifactFingerprint") or ""
    ).strip().lower()
    if not _SHA256.fullmatch(expected_fingerprint):
        raise error("image bootstrap config has invalid capability fingerprint")
    remote_root = str(config.get("remoteRoot") or DEFAULT_REMOTE_ROOT)
    config_path = f"{remote_root}/controller-input/machine-config.json"
    token_path = f"{remote_root}/controller-input/hf-token"
    receipt_path = f"{remote_root}/bootstrap-receipt.json"
    sentinel_config = config.get("sentinel")
    if not isinstance(sentinel_config, Mapping):
        raise error("image bootstrap config has no Sentinel mode")
    sentinel_secrets = sentinel_secrets or {}
    heartbeat_secrets = heartbeat_secrets or {}
    credential_writes: list[tuple[str, str]] = []
    for config_key, secret_key in (
        ("bootstrapTokenFile", "bootstrapToken"),
        ("machineTokenFile", "machineToken"),
    ):
        path = str(sentinel_config.get(config_key) or "").strip()
        secret = str(sentinel_secrets.get(secret_key) or "")
        if path:
            expected_prefix = f"{remote_root}/controller-input/"
            if not path.startswith(expected_prefix) or posixpath.normpath(path) != path:
                raise error("image bootstrap Sentinel credential path escapes controller-input")
            if not secret or any(character in secret for character in "\r\n\x00"):
                raise error(f"image bootstrap requires one-line Sentinel {secret_key}")
            credential_writes.append((path, secret))
        elif secret:
            raise error("image bootstrap Sentinel credential has no declared file")
    heartbeat_config = config.get("heartbeat")
    if not isinstance(heartbeat_config, Mapping):
        raise error("image bootstrap config has no heartbeat mode")
    heartbeat_path = str(heartbeat_config.get("secretFile") or "").strip()
    heartbeat_secret = str(heartbeat_secrets.get("machineSecret") or "")
    if heartbeat_path:
        expected_prefix = f"{remote_root}/controller-input/"
        if (
            not heartbeat_path.startswith(expected_prefix)
            or posixpath.normpath(heartbeat_path) != heartbeat_path
        ):
            raise error("image bootstrap heartbeat credential path escapes controller-input")
        if not heartbeat_secret or any(
            character in heartbeat_secret for character in "\r\n\x00"
        ):
            raise error("image bootstrap requires one-line heartbeat machineSecret")
        credential_writes.append(
            (
                heartbeat_path,
                "AGORA_HEARTBEAT_SECRET=" + shlex.quote(heartbeat_secret),
            )
        )
    elif heartbeat_secret:
        raise error("image bootstrap heartbeat credential has no declared file")
    config_bytes = (
        json.dumps(
            dict(config), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        + "\n"
    ).encode("utf-8")
    config_sha = hashlib.sha256(config_bytes).hexdigest()
    config_b64 = base64.b64encode(config_bytes).decode("ascii")
    if not token or any(character in token for character in "\r\n\x00"):
        raise error("image bootstrap token must be one non-empty line")
    expected = {
        key: config[key]
        for key in (
            "machineId",
            "provider",
            "accountScope",
            "providerResourceId",
            "assignmentOperationId",
            "assignmentGeneration",
        )
    }
    expected["startTraining"] = bool(config.get("startTraining"))
    expected["heartbeatConfigured"] = heartbeat_config.get("mode") == "configured"
    transition_config = config.get("assignmentTransition")
    if not isinstance(transition_config, Mapping):
        raise error("image bootstrap config has no assignment transition")
    expected["assignmentTransitionKind"] = transition_config.get("kind")
    expected_b64 = base64.b64encode(
        json.dumps(
            expected, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).decode("ascii")
    token_b64 = base64.b64encode(token.encode("utf-8")).decode("ascii")
    private_writes = [
        {"path": config_path, "contentB64": config_b64},
        {"path": token_path, "contentB64": token_b64},
    ]
    for path, secret in credential_writes:
        encoded = base64.b64encode(secret.encode("utf-8")).decode("ascii")
        if callable(register_secret):
            register_secret(encoded)
        private_writes.append({"path": path, "contentB64": encoded})
    writes_b64 = base64.b64encode(
        json.dumps(private_writes, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).decode("ascii")
    if callable(register_secret):
        register_secret(config_b64)
        register_secret(token_b64)
        register_secret(expected_b64)
        register_secret(writes_b64)
    q = shlex.quote
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
CAPABILITY={q(CAPABILITY_PATH)}
BOOTSTRAP={q(BOOTSTRAP_PATH)}
PYTHON={q(PYTHON_PATH)}
CONFIG={q(config_path)}
TOKEN_FILE={q(token_path)}
RECEIPT={q(receipt_path)}
EXPECTED_FINGERPRINT={q(expected_fingerprint)}
EXPECTED_CONFIG_SHA={q(config_sha)}
test -r "$CAPABILITY" || {{ echo 'declared image capability marker is missing' >&2; exit 78; }}
test -x "$PYTHON" || {{ echo 'declared image Python runtime is missing' >&2; exit 78; }}
test -f "$BOOTSTRAP" || {{ echo 'declared image bootstrap is missing' >&2; exit 78; }}
"$PYTHON" - "$CAPABILITY" "$EXPECTED_FINGERPRINT" <<'PYCAP'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
if value.get("schemaVersion") != "{CAPABILITY_SCHEMA}":
    raise SystemExit("declared image capability schema mismatch")
runtime = value.get("runtimeExport")
if not isinstance(runtime, dict) or runtime.get("artifactFingerprint") != sys.argv[2]:
    raise SystemExit("declared image runtime fingerprint mismatch")
bootstrap = value.get("bootstrap")
bootstrap_path = bootstrap.get("path") if isinstance(bootstrap, dict) else value.get("bootstrapPath")
if bootstrap_path != "{BOOTSTRAP_PATH}":
    raise SystemExit("declared image bootstrap path mismatch")
PYCAP
"$PYTHON" - {q(remote_root)} {q(writes_b64)} "$RECEIPT" <<'PYPRIVATE'
import base64,json,os,pathlib,sys
runtime = pathlib.Path('/opt/agora-image-runtime')
sys.path.insert(0, str(runtime))
from assignment_transition import ensure_private_directory, preflight_private_root, private_atomic_write, verify_private_file
root = pathlib.Path(os.path.abspath(sys.argv[1]))
controller = root / 'controller-input'
preflight_private_root(root)
ensure_private_directory(controller, label='controller input')
writes = json.loads(base64.b64decode(sys.argv[2], validate=True))
for item in writes:
    path = pathlib.Path(os.path.abspath(str(item['path'])))
    try:
        path.relative_to(controller)
    except ValueError as exc:
        raise SystemExit('private controller input escapes its root') from exc
    private_atomic_write(path, base64.b64decode(item['contentB64'], validate=True))
    verify_private_file(path, label='private controller input')
receipt = pathlib.Path(os.path.abspath(sys.argv[3]))
if receipt != root / 'bootstrap-receipt.json':
    raise SystemExit('bootstrap receipt path is not canonical')
if receipt.exists() or receipt.is_symlink():
    verify_private_file(receipt, label='bootstrap receipt')
    receipt.unlink()
PYPRIVATE
"$PYTHON" "$BOOTSTRAP" --config "$CONFIG" --token-file "$TOKEN_FILE" --receipt "$RECEIPT"
test -r "$RECEIPT" || {{ echo 'image bootstrap receipt is missing' >&2; exit 79; }}
EXPECTED_JSON="$(printf '%s' {q(expected_b64)} | base64 -d)" \
"$PYTHON" - "$CAPABILITY" "$RECEIPT" "$EXPECTED_CONFIG_SHA" "$EXPECTED_FINGERPRINT" <<'PYRECEIPT'
import hashlib, json, os, pathlib, sys
capability_path, receipt_path = map(pathlib.Path, sys.argv[1:3])
receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
expected = json.loads(os.environ["EXPECTED_JSON"])
if receipt.get("schemaVersion") != "{RECEIPT_SCHEMA}" or receipt.get("status") != "ready":
    raise SystemExit("image bootstrap receipt is not ready")
for key, value in expected.items():
    if key in {"startTraining", "heartbeatConfigured", "assignmentTransitionKind"}:
        continue
    if receipt.get(key) != value:
        raise SystemExit(f"image bootstrap receipt mismatch: {{key}}")
training = receipt.get("training")
expected_requested = expected["startTraining"]
transition = receipt.get("assignmentTransition")
transition_kind = expected["assignmentTransitionKind"]
allowed_states = {{
    "stage": {{"staged", "ready"}},
    "ready": {{"ready"}},
    "rollback_prior": {{"fenced"}},
}}
if (
    not isinstance(transition, dict)
    or transition.get("kind") != transition_kind
    or transition.get("state") not in allowed_states.get(transition_kind, set())
):
    raise SystemExit("image bootstrap assignment transition mismatch")
expected_training_status = (
    "started"
    if expected_requested
    else "fenced"
    if transition_kind == "rollback_prior"
    else "already_started"
    if transition.get("state") == "ready"
    else "staged"
)
if (
    not isinstance(training, dict)
    or training.get("requested") is not expected_requested
    or training.get("status") != expected_training_status
):
    raise SystemExit("image bootstrap training outcome mismatch")
heartbeat = receipt.get("heartbeat")
heartbeat_configured = expected["heartbeatConfigured"]
expected_heartbeat_status = (
    ("started", "unavailable") if heartbeat_configured and expected_requested
    else ("started", "staged", "unavailable") if heartbeat_configured and transition.get("state") == "ready"
    else "staged" if heartbeat_configured
    else "disabled"
)
if (
    not isinstance(heartbeat, dict)
    or heartbeat.get("requested") is not heartbeat_configured
    or (
        heartbeat.get("status") not in expected_heartbeat_status
        if isinstance(expected_heartbeat_status, tuple)
        else heartbeat.get("status") != expected_heartbeat_status
    )
):
    raise SystemExit("image bootstrap heartbeat outcome mismatch")
if receipt.get("configSha256") != sys.argv[3]:
    raise SystemExit("image bootstrap config hash mismatch")
capability_sha = hashlib.sha256(capability_path.read_bytes()).hexdigest()
if receipt.get("capabilitySha256") != capability_sha:
    raise SystemExit("image bootstrap capability hash mismatch")
runtime = receipt.get("runtimeExport")
if not isinstance(runtime, dict) or runtime.get("artifactFingerprint") != sys.argv[4]:
    raise SystemExit("image bootstrap runtime provenance mismatch")
training = receipt.get("trainingSource")
commit = training.get("commit") if isinstance(training, dict) else None
if not isinstance(commit, str) or len(commit) != 40:
    raise SystemExit("image bootstrap training provenance is missing")
print(f"__AGORA_PROVENANCE__ agora_commit={{commit}}")
print("__AGORA_IMAGE_RUNTIME__ verified")
PYRECEIPT
"""


def render_image_receipt_verification_script(
    config: Mapping[str, Any], *, error: type[Exception] = ValueError
) -> str:
    """Verify the durable receipt for one already-started image assignment."""

    capability = config.get("imageCapability")
    if not isinstance(capability, Mapping):
        raise error("image receipt verification has no capability declaration")
    fingerprint = str(
        capability.get("runtimeArtifactFingerprint") or ""
    ).strip().lower()
    if not _SHA256.fullmatch(fingerprint):
        raise error("image receipt verification has invalid capability fingerprint")
    remote_root = _remote_root(config, error=error)
    receipt_path = f"{remote_root}/bootstrap-receipt.json"
    config_bytes = (
        json.dumps(
            dict(config), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        + "\n"
    ).encode("utf-8")
    config_sha = hashlib.sha256(config_bytes).hexdigest()
    expected = {
        key: config[key]
        for key in (
            "machineId",
            "provider",
            "accountScope",
            "providerResourceId",
            "assignmentOperationId",
            "assignmentGeneration",
        )
    }
    expected["configSha256"] = config_sha
    expected["runtimeArtifactFingerprint"] = fingerprint
    transition_config = config.get("assignmentTransition")
    if not isinstance(transition_config, Mapping):
        raise error("image receipt verification has no assignment transition")
    expected["assignmentTransitionKind"] = transition_config.get("kind")
    expected["heartbeatConfigured"] = (
        isinstance(config.get("heartbeat"), Mapping)
        and config["heartbeat"].get("mode") == "configured"
    )
    encoded = base64.b64encode(
        json.dumps(
            expected, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).decode("ascii")
    q = shlex.quote
    return f"""PYTHON={q(PYTHON_PATH)}
RECEIPT={q(receipt_path)}
CAPABILITY={q(CAPABILITY_PATH)}
test -r "$RECEIPT" || {{ echo 'image bootstrap receipt is missing' >&2; exit 79; }}
test -r "$CAPABILITY" || {{ echo 'declared image capability marker is missing' >&2; exit 78; }}
"$PYTHON" - "$RECEIPT" <<'PYPRIVATE'
import pathlib,sys
sys.path.insert(0, '/opt/agora-image-runtime')
from assignment_transition import verify_private_file
verify_private_file(pathlib.Path(sys.argv[1]), label='bootstrap receipt')
PYPRIVATE
EXPECTED_IMAGE_RECEIPT="$(printf '%s' {q(encoded)} | base64 -d)"
CAPABILITY_SHA="$(sha256sum "$CAPABILITY" | awk '{{print $1}}')"
jq -e --argjson expected "$EXPECTED_IMAGE_RECEIPT" --arg capabilitySha "$CAPABILITY_SHA" '
  .schemaVersion == "{RECEIPT_SCHEMA}" and .status == "ready" and
  .machineId == $expected.machineId and
  .provider == $expected.provider and
  .accountScope == $expected.accountScope and
  .providerResourceId == $expected.providerResourceId and
  .assignmentOperationId == $expected.assignmentOperationId and
  .assignmentGeneration == $expected.assignmentGeneration and
  .configSha256 == $expected.configSha256 and
  .capabilitySha256 == $capabilitySha and
  .runtimeExport.artifactFingerprint == $expected.runtimeArtifactFingerprint and
  .assignmentTransition.kind == $expected.assignmentTransitionKind and
  .assignmentTransition.state == "ready" and
  .training.requested == true and .training.status == "started" and
  (.heartbeat.requested == $expected.heartbeatConfigured) and
  (.heartbeat.status as $heartbeatStatus |
    if $expected.heartbeatConfigured then
      ($heartbeatStatus == "started" or $heartbeatStatus == "unavailable")
    else $heartbeatStatus == "disabled" end)
' "$RECEIPT" >/dev/null 2>&1 || {{ echo 'image bootstrap receipt no longer proves this assignment' >&2; exit 79; }}
"""


def render_image_neutral_verification_script(
    capability: Mapping[str, Any],
    *,
    remote_root: str = DEFAULT_REMOTE_ROOT,
    never_started_assertions: str = "",
    error: type[Exception] = ValueError,
) -> str:
    """Verify an unassigned baked image without writing assignment state."""

    expected = configured_image_capability(
        capability.get("contractVersion"),
        capability.get("runtimeArtifactFingerprint"),
        error=error,
    )
    if expected is None:
        raise error("neutral image verification requires an image capability")
    root = _remote_root({"remoteRoot": remote_root}, error=error)
    q = shlex.quote
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={q(root)}
CAPABILITY={q(CAPABILITY_PATH)}
BOOTSTRAP={q(BOOTSTRAP_PATH)}
PYTHON={q(PYTHON_PATH)}
{never_started_assertions}
test -r "$CAPABILITY" || {{ echo 'declared image capability marker is missing' >&2; exit 78; }}
test -x "$PYTHON" || {{ echo 'declared image Python runtime is missing' >&2; exit 78; }}
test -f "$BOOTSTRAP" || {{ echo 'declared image bootstrap is missing' >&2; exit 78; }}
"$PYTHON" - "$CAPABILITY" {q(expected['runtimeArtifactFingerprint'])} <<'PYCAP'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
if value.get("schemaVersion") != "{CAPABILITY_SCHEMA}":
    raise SystemExit("declared image capability schema mismatch")
runtime = value.get("runtimeExport")
if not isinstance(runtime, dict) or runtime.get("artifactFingerprint") != sys.argv[2]:
    raise SystemExit("declared image runtime fingerprint mismatch")
bootstrap = value.get("bootstrap")
bootstrap_path = bootstrap.get("path") if isinstance(bootstrap, dict) else value.get("bootstrapPath")
if bootstrap_path != "{BOOTSTRAP_PATH}":
    raise SystemExit("declared image bootstrap path mismatch")
PYCAP
{never_started_assertions}
OBSERVED_AT="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
printf '__AGORA_PREPARED_MACHINE_READY__ training_started=no observed_at=%s\\n' "$OBSERVED_AT"
"""
