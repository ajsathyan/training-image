from __future__ import annotations

import copy
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .sentinel import ALLOWED_ACTIONS, MachineSentinel

IDENTITY_FIELDS = (
    "fleetId",
    "launchId",
    "reservationId",
    "slotId",
    "slotGeneration",
    "machineGenerationId",
    "machineId",
    "provider",
    "providerResourceId",
    "nodeType",
    "gpuModel",
    "bootId",
)
WIRE_COMMAND_FIELDS = {
    "commandId",
    "action",
    "scope",
    "authorityEpoch",
    "issuedAt",
    "expiresAt",
    "arguments",
}
RUNTIME_BUNDLE_FIELDS = {
    "kind", "tokenLabel", "huggingFaceToken", "machineSigningSecret",
    "authorityEpoch", "credentialGeneration",
}


def _redact(value: Any, secret: str) -> str:
    text = str(value)
    return text.replace(secret, "[REDACTED]") if secret else text


def _validate_identity(identity: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(identity, dict):
        raise ValueError("sentinel identity must be an object")
    for field in IDENTITY_FIELDS:
        value = identity.get(field)
        if field == "slotGeneration":
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"sentinel identity {field} is invalid")
        elif not isinstance(value, str) or not value:
            raise ValueError(f"sentinel identity {field} is invalid")
    return copy.deepcopy(identity)


def _wire_command(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != WIRE_COMMAND_FIELDS:
        raise ValueError("sentinel response command does not match the exact wire schema")
    if value.get("action") not in ALLOWED_ACTIONS:
        raise ValueError("sentinel response contains a forbidden command action")
    return copy.deepcopy(value)


class SentinelIngressClient:
    def __init__(
        self,
        url: str,
        bearer_token: str,
        *,
        timeout: float = 5.0,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not str(url).startswith("https://"):
            raise ValueError("Machine Sentinel ingress URL must use HTTPS")
        if not bearer_token:
            raise ValueError("Machine Sentinel bearer token is required")
        self.url = str(url)
        self.bearer_token = str(bearer_token)
        self.timeout = float(timeout)
        self.opener = opener

    def _credential_url(self, action: str) -> str:
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.path == "/api/machine-sentinel/observe":
            path = f"/api/machine-sentinel/{action}"
        elif parsed.path == "/internal/sentinel/observe":
            path = f"/internal/sentinel/{action}"
        else:
            raise ValueError("Machine Sentinel ingress URL has no credential sibling")
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    def _credential_request(self, action: str, identity: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({"identity": _validate_identity(identity)}, sort_keys=True, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self._credential_url(action),
            method="POST",
            data=body,
            headers={
                "authorization": f"Bearer {self.bearer_token}",
                "content-type": "application/json",
                "user-agent": "agora-machine-sentinel/1",
            },
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read(32 * 1024).decode("utf-8", errors="replace")
                status = int(getattr(response, "status", 200))
        except urllib.error.HTTPError as error:
            raw = error.read(8 * 1024).decode("utf-8", errors="replace")
            raise RuntimeError(f"Machine Sentinel credential exchange failed ({error.code}): {_redact(raw, self.bearer_token)}") from error
        except OSError as error:
            raise RuntimeError(_redact(error, self.bearer_token)) from error
        if status < 200 or status >= 300:
            raise RuntimeError(f"Machine Sentinel credential exchange returned {status}: {_redact(raw, self.bearer_token)}")
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError("Machine Sentinel credential exchange returned invalid JSON") from error
        if not isinstance(result, dict) or not isinstance(result.get("principalToken"), str) or not result["principalToken"]:
            raise RuntimeError("Machine Sentinel credential exchange did not return a machine principal")
        if not isinstance(result.get("expiresAt"), int):
            raise RuntimeError("Machine Sentinel credential exchange did not return a valid expiry")
        return result

    def exchange_bootstrap(self, identity: dict[str, Any]) -> dict[str, Any]:
        return self._credential_request("bootstrap", identity)

    def refresh_machine_credential(self, identity: dict[str, Any]) -> dict[str, Any]:
        return self._credential_request("refresh", identity)

    def machine_runtime_bundle(self, identity: dict[str, Any], token_label: str) -> dict[str, Any]:
        if not isinstance(token_label, str) or not token_label or not token_label.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Machine Sentinel token label is invalid")
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.path == "/api/machine-sentinel/observe":
            path = "/api/machine-sentinel/secret"
        elif parsed.path == "/internal/sentinel/observe":
            path = "/internal/sentinel/secret"
        else:
            raise ValueError("Machine Sentinel ingress URL has no secret sibling")
        body = json.dumps({
            "identity": _validate_identity(identity),
            "request": {"kind": "machine_runtime_bundle", "tokenLabel": token_label},
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", "")),
            method="POST",
            data=body,
            headers={
                "authorization": f"Bearer {self.bearer_token}",
                "content-type": "application/json",
                "user-agent": "agora-machine-sentinel/1",
            },
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read(32 * 1024).decode("utf-8", errors="replace")
                status = int(getattr(response, "status", 200))
        except urllib.error.HTTPError as error:
            raw = error.read(8 * 1024).decode("utf-8", errors="replace")
            raise RuntimeError(f"Machine Sentinel secret request failed ({error.code}): {_redact(raw, self.bearer_token)}") from error
        except OSError as error:
            raise RuntimeError(_redact(error, self.bearer_token)) from error
        if status < 200 or status >= 300:
            raise RuntimeError(f"Machine Sentinel secret request returned {status}: {_redact(raw, self.bearer_token)}")
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError("Machine Sentinel secret request returned invalid JSON") from error
        if not isinstance(result, dict) or set(result) != RUNTIME_BUNDLE_FIELDS:
            raise RuntimeError("Machine Sentinel secret response does not match the exact schema")
        if result["kind"] != "machine_runtime_bundle" or result["tokenLabel"] != token_label:
            raise RuntimeError("Machine Sentinel secret response does not match machine authority")
        if not isinstance(result["huggingFaceToken"], str) or not result["huggingFaceToken"].startswith("hf_"):
            raise RuntimeError("Machine Sentinel secret response has an invalid Hugging Face token")
        if not isinstance(result["machineSigningSecret"], str) or len(result["machineSigningSecret"]) < 32:
            raise RuntimeError("Machine Sentinel secret response has an invalid signing secret")
        for field in ("authorityEpoch", "credentialGeneration"):
            if not isinstance(result[field], int) or isinstance(result[field], bool) or result[field] < 1:
                raise RuntimeError(f"Machine Sentinel secret response has an invalid {field}")
        return result

    def observe(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            method="POST",
            data=body,
            headers={
                "authorization": f"Bearer {self.bearer_token}",
                "content-type": "application/json",
                "user-agent": "agora-machine-sentinel/1",
            },
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read(256 * 1024).decode("utf-8", errors="replace")
                status = int(getattr(response, "status", 200))
        except urllib.error.HTTPError as error:
            raw = error.read(8 * 1024).decode("utf-8", errors="replace")
            raise RuntimeError(f"Machine Sentinel ingress rejected the observation ({error.code}): {_redact(raw, self.bearer_token)}") from error
        except OSError as error:
            raise RuntimeError(_redact(error, self.bearer_token)) from error
        if status < 200 or status >= 300:
            raise RuntimeError(f"Machine Sentinel ingress returned {status}: {_redact(raw, self.bearer_token)}")
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as error:
            raise RuntimeError("Machine Sentinel ingress returned invalid JSON") from error
        if not isinstance(result, dict) or result.get("accepted") is not True:
            raise RuntimeError("Machine Sentinel ingress did not accept the observation")
        commands = result.get("commands", [])
        if not isinstance(commands, list) or len(commands) > 25:
            raise RuntimeError("Machine Sentinel ingress returned an invalid command batch")
        result["commands"] = [_wire_command(command) for command in commands]
        return result


def ingress_payload(
    sentinel: MachineSentinel,
    *,
    identity: dict[str, Any],
    credential_kind: str,
    sequence: int,
    sent_at: str,
    fleet_size: int,
    launch_active: bool,
) -> dict[str, Any]:
    if credential_kind not in {"bootstrap", "machine"}:
        raise ValueError("Machine Sentinel credential kind is invalid")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise ValueError("Machine Sentinel sequence is invalid")
    state = sentinel.snapshot()
    process = state.get("process", {})
    evidence = state.get("latestEvidence") or {}
    protection = state.get("protection", {})
    errors = []
    acknowledgements = []
    command_acknowledgements = []
    for error in state.get("errors", {}).values():
        errors.append({
            "occurrenceId": error.get("errorId"),
            "category": error.get("category") or "unknown",
            "actualError": error.get("rawError") or "unknown error",
            "acknowledged": error.get("acknowledgedAt") is not None,
        })
        if error.get("acknowledgedAt") is not None:
            acknowledgements.append({
                "occurrenceId": error.get("errorId"),
                "acknowledgedAt": error.get("acknowledgedAt"),
                "acknowledgedBy": error.get("acknowledgedBy"),
            })
    for record in state.get("commands", {}).values():
        status = record.get("status")
        if status == "executed":
            wire_status = "executed"
            acknowledged_at = record.get("executedAt") or record.get("receivedAt")
        elif status == "rejected":
            wire_status = "rejected"
            acknowledged_at = record.get("rejectedAt") or record.get("receivedAt")
        elif status in {"received", "executing", "effect_unknown"}:
            wire_status = "received"
            acknowledged_at = record.get("receivedAt")
        else:
            continue
        command_id = record.get("commandId")
        if isinstance(command_id, str) and command_id and isinstance(acknowledged_at, str):
            command_acknowledgements.append({
                "commandId": command_id,
                "acknowledgementId": f"{command_id}:{wire_status}",
                "status": wire_status,
                "acknowledgedAt": acknowledged_at,
            })
    return {
        "credentialKind": credential_kind,
        "identity": _validate_identity(identity),
        "sequence": sequence,
        "lastAcknowledgementCursor": int(state.get("lastServerCommandCursor", 0)),
        "sentAt": sent_at,
        "fleetSize": max(0, int(fleet_size)),
        "launchActive": bool(launch_active),
        "lifecycleState": evidence.get("joinState") or state.get("training", {}).get("state") or "unknown",
        "provisioningOrigin": state.get("provisioningOrigin", "unknown_non_retiring"),
        "protectionState": "joined_protected" if protection.get("joined") else "warm_waiting_protected" if protection.get("warm") else "unjoined",
        "setup": {**copy.deepcopy(state.get("setup", {})), "revision": state["identity"]["setupRevision"]},
        "publicMapping": copy.deepcopy(state.get("publicMapping", {})),
        "training": copy.deepcopy(state.get("training", {})),
        "tmux": {
            "state": "running" if process.get("tmuxAgora") else "missing",
            "session": process.get("tmuxSessionId"),
            "window": process.get("tmuxWindowId"),
            "pane": process.get("tmuxPaneId"),
            "panePid": process.get("tmuxPanePid"),
        },
        "watchdog": {
            "state": "running" if process.get("watchdog") else "missing",
            "instanceId": process.get("watchdogInstanceId"),
            "retryContinuityToken": process.get("retryContinuityToken"),
        },
        "ports": [],
        "join": {
            "state": evidence.get("joinState") or "unknown",
            "role": evidence.get("role"),
            "activity": evidence.get("activity"),
        },
        "errors": errors,
        "acknowledgements": acknowledgements,
        "commandAcknowledgements": command_acknowledgements[-100:],
    }


def execute_ingress_cycle(
    sentinel: MachineSentinel,
    client: SentinelIngressClient,
    *,
    identity: dict[str, Any],
    credential_kind: str,
    sequence: int,
    sent_at: str,
    fleet_size: int,
    launch_active: bool,
) -> dict[str, Any]:
    response = client.observe(ingress_payload(
        sentinel,
        identity=identity,
        credential_kind=credential_kind,
        sequence=sequence,
        sent_at=sent_at,
        fleet_size=fleet_size,
        launch_active=launch_active,
    ))
    executed = []
    outcomes = []
    for command in response["commands"]:
        outcome = sentinel.execute_command(command, now=sent_at)
        executed.append(command["commandId"])
        outcomes.append(outcome)
    return {
        "accepted": True,
        "nextPollSeconds": response.get("nextPollSeconds"),
        "commandCursor": response.get("commandCursor"),
        "executedCommands": executed,
        "outcomes": outcomes,
        "commandAttention": response.get("commandAttention"),
    }
