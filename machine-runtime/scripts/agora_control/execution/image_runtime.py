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
CAPABILITY_PATH = "/opt/agora-image-runtime/capability.json"
BOOTSTRAP_PATH = "/opt/agora-image-runtime/agora_image_bootstrap.py"
PYTHON_PATH = "/opt/agora-venv/bin/python"
TRAINING_SOURCE_ROOT = "/opt/agora-source"
DEFAULT_REMOTE_ROOT = "/workspace/agora-run"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ASSIGNMENT_STATES = {"fenced", "staged", "ready"}
_ASSIGNMENT_TRANSITIONS = {"stage", "ready", "rollback_prior"}


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
    credential_lines: list[str] = []
    for path, secret in credential_writes:
        encoded = base64.b64encode(secret.encode("utf-8")).decode("ascii")
        if callable(register_secret):
            register_secret(encoded)
        credential_lines.extend(
            [
                f"printf '%s' {shlex.quote(encoded)} | base64 -d > {shlex.quote(path)}",
                f"chmod 600 {shlex.quote(path)}",
            ]
        )
    if callable(register_secret):
        register_secret(config_b64)
        register_secret(token_b64)
        register_secret(expected_b64)
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
install -d -m 700 {q(remote_root)} {q(posixpath.dirname(config_path))}
printf '%s' {q(config_b64)} | base64 -d > "$CONFIG"
printf '%s' {q(token_b64)} | base64 -d > "$TOKEN_FILE"
chmod 600 "$CONFIG" "$TOKEN_FILE"
{chr(10).join(credential_lines)}
rm -f "$RECEIPT"
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
    "started" if heartbeat_configured and expected_requested
    else ("started", "staged") if heartbeat_configured and transition.get("state") == "ready"
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
    return f"""RECEIPT={q(receipt_path)}
CAPABILITY={q(CAPABILITY_PATH)}
test -r "$RECEIPT" || {{ echo 'image bootstrap receipt is missing' >&2; exit 79; }}
test -r "$CAPABILITY" || {{ echo 'declared image capability marker is missing' >&2; exit 78; }}
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
  (.heartbeat.status == (if $expected.heartbeatConfigured then "started" else "disabled" end))
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
