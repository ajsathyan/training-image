"""Canonical remote assets monitoring owner with explicit collaborators."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import posixpath
from pathlib import Path
from typing import Any

from ..execution.assets import render_assignment_start_guard

MACHINE_SENTINEL_PROVISIONING_ORIGINS = frozenset({
    "autoscaler_provider_create",
    "drop_recovery",
    "other_non_retiring",
    "read_model_migration",
    "unknown_non_retiring",
    "warm_waiting",
})
# Old fleet rows may retain this retired source. Treat it as historical evidence
# and render a non-retiring current origin so it cannot enable cleanup behavior.
LEGACY_NON_RETIRING_PROVISIONING_ORIGINS = frozenset({"zulip_opening"})
MACHINE_SENTINEL_PYTHON_CANDIDATES = (
    "/opt/conda/bin/python3",
    "/opt/conda/bin/python",
    "/opt/agora-venv/bin/python3",
    "/usr/local/bin/python3",
    "/usr/bin/python3",
)


def machine_sentinel_setup_revision(
    *,
    agent_file: Path | None = None,
    package_dir: Path | None = None,
    renderer_file: Path | None = None,
) -> str:
    """Fingerprint the exact local Sentinel renderer and runtime sources.

    The revision is deliberately derived at use time.  It must not be copied
    into autoscaler configuration, because that would allow a stale/manual
    value to describe a different runtime than the one being rendered.
    """

    module_dir = Path(__file__).resolve().parents[2]
    agent_path = Path(agent_file or module_dir / "agora_machine_sentinel_agent.py").resolve()
    package_path = Path(package_dir or module_dir / "machine_sentinel").resolve()
    renderer_path = Path(renderer_file or __file__).resolve()
    if not agent_path.is_file() or not package_path.is_dir() or not renderer_path.is_file():
        raise ValueError("Machine Sentinel runtime source is missing")
    package_files = sorted(package_path.glob("*.py"), key=lambda path: path.name)
    if not package_files:
        raise ValueError("Machine Sentinel package source is missing")

    digest = hashlib.sha256()
    for label, path in (
        ("renderer", renderer_path),
        ("agent", agent_path),
        *( (f"package/{path.name}", path) for path in package_files ),
    ):
        source = path.read_bytes()
        label_bytes = label.encode("utf-8")
        digest.update(len(label_bytes).to_bytes(4, "big"))
        digest.update(label_bytes)
        digest.update(len(source).to_bytes(8, "big"))
        digest.update(source)
    return f"setup-{digest.hexdigest()[:12]}"


def machine_sentinel_provisioning_origin(machine: dict[str, Any]) -> str:
    explicit = str(machine.get("provisioningOrigin") or "").strip()
    if explicit in MACHINE_SENTINEL_PROVISIONING_ORIGINS:
        return explicit
    if explicit in LEGACY_NON_RETIRING_PROVISIONING_ORIGINS:
        return "other_non_retiring"
    generation = str(machine.get("machineGenerationId") or "").strip()
    if (
        generation.startswith("migration-machine-")
        and len(generation) == len("migration-machine-") + 24
        and all(character in "0123456789abcdef" for character in generation.removeprefix("migration-machine-"))
    ):
        return "read_model_migration"
    return "unknown_non_retiring"


def _render_machine_sentinel_python_selection_script(
    *,
    candidates: tuple[str, ...],
    sh_single,
) -> str:
    normalized = tuple(str(candidate) for candidate in candidates)
    if any(
        not posixpath.isabs(candidate)
        or candidate == "/"
        or "\n" in candidate
        or "\r" in candidate
        for candidate in normalized
    ):
        raise ValueError("Machine Sentinel Python candidate must be a safe absolute path")
    rendered = " \\\n    ".join(sh_single(candidate) for candidate in normalized)
    iterator = f" \\\n    {rendered}" if rendered else ""
    return f"""PYTHON_BIN=""
for candidate in{iterator}
do
  if [ -x "$candidate" ] \\
    && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info.major == 3 else 1)' >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  printf 'Machine Sentinel requires an existing allowlisted Python 3 interpreter\\n' >&2
  exit 65
fi"""


def machine_sentinel_python_selection_script(*, sh_single) -> str:
    return _render_machine_sentinel_python_selection_script(
        candidates=MACHINE_SENTINEL_PYTHON_CANDIDATES,
        sh_single=sh_single,
    )


def heartbeat_url_from_status_url(status_url: Any) -> str:
    text = str(status_url or "").strip()
    if not text:
        return ""
    for suffix in ("/v1/status", "/status"):
        if text.endswith(suffix):
            return text[: -len(suffix)] + suffix.replace("status", "heartbeat")
    return text

def derive_heartbeat_machine_secret(master_secret: str, machine_id: str) -> str:
    return hmac.new(
        master_secret.encode("utf-8"),
        f"agora-heartbeat-machine:{machine_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

def load_heartbeat_agent_settings(config: dict[str, Any] | None = None, *, HEARTBEAT_SECRETS_FILE, decode_secret_value, heartbeat_url_from_status_url, load_autoscaler_config, parse_env_file, project_path) -> dict[str, Any] | None:
    config = config if isinstance(config, dict) else load_autoscaler_config()
    recovery_config = config.get("ownMachineRecovery", {}) if isinstance(config.get("ownMachineRecovery"), dict) else {}
    if not bool(recovery_config.get("installHeartbeatAgent", True)):
        return None
    heartbeat_url = str(recovery_config.get("heartbeatUrl") or "").strip()
    if not heartbeat_url:
        heartbeat_url = heartbeat_url_from_status_url(recovery_config.get("statusUrl"))
    if not heartbeat_url:
        return None
    secret_env = str(recovery_config.get("heartbeatSecretEnv") or "AGORA_HEARTBEAT_SECRET")
    secret_file = project_path(
        recovery_config.get("heartbeatSecretEnvFile") or recovery_config.get("statusTokenEnvFile"),
        HEARTBEAT_SECRETS_FILE,
    )
    env = parse_env_file(secret_file)
    secret = decode_secret_value(env, secret_env)
    if not secret:
        return None
    return {
        "url": heartbeat_url,
        "masterSecret": secret,
        "secretEnv": secret_env,
        "secretEnvFile": str(secret_file),
        "intervalSeconds": float(recovery_config.get("heartbeatIntervalSeconds", 3)),
        "jitterSeconds": float(recovery_config.get("heartbeatJitterSeconds", 1)),
        "timeoutSeconds": float(recovery_config.get("heartbeatTimeoutSeconds", 5)),
    }

def load_machine_sentinel_settings(config: dict[str, Any] | None = None, *, allow_missing_credentials: bool = False, FleetError, HEARTBEAT_SECRETS_FILE, add_runtime_secret_for_redaction, decode_secret_value, load_autoscaler_config, parse_env_file, project_path, MACHINE_SENTINEL_AGENT_FILE: Path | None = None, MACHINE_SENTINEL_PACKAGE_DIR: Path | None = None) -> dict[str, Any] | None:
    config = config if isinstance(config, dict) else load_autoscaler_config()
    sentinel_config = config.get("machineSentinel", {}) if isinstance(config.get("machineSentinel"), dict) else {}
    export_enabled = bool(sentinel_config.get("enabled", False))
    collector_enabled = bool(sentinel_config.get("collectorEnabled", export_enabled))
    if not collector_enabled:
        return None
    ingress_url = str(sentinel_config.get("ingressUrl") or "").strip()
    if export_enabled and not ingress_url.startswith("https://"):
        raise FleetError("machineSentinel.ingressUrl must use HTTPS")
    secret_file = project_path(
        sentinel_config.get("tokenEnvFile"),
        HEARTBEAT_SECRETS_FILE,
    )
    bootstrap_env = str(sentinel_config.get("bootstrapTokenEnv") or "AGORA_SENTINEL_BOOTSTRAP_TOKEN")
    machine_env = str(sentinel_config.get("machineTokenEnv") or "AGORA_SENTINEL_MACHINE_TOKEN")
    env = parse_env_file(secret_file) if export_enabled else {}
    bootstrap_token = decode_secret_value(env, bootstrap_env) if export_enabled else ""
    machine_token = decode_secret_value(env, machine_env) if export_enabled else ""
    if export_enabled and not allow_missing_credentials and not bootstrap_token and not machine_token:
        raise FleetError("Machine Sentinel requires a bootstrap or current machine credential")
    if bootstrap_token:
        add_runtime_secret_for_redaction(bootstrap_token)
    if machine_token:
        add_runtime_secret_for_redaction(machine_token)
    # Collection is intentionally useful without Cloud export.  Blank every
    # network capability at this boundary so a retained URL or credential in
    # operator configuration cannot turn local-only collection back on.
    if not export_enabled:
        ingress_url = ""
        bootstrap_token = ""
        machine_token = ""
    try:
        setup_revision = machine_sentinel_setup_revision(
            agent_file=MACHINE_SENTINEL_AGENT_FILE,
            package_dir=MACHINE_SENTINEL_PACKAGE_DIR,
        )
    except ValueError as exc:
        raise FleetError(str(exc)) from exc
    return {
        "url": ingress_url,
        "exportEnabled": export_enabled,
        "bootstrapToken": bootstrap_token,
        "machineToken": machine_token,
        "tokenEnvFile": str(secret_file),
        "setupRevision": setup_revision,
        "timeoutSeconds": float(sentinel_config.get("timeoutSeconds") or 5),
        "fleetId": str(sentinel_config.get("fleetId") or "agora-fleet"),
    }

def machine_sentinel_identity(machine: dict[str, Any], settings: dict[str, Any], *, FleetError) -> dict[str, Any]:
    provider = str(machine.get("provider") or ("runpod" if machine.get("runpodId") else "vast" if machine.get("vastId") else ""))
    node_type = str(machine.get("agoraJoinRole") or machine.get("provisioningRole") or machine.get("role") or "").strip().lower()
    if node_type not in {"head", "body", "tail"}:
        node_type = ""
    values = {
        "fleetId": settings.get("fleetId"),
        "launchId": machine.get("launchId"),
        "reservationId": machine.get("reservationId"),
        "slotId": machine.get("slotId"),
        "slotGeneration": machine.get("slotGeneration"),
        "machineGenerationId": machine.get("machineGenerationId"),
        "machineId": machine.get("id"),
        "provider": provider,
        "accountScope": machine.get("accountScope") or machine.get("providerAccount") or "default",
        "providerResourceId": machine.get("cloudProviderResourceId") or machine.get("providerResourceId") or machine.get("runpodId") or machine.get("vastId"),
        "nodeType": node_type,
        "gpuModel": machine.get("gpuModel") or machine.get("gpuTypeId"),
        "bootId": "__HOST_BOOT_ID__",
    }
    for key, value in values.items():
        if key == "slotGeneration":
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise FleetError(f"Machine Sentinel identity is missing {key}")
        elif not isinstance(value, str) or not value:
            raise FleetError(f"Machine Sentinel identity is missing {key}")
    if settings.get("includeProviderBindingIdentity") is True:
        provider_identity_source = machine.get("providerIdentitySource")
        if provider_identity_source is None:
            raise FleetError("Machine Sentinel identity is missing providerIdentitySource")
        optional_values = {
            "accountScope": machine.get("accountScope") or machine.get("providerAccount"),
            "providerMachineId": machine.get("providerMachineId") or machine.get("providerResourceId") or machine.get("runpodId") or machine.get("vastId"),
            "providerMachineName": machine.get("providerMachineName") or machine.get("runpodName") or machine.get("vastName") or machine.get("label"),
            "providerIdentitySource": provider_identity_source,
            "setupRevision": settings.get("setupRevision"),
        }
        values.update({key: value for key, value in optional_values.items() if value is not None})
    return values

def validated_machine_sentinel_remote_root(
    machine: dict[str, Any],
    *,
    DEFAULT_REMOTE_ROOT,
    FleetError,
) -> str:
    configured = machine.get("remoteRoot")
    remote_root = str(DEFAULT_REMOTE_ROOT if configured is None else configured).strip()
    if remote_root in {"", "/", "~", "$HOME", "/workspace"}:
        raise FleetError(f"Unsafe Machine Sentinel remoteRoot: {remote_root or '[empty]'}")
    if not remote_root.startswith("/") or posixpath.normpath(remote_root) != remote_root:
        raise FleetError(f"Machine Sentinel remoteRoot must be a normalized absolute path: {remote_root}")
    components = [part for part in remote_root.split("/") if part]
    if len(components) < 2:
        raise FleetError(f"Machine Sentinel remoteRoot must be at least two components deep: {remote_root}")
    return remote_root

def remote_machine_sentinel_install_body(
    machine: dict[str, Any],
    settings: dict[str, Any] | None,
    *,
    DEFAULT_REMOTE_ROOT,
    FleetError,
    MACHINE_SENTINEL_AGENT_FILE,
    MACHINE_SENTINEL_PACKAGE_DIR,
    machine_sentinel_identity,
    sh_single,
    record_progress: bool = True,
    asset_remote_root: str | None = None,
    write_credentials: bool = True,
    manage_session: bool = True,
) -> str:
    remote_root = validated_machine_sentinel_remote_root(
        machine,
        DEFAULT_REMOTE_ROOT=DEFAULT_REMOTE_ROOT,
        FleetError=FleetError,
    )
    if not settings:
        return ""
    if not MACHINE_SENTINEL_AGENT_FILE.exists() or not MACHINE_SENTINEL_PACKAGE_DIR.is_dir():
        raise FleetError("Machine Sentinel runtime source is missing")
    # Recompute this from the sources being embedded.  This keeps repair and
    # install tied to the exact local runtime even when an older caller still
    # supplies a tracked/manual setupRevision value.
    try:
        setup_revision = machine_sentinel_setup_revision(
            agent_file=MACHINE_SENTINEL_AGENT_FILE,
            package_dir=MACHINE_SENTINEL_PACKAGE_DIR,
        )
    except ValueError as exc:
        raise FleetError(str(exc)) from exc
    settings = {**settings, "setupRevision": setup_revision}
    export_enabled = bool(settings.get("exportEnabled", True))
    effective_url = str(settings.get("url") or "") if export_enabled else ""
    effective_bootstrap_token = (
        str(settings.get("bootstrapToken") or "") if export_enabled else ""
    )
    effective_machine_token = (
        str(settings.get("machineToken") or "") if export_enabled else ""
    )
    identity = machine_sentinel_identity(machine, settings)
    agent_source = MACHINE_SENTINEL_AGENT_FILE.read_text(encoding="utf-8")
    provisioning_origin = machine_sentinel_provisioning_origin(machine)
    python_selection_script = machine_sentinel_python_selection_script(
        sh_single=sh_single,
    )
    assignment_guard = render_assignment_start_guard(
        machine, sh_single=sh_single
    )
    package_sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(MACHINE_SENTINEL_PACKAGE_DIR.glob("*.py"))
    }
    package_blocks = "\n".join(
        f"cat > \"$SENTINEL_DIR/machine_sentinel/{name}\" <<'SENTINEL_{index}_EOF'\n{source}\nSENTINEL_{index}_EOF"
        for index, (name, source) in enumerate(package_sources.items())
    )
    install_start = (
        "printf '%s event=machine_sentinel_install_start\\n' \"$(date -Iseconds)\" >> \"$ROOT/progress.log\""
        if record_progress
        else ":"
    )
    install_done = (
        "printf '%s event=machine_sentinel_installed mode="
        + ("remote_export" if settings.get("exportEnabled", True) else "local_collector")
        + "\\n' \"$(date -Iseconds)\" >> \"$ROOT/progress.log\""
        if record_progress
        else ":"
    )
    asset_root = asset_remote_root or remote_root
    credential_block = ""
    if write_credentials and export_enabled:
        credential_block = f"""cat > "$SENTINEL_DIR/credential.env" <<'SENTINEL_CREDENTIAL_EOF'
AGORA_SENTINEL_MACHINE_TOKEN={sh_single(effective_machine_token)}
SENTINEL_CREDENTIAL_EOF
chmod 600 "$SENTINEL_DIR/credential.env"
"""
    session_block = ""
    if manage_session:
        session_block = f"""tmux kill-session -t agora_sentinel >/dev/null 2>&1 || true
SENTINEL_BOOTSTRAP_TOKEN={sh_single(effective_bootstrap_token)}
if [ -n "$SENTINEL_BOOTSTRAP_TOKEN" ]; then
  # The one-time bootstrap crosses SSH stdin and the tmux session environment
  # only. It is inherited by the agent, popped immediately at process start,
  # and removed from tmux as soon as the child has been created.
  tmux new-session -d -s agora_sentinel \\
    -e "AGORA_SENTINEL_BOOTSTRAP_TOKEN=$SENTINEL_BOOTSTRAP_TOKEN" \\
    "$ROOT/start-machine-sentinel.sh"
  tmux set-environment -t agora_sentinel -u AGORA_SENTINEL_BOOTSTRAP_TOKEN >/dev/null 2>&1 || true
  unset SENTINEL_BOOTSTRAP_TOKEN
else
  tmux new-session -d -s agora_sentinel "$ROOT/start-machine-sentinel.sh"
fi
{install_done}
"""
    return f"""ROOT={sh_single(remote_root)}
ASSET_ROOT={sh_single(asset_root)}
SENTINEL_DIR="$ASSET_ROOT/machine-sentinel"
mkdir -p "$SENTINEL_DIR/machine_sentinel" "$ASSET_ROOT/logs"
{install_start}
cat > "$SENTINEL_DIR/agora_machine_sentinel_agent.py" <<'SENTINEL_AGENT_EOF'
{agent_source}
SENTINEL_AGENT_EOF
{package_blocks}
chmod 700 "$SENTINEL_DIR/agora_machine_sentinel_agent.py"
cat > "$SENTINEL_DIR/identity.json" <<'SENTINEL_IDENTITY_EOF'
{json.dumps(identity, sort_keys=True, separators=(",", ":"))}
SENTINEL_IDENTITY_EOF
{credential_block}chmod 600 "$SENTINEL_DIR/identity.json"
cat > "$ASSET_ROOT/sentinel-verify-setup.sh" <<'SENTINEL_VERIFY_EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={sh_single(remote_root)}
test -x "$ROOT/launch-agora-gpu0.sh"
test -x "$ROOT/watchdog-agora-tmux.sh"
test -f "$ROOT/agora.env"
SENTINEL_VERIFY_EOF
chmod 700 "$ASSET_ROOT/sentinel-verify-setup.sh"
cat > "$ASSET_ROOT/sentinel-start-training.sh" <<'SENTINEL_START_EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={sh_single(remote_root)}
TRAINING_RUN_ID="$1"
TRAINING_PLAN_ID="$2"
CONFIGURATION_REVISION="$3"
ANNOUNCE_PORT="$4"
{assignment_guard}
assignment_guard_ready
assignment_token_matches
for VALUE in "$TRAINING_RUN_ID" "$TRAINING_PLAN_ID" "$CONFIGURATION_REVISION"; do
  printf '%s\n' "$VALUE" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._:-]{{0,191}}$'
done
case "$ANNOUNCE_PORT" in (*[!0-9]*|'') exit 64;; esac
if [ -f "$ROOT/.agora/machine-sentinel/cancelled-training-run" ] \
  && grep -qxF "$TRAINING_RUN_ID" "$ROOT/.agora/machine-sentinel/cancelled-training-run"; then
  exit 75
fi
if [ -f "$ROOT/.agora/machine-sentinel/stopped-training-run" ] \
  && grep -qxF "$TRAINING_RUN_ID" "$ROOT/.agora/machine-sentinel/stopped-training-run"; then
  exit 75
fi
if tmux has-session -t agora_gpu >/dev/null 2>&1; then
  assignment_guard_release
  exit 0
fi
tmp_env="$(mktemp "$ROOT/.agora.env.XXXXXX")"
grep -Ev '^(ANNOUNCE_PORT|AGORA_TRAINING_RUN_ID|AGORA_TRAINING_PLAN_ID|AGORA_CONFIGURATION_REVISION)=' \
  "$ROOT/agora.env" > "$tmp_env" || true
printf 'ANNOUNCE_PORT=%s\nAGORA_TRAINING_RUN_ID=%s\nAGORA_TRAINING_PLAN_ID=%s\nAGORA_CONFIGURATION_REVISION=%s\n' \
  "$ANNOUNCE_PORT" "$TRAINING_RUN_ID" "$TRAINING_PLAN_ID" "$CONFIGURATION_REVISION" >> "$tmp_env"
chmod 600 "$tmp_env"
mv "$tmp_env" "$ROOT/agora.env"
for pid in $(pgrep -f "$ROOT/watch-agora-tmux-loop.sh" 2>/dev/null || true); do
  if [ "$pid" != "$$" ]; then kill "$pid" 2>/dev/null || true; fi
done
nohup "$ROOT/watch-agora-tmux-loop.sh" >> "$ROOT/watchdog.log" 2>&1 < /dev/null &
assignment_assert_no_owned_servers
tmux new-session -d -s agora_gpu "$ROOT/supervise-agora-gpu0.sh"
assignment_guard_release
printf '%s event=sentinel_training_started announce_port=%s\n' "$(date -Iseconds)" "$ANNOUNCE_PORT" >> "$ROOT/progress.log"
SENTINEL_START_EOF
chmod 700 "$ASSET_ROOT/sentinel-start-training.sh"
cat > "$ASSET_ROOT/sentinel-stop-training.sh" <<'SENTINEL_STOP_EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={sh_single(remote_root)}
TRAINING_RUN_ID="$1"
GRACE_SECONDS="$2"
REASON_CODE="$3"
APPROVAL_RECEIPT_ID="$4"
for VALUE in "$TRAINING_RUN_ID" "$REASON_CODE" "$APPROVAL_RECEIPT_ID"; do
  printf '%s\n' "$VALUE" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._:-]{{0,191}}$'
done
case "$GRACE_SECONDS" in (*[!0-9]*|'') exit 64;; esac
[ "$GRACE_SECONDS" -le 600 ]
grep -qxF "AGORA_TRAINING_RUN_ID=$TRAINING_RUN_ID" "$ROOT/agora.env"
tmux has-session -t agora_gpu >/dev/null 2>&1
mkdir -p "$ROOT/.agora/machine-sentinel"
printf '%s\n' "$TRAINING_RUN_ID" > "$ROOT/.agora/machine-sentinel/stopped-training-run"
for PID in $(pgrep -f "$ROOT/watch-agora-tmux-loop.sh" 2>/dev/null || true); do
  if [ "$PID" != "$$" ]; then kill "$PID" 2>/dev/null || true; fi
done
tmux send-keys -t agora_gpu C-c
DEADLINE=$((SECONDS + GRACE_SECONDS))
while tmux has-session -t agora_gpu >/dev/null 2>&1; do
  if [ "$SECONDS" -ge "$DEADLINE" ]; then exit 75; fi
  sleep 1
done
SENTINEL_STOP_EOF
chmod 700 "$ASSET_ROOT/sentinel-stop-training.sh"
cat > "$ASSET_ROOT/sentinel-cancel-training.sh" <<'SENTINEL_CANCEL_EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={sh_single(remote_root)}
TRAINING_RUN_ID="$1"
REASON_CODE="$2"
APPROVAL_RECEIPT_ID="$3"
for VALUE in "$TRAINING_RUN_ID" "$REASON_CODE" "$APPROVAL_RECEIPT_ID"; do
  printf '%s\n' "$VALUE" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._:-]{{0,191}}$'
done
if tmux has-session -t agora_gpu >/dev/null 2>&1; then exit 75; fi
mkdir -p "$ROOT/.agora/machine-sentinel"
printf '%s\n' "$TRAINING_RUN_ID" > "$ROOT/.agora/machine-sentinel/cancelled-training-run"
SENTINEL_CANCEL_EOF
chmod 700 "$ASSET_ROOT/sentinel-cancel-training.sh"
cat > "$ASSET_ROOT/sentinel-repair-heartbeat.sh" <<'SENTINEL_REPAIR_EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={sh_single(remote_root)}
TRAINING_RUN_ID="$1"
WATCHDOG_REVISION="$2"
for VALUE in "$TRAINING_RUN_ID" "$WATCHDOG_REVISION"; do
  printf '%s\n' "$VALUE" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._:-]{{0,191}}$'
done
grep -qxF "AGORA_TRAINING_RUN_ID=$TRAINING_RUN_ID" "$ROOT/agora.env"
tmux has-session -t agora_gpu >/dev/null 2>&1
test -x "$ROOT/start-agora-heartbeat.sh"
"$ROOT/start-agora-heartbeat.sh"
SENTINEL_REPAIR_EOF
chmod 700 "$ASSET_ROOT/sentinel-repair-heartbeat.sh"
cat > "$ASSET_ROOT/start-machine-sentinel.sh" <<'SENTINEL_RUN_EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={sh_single(remote_root)}
SENTINEL_DIR="$ROOT/machine-sentinel"
{python_selection_script}
exec "$PYTHON_BIN" -u "$SENTINEL_DIR/agora_machine_sentinel_agent.py" \
  --url {sh_single(effective_url)} \
  --export-mode {"remote" if export_enabled else "local"} \
  --identity-file "$SENTINEL_DIR/identity.json" \
  --credential-env-file "$SENTINEL_DIR/credential.env" \
  --state-file "$SENTINEL_DIR/state.json" \
  --root "$ROOT" \
  --setup-revision {sh_single(str(settings["setupRevision"]))} \
  --provisioning-origin {sh_single(provisioning_origin)} \
  --authority-epoch {int(settings["authorityEpoch"])} \
  --timeout {float(settings["timeoutSeconds"])} \
  >> "$ROOT/logs/machine-sentinel.log" 2>&1
SENTINEL_RUN_EOF
chmod 700 "$ASSET_ROOT/start-machine-sentinel.sh"
{session_block}
"""

def remote_machine_sentinel_install_script(machine: dict[str, Any], settings: dict[str, Any], *, remote_machine_sentinel_install_body) -> str:
    return "#!/usr/bin/env bash\nset -Eeuo pipefail\n" + remote_machine_sentinel_install_body(
        machine,
        settings,
        record_progress=False,
    )


MACHINE_SENTINEL_REPAIR_ARTIFACTS = (
    "machine-sentinel/agora_machine_sentinel_agent.py",
    "machine-sentinel/machine_sentinel",
    "machine-sentinel/identity.json",
    "start-machine-sentinel.sh",
    "sentinel-verify-setup.sh",
    "sentinel-start-training.sh",
    "sentinel-stop-training.sh",
    "sentinel-cancel-training.sh",
    "sentinel-repair-heartbeat.sh",
)


def _machine_sentinel_repair_shell_helpers(*, bootstrap_token: str, sh_single) -> str:
    artifacts = "\n  ".join(sh_single(path) for path in MACHINE_SENTINEL_REPAIR_ARTIFACTS)
    return f"""REPAIR_ARTIFACTS=(
  {artifacts}
)
sentinel_sha256() {{
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{{print $1}}'
  else
    shasum -a 256 "$1" | awk '{{print $1}}'
  fi
}}
sentinel_preservation_proof() {{
  test -f "$ROOT/machine-sentinel/credential.env"
  test "$(sentinel_sha256 "$ROOT/machine-sentinel/credential.env")" = "$CREDENTIAL_SHA256"
  if [ "$STATE_PRESENT" = 1 ]; then
    test -f "$ROOT/machine-sentinel/state.json"
    test "$(sentinel_sha256 "$ROOT/machine-sentinel/state.json")" = "$STATE_SHA256"
  else
    test ! -e "$ROOT/machine-sentinel/state.json"
  fi
  if [ "$EVENTS_PRESENT" = 1 ]; then
    test "$(sentinel_sha256 "$ROOT/machine-sentinel/events.jsonl")" = "$EVENTS_SHA256"
  else
    test ! -e "$ROOT/machine-sentinel/events.jsonl"
  fi
  if [ "$EVENT_CURSOR_PRESENT" = 1 ]; then
    test "$(sentinel_sha256 "$ROOT/machine-sentinel/events.cursor.json")" = "$EVENT_CURSOR_SHA256"
  else
    test ! -e "$ROOT/machine-sentinel/events.cursor.json"
  fi
}}
sentinel_session_alive() {{
  tmux has-session -t agora_sentinel >/dev/null 2>&1 || return 1
  sentinel_pane_pid="$(tmux display-message -p -t agora_sentinel '#{{pane_pid}}' 2>/dev/null || true)"
  [ -n "$sentinel_pane_pid" ] && ps -p "$sentinel_pane_pid" >/dev/null 2>&1
}}
start_sentinel_with_ephemeral_bootstrap() {{
  SENTINEL_BOOTSTRAP_TOKEN={sh_single(bootstrap_token)}
  [ -n "$SENTINEL_BOOTSTRAP_TOKEN" ]
  tmux new-session -d -s agora_sentinel \\
    -e "AGORA_SENTINEL_BOOTSTRAP_TOKEN=$SENTINEL_BOOTSTRAP_TOKEN" \\
    "$ROOT/start-machine-sentinel.sh"
  tmux set-environment -t agora_sentinel -u AGORA_SENTINEL_BOOTSTRAP_TOKEN >/dev/null 2>&1 || true
  unset SENTINEL_BOOTSTRAP_TOKEN
}}
wait_for_sentinel_session() {{
  for _sentinel_attempt in 1 2 3 4 5; do
    if sentinel_session_alive; then return 0; fi
    sleep 1
  done
  return 1
}}
restore_sentinel_sources() {{
  tmux kill-session -t agora_sentinel >/dev/null 2>&1 || true
  for relative in "${{REPAIR_ARTIFACTS[@]}}"; do
    target="$ROOT/$relative"
    backup="$TXN/backup/$relative"
    marker="$TXN/absent/$relative"
    if [ -e "$backup" ] || [ -L "$backup" ]; then
      rm -rf -- "$target"
      mkdir -p "$(dirname "$target")"
      mv -- "$backup" "$target"
    elif [ -e "$marker" ]; then
      rm -rf -- "$target"
    fi
  done
  test -f "$ROOT/machine-sentinel/credential.env"
  if [ "$STATE_PRESENT" = 1 ]; then test -f "$ROOT/machine-sentinel/state.json"; fi
  start_sentinel_with_ephemeral_bootstrap
  wait_for_sentinel_session
}}
"""


def remote_machine_sentinel_repair_script(
    machine: dict[str, Any],
    settings: dict[str, Any],
    *,
    DEFAULT_REMOTE_ROOT,
    FleetError,
    remote_machine_sentinel_install_body,
    sh_single,
) -> str:
    remote_root = validated_machine_sentinel_remote_root(
        machine,
        DEFAULT_REMOTE_ROOT=DEFAULT_REMOTE_ROOT,
        FleetError=FleetError,
    )
    transaction_root = f"{remote_root}/.machine-sentinel-repair"
    stage_body = remote_machine_sentinel_install_body(
        machine,
        settings,
        record_progress=False,
        asset_remote_root=f"{transaction_root}/new",
        write_credentials=False,
        manage_session=False,
    )
    helpers = _machine_sentinel_repair_shell_helpers(
        bootstrap_token=str(settings.get("bootstrapToken") or ""),
        sh_single=sh_single,
    )
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={sh_single(remote_root)}
TXN="$ROOT/.machine-sentinel-repair"
if [ -d "$TXN" ] && [ ! -f "$TXN/preservation.env" ]; then
  rm -rf -- "$TXN"
fi
test ! -e "$TXN"
test -f "$ROOT/machine-sentinel/credential.env"
test -f "$ROOT/machine-sentinel/state.json"
test -x "$ROOT/start-machine-sentinel.sh"
{helpers}
for relative in "${{REPAIR_ARTIFACTS[@]}}"; do
  test -e "$ROOT/$relative" || test -L "$ROOT/$relative"
done
mkdir -m 700 "$TXN"
mkdir -p "$TXN/new" "$TXN/backup" "$TXN/absent"
chmod 700 "$TXN/new" "$TXN/backup" "$TXN/absent"
trap 'rm -rf -- "$TXN"' ERR
{stage_body}
trap - ERR
CREDENTIAL_SHA256="$(sentinel_sha256 "$ROOT/machine-sentinel/credential.env")"
if [ -f "$ROOT/machine-sentinel/state.json" ]; then
  STATE_PRESENT=1
  STATE_SHA256="$(sentinel_sha256 "$ROOT/machine-sentinel/state.json")"
elif [ -e "$ROOT/machine-sentinel/state.json" ]; then
  exit 66
else
  STATE_PRESENT=0
  STATE_SHA256=""
fi
if [ -f "$ROOT/machine-sentinel/events.jsonl" ]; then EVENTS_PRESENT=1; EVENTS_SHA256="$(sentinel_sha256 "$ROOT/machine-sentinel/events.jsonl")"; else EVENTS_PRESENT=0; EVENTS_SHA256=""; fi
if [ -f "$ROOT/machine-sentinel/events.cursor.json" ]; then EVENT_CURSOR_PRESENT=1; EVENT_CURSOR_SHA256="$(sentinel_sha256 "$ROOT/machine-sentinel/events.cursor.json")"; else EVENT_CURSOR_PRESENT=0; EVENT_CURSOR_SHA256=""; fi
printf 'CREDENTIAL_SHA256=%s\nSTATE_PRESENT=%s\nSTATE_SHA256=%s\nEVENTS_PRESENT=%s\nEVENTS_SHA256=%s\nEVENT_CURSOR_PRESENT=%s\nEVENT_CURSOR_SHA256=%s\n' \\
  "$CREDENTIAL_SHA256" "$STATE_PRESENT" "$STATE_SHA256" "$EVENTS_PRESENT" "$EVENTS_SHA256" "$EVENT_CURSOR_PRESENT" "$EVENT_CURSOR_SHA256" > "$TXN/preservation.env"
chmod 600 "$TXN/preservation.env"
rollback_repair() {{
  original_status=$?
  trap - ERR
  if ! restore_sentinel_sources; then
    printf '__AGORA_SENTINEL_REPAIR_ROLLBACK__=failed\n' >&2
    exit 86
  fi
  rm -rf -- "$TXN"
  printf '__AGORA_SENTINEL_REPAIR_ROLLBACK__=restored\n' >&2
  exit "$original_status"
}}
trap rollback_repair ERR
tmux kill-session -t agora_sentinel >/dev/null 2>&1 || true
CREDENTIAL_SHA256="$(sentinel_sha256 "$ROOT/machine-sentinel/credential.env")"
STATE_SHA256="$(sentinel_sha256 "$ROOT/machine-sentinel/state.json")"
if [ -f "$ROOT/machine-sentinel/events.jsonl" ]; then EVENTS_PRESENT=1; EVENTS_SHA256="$(sentinel_sha256 "$ROOT/machine-sentinel/events.jsonl")"; else EVENTS_PRESENT=0; EVENTS_SHA256=""; fi
if [ -f "$ROOT/machine-sentinel/events.cursor.json" ]; then EVENT_CURSOR_PRESENT=1; EVENT_CURSOR_SHA256="$(sentinel_sha256 "$ROOT/machine-sentinel/events.cursor.json")"; else EVENT_CURSOR_PRESENT=0; EVENT_CURSOR_SHA256=""; fi
printf 'CREDENTIAL_SHA256=%s\nSTATE_PRESENT=%s\nSTATE_SHA256=%s\nEVENTS_PRESENT=%s\nEVENTS_SHA256=%s\nEVENT_CURSOR_PRESENT=%s\nEVENT_CURSOR_SHA256=%s\n' \\
  "$CREDENTIAL_SHA256" "$STATE_PRESENT" "$STATE_SHA256" "$EVENTS_PRESENT" "$EVENTS_SHA256" "$EVENT_CURSOR_PRESENT" "$EVENT_CURSOR_SHA256" > "$TXN/preservation.env"
for relative in "${{REPAIR_ARTIFACTS[@]}}"; do
  staged="$TXN/new/$relative"
  target="$ROOT/$relative"
  test -e "$staged" || test -L "$staged"
  mkdir -p "$TXN/backup/$(dirname "$relative")" "$TXN/absent/$(dirname "$relative")"
  if [ -e "$target" ] || [ -L "$target" ]; then
    mv -- "$target" "$TXN/backup/$relative"
  else
    : > "$TXN/absent/$relative"
  fi
  mkdir -p "$(dirname "$target")"
  mv -- "$staged" "$target"
done
sentinel_preservation_proof
start_sentinel_with_ephemeral_bootstrap
wait_for_sentinel_session
test -f "$ROOT/machine-sentinel/credential.env"
if [ "$STATE_PRESENT" = 1 ]; then test -f "$ROOT/machine-sentinel/state.json"; fi
trap - ERR
printf '__AGORA_SENTINEL_REPAIR_PREPARED__=yes\n'
"""


def remote_machine_sentinel_repair_rollback_script(
    machine: dict[str, Any],
    bootstrap_token: str,
    *,
    DEFAULT_REMOTE_ROOT,
    FleetError,
    sh_single,
) -> str:
    remote_root = validated_machine_sentinel_remote_root(
        machine,
        DEFAULT_REMOTE_ROOT=DEFAULT_REMOTE_ROOT,
        FleetError=FleetError,
    )
    helpers = _machine_sentinel_repair_shell_helpers(
        bootstrap_token=bootstrap_token,
        sh_single=sh_single,
    )
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={sh_single(remote_root)}
TXN="$ROOT/.machine-sentinel-repair"
if [ ! -d "$TXN" ]; then
  printf '__AGORA_SENTINEL_REPAIR_ROLLBACK__=not_needed\n'
  exit 0
fi
if [ ! -f "$TXN/preservation.env" ]; then
  rm -rf -- "$TXN"
  printf '__AGORA_SENTINEL_REPAIR_ROLLBACK__=cleared_pre_swap\n'
  exit 0
fi
. "$TXN/preservation.env"
{helpers}
restore_sentinel_sources
rm -rf -- "$TXN"
printf '__AGORA_SENTINEL_REPAIR_ROLLBACK__=restored\n'
"""


def remote_machine_sentinel_repair_finalize_script(
    machine: dict[str, Any],
    *,
    DEFAULT_REMOTE_ROOT,
    FleetError,
    sh_single,
) -> str:
    remote_root = validated_machine_sentinel_remote_root(
        machine,
        DEFAULT_REMOTE_ROOT=DEFAULT_REMOTE_ROOT,
        FleetError=FleetError,
    )
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={sh_single(remote_root)}
TXN="$ROOT/.machine-sentinel-repair"
test -d "$TXN"
. "$TXN/preservation.env"
test -f "$ROOT/machine-sentinel/credential.env"
if [ "$STATE_PRESENT" = 1 ]; then
  test -f "$ROOT/machine-sentinel/state.json"
else
  test ! -e "$ROOT/machine-sentinel/state.json"
fi
rm -rf -- "$TXN"
printf '__AGORA_SENTINEL_REPAIR_FINALIZED__=yes\n'
"""

def remote_machine_sentinel_uninstall_script(machine: dict[str, Any], *, DEFAULT_REMOTE_ROOT, FleetError, sh_single) -> str:
    remote_root = validated_machine_sentinel_remote_root(
        machine,
        DEFAULT_REMOTE_ROOT=DEFAULT_REMOTE_ROOT,
        FleetError=FleetError,
    )
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={sh_single(remote_root)}
tmux kill-session -t agora_sentinel >/dev/null 2>&1 || true
rm -rf -- "$ROOT/machine-sentinel"
rm -f -- \
  "$ROOT/start-machine-sentinel.sh" \
  "$ROOT/sentinel-verify-setup.sh" \
  "$ROOT/sentinel-start-training.sh" \
  "$ROOT/sentinel-stop-training.sh" \
  "$ROOT/sentinel-cancel-training.sh" \
  "$ROOT/sentinel-repair-heartbeat.sh" \
  "$ROOT/logs/machine-sentinel.log"
"""

def remote_machine_sentinel_preflight_script(machine: dict[str, Any], *, DEFAULT_REMOTE_ROOT, FleetError, sh_single) -> str:
    remote_root = validated_machine_sentinel_remote_root(
        machine,
        DEFAULT_REMOTE_ROOT=DEFAULT_REMOTE_ROOT,
        FleetError=FleetError,
    )
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={sh_single(remote_root)}
occupied=0
if tmux has-session -t agora_sentinel >/dev/null 2>&1; then
  printf '__AGORA_SENTINEL_PREEXISTING__=session\n'
  occupied=1
fi
for path in \
  "$ROOT/machine-sentinel" \
  "$ROOT/start-machine-sentinel.sh" \
  "$ROOT/sentinel-verify-setup.sh" \
  "$ROOT/sentinel-start-training.sh" \
  "$ROOT/sentinel-stop-training.sh" \
  "$ROOT/sentinel-cancel-training.sh" \
  "$ROOT/sentinel-repair-heartbeat.sh" \
  "$ROOT/logs/machine-sentinel.log"
do
  if [ -e "$path" ]; then
    printf '__AGORA_SENTINEL_PREEXISTING__=%s\n' "$path"
    occupied=1
  fi
done
if [ "$occupied" -ne 0 ]; then
  exit 3
fi
printf '__AGORA_SENTINEL_ABSENT__=yes\n'
"""

def remote_machine_sentinel_status_script() -> str:
    return """#!/usr/bin/env bash
set -Eeuo pipefail
if ! tmux has-session -t agora_sentinel >/dev/null 2>&1; then
  printf '__AGORA_SENTINEL_ALIVE__=no\n'
  exit 1
fi
pane_pid="$(tmux display-message -p -t agora_sentinel '#{pane_pid}' 2>/dev/null || true)"
if [ -z "$pane_pid" ] || ! ps -p "$pane_pid" >/dev/null 2>&1; then
  printf '__AGORA_SENTINEL_ALIVE__=no\n'
  exit 1
fi
printf '__AGORA_SENTINEL_ALIVE__=yes\n'
"""

def remote_machine_sentinel_log_tail_script(machine: dict[str, Any], *, DEFAULT_REMOTE_ROOT, FleetError, sh_single) -> str:
    remote_root = validated_machine_sentinel_remote_root(
        machine,
        DEFAULT_REMOTE_ROOT=DEFAULT_REMOTE_ROOT,
        FleetError=FleetError,
    )
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={sh_single(remote_root)}
LOG="$ROOT/logs/machine-sentinel.log"
if [ ! -f "$LOG" ]; then
  printf '__AGORA_SENTINEL_LOG_UNAVAILABLE__=missing\n'
  exit 0
fi
tail -c 4000 -- "$LOG" 2>/dev/null || true
"""

def _validate_machine_sentinel_settings(
    settings: dict[str, Any],
    *,
    FleetError,
    require_credential: bool = True,
) -> None:
    export_enabled = bool(settings.get("exportEnabled", True))
    required_text = ("url", "fleetId") if export_enabled else ("fleetId",)
    for key in required_text:
        if not isinstance(settings.get(key), str) or not str(settings[key]).strip():
            raise FleetError(f"Machine Sentinel settings are missing {key}")
    if export_enabled and not str(settings["url"]).startswith("https://"):
        raise FleetError("Machine Sentinel url must use HTTPS")
    if not isinstance(settings.get("setupRevision"), str) or not str(settings["setupRevision"]).strip():
        raise FleetError("Machine Sentinel settings are missing setupRevision")
    bootstrap_token = settings.get("bootstrapToken")
    machine_token = settings.get("machineToken")
    if bootstrap_token is not None and not isinstance(bootstrap_token, str):
        raise FleetError("Machine Sentinel bootstrap credential is invalid")
    if machine_token is not None and not isinstance(machine_token, str):
        raise FleetError("Machine Sentinel machine credential is invalid")
    if (
        require_credential
        and export_enabled
        and not str(bootstrap_token or "").strip()
        and not str(machine_token or "").strip()
    ):
        raise FleetError("Machine Sentinel requires a bootstrap or current machine credential")
    for key in ("authorityEpoch",):
        value = settings.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise FleetError(f"Machine Sentinel settings are missing {key}")
    timeout = settings.get("timeoutSeconds")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise FleetError("Machine Sentinel settings are missing timeoutSeconds")

def _machine_sentinel_operation(
    args: argparse.Namespace,
    *,
    action: str,
    add_runtime_secret_for_redaction,
    FleetError,
    capture_agora_tmux_identity,
    compare_agora_tmux_identity,
    get_machine,
    load_machine_sentinel_settings,
    machine_sentinel_identity,
    redact,
    remote_machine_sentinel_install_script,
    remote_machine_sentinel_log_tail_script,
    remote_machine_sentinel_preflight_script,
    remote_machine_sentinel_repair_finalize_script=None,
    remote_machine_sentinel_repair_rollback_script=None,
    remote_machine_sentinel_repair_script=None,
    remote_machine_sentinel_status_script,
    remote_machine_sentinel_uninstall_script,
    run_ssh,
) -> dict[str, Any]:
    execute = bool(getattr(args, "execute", False))
    confirmed = bool(getattr(args, "yes", False))
    if execute != confirmed:
        raise FleetError("Machine Sentinel mutation requires --execute and --yes together")
    _, machine = get_machine(args.machine_id)
    if action in {"install", "repair"}:
        ephemeral_bootstrap = getattr(args, "_machine_sentinel_bootstrap_token", None)
        settings = load_machine_sentinel_settings(
            allow_missing_credentials=(
                action == "repair" or ephemeral_bootstrap is not None
            ),
        )
        if not settings:
            raise FleetError("Machine Sentinel settings are missing or disabled")
        settings = dict(settings)
        authority_epoch = getattr(args, "_machine_sentinel_authority_epoch", None)
        if authority_epoch is not None:
            settings["authorityEpoch"] = authority_epoch
        if ephemeral_bootstrap is not None:
            if not isinstance(ephemeral_bootstrap, str) or not ephemeral_bootstrap:
                raise FleetError("Machine Sentinel bootstrap grant is invalid")
            settings = {**settings, "bootstrapToken": ephemeral_bootstrap}
            if action == "install":
                settings["machineToken"] = ""
            add_runtime_secret_for_redaction(ephemeral_bootstrap)
        if action == "repair" and execute and ephemeral_bootstrap is None:
            raise FleetError("Machine Sentinel repair requires a fresh cloud bootstrap grant")
        _validate_machine_sentinel_settings(
            settings,
            FleetError=FleetError,
            require_credential=action == "install" or execute,
        )
        # Render before any SSH so every identity field and runtime setting is
        # validated even for a dry-run preview.
        machine_sentinel_identity(machine, settings)
        if action == "install":
            script = remote_machine_sentinel_install_script(machine, settings)
            planned_artifacts = [
                "machine-sentinel/",
                "start-machine-sentinel.sh",
                "sentinel-verify-setup.sh",
                "sentinel-start-training.sh",
                "sentinel-stop-training.sh",
                "sentinel-cancel-training.sh",
                "sentinel-repair-heartbeat.sh",
                "logs/machine-sentinel.log",
            ]
        else:
            if not all(
                callable(value)
                for value in (
                    remote_machine_sentinel_repair_script,
                    remote_machine_sentinel_repair_rollback_script,
                    remote_machine_sentinel_repair_finalize_script,
                )
            ):
                raise FleetError("Machine Sentinel repair collaborators are unavailable")
            assert callable(remote_machine_sentinel_repair_script)
            assert callable(remote_machine_sentinel_repair_rollback_script)
            assert callable(remote_machine_sentinel_repair_finalize_script)
            script = remote_machine_sentinel_repair_script(machine, settings)
            planned_artifacts = list(MACHINE_SENTINEL_REPAIR_ARTIFACTS)
    elif action == "uninstall":
        script = remote_machine_sentinel_uninstall_script(machine)
        planned_artifacts = [
            "machine-sentinel/",
            "start-machine-sentinel.sh",
            "sentinel-verify-setup.sh",
            "sentinel-start-training.sh",
            "sentinel-stop-training.sh",
            "sentinel-cancel-training.sh",
            "sentinel-repair-heartbeat.sh",
            "logs/machine-sentinel.log",
        ]
    else:
        raise FleetError(f"Unsupported Machine Sentinel operation: {action}")

    if not execute:
        preview = {
            "ok": True,
            "machineId": machine["id"],
            "operation": action,
            "sentinelSession": "agora_sentinel",
            "artifacts": planned_artifacts,
            "dryRun": True,
        }
        if action == "repair":
            preview["preservedArtifacts"] = [
                "machine-sentinel/credential.env",
                "machine-sentinel/state.json",
            ]
        return preview

    if action == "install":
        preflight = run_ssh(
            machine,
            remote_machine_sentinel_preflight_script(machine),
            timeout=args.timeout,
            dry_run=False,
        )
        if preflight.returncode != 0 or "__AGORA_SENTINEL_ABSENT__=yes" not in preflight.stdout:
            raise FleetError(
                "Machine Sentinel already has owned artifacts or its absence could not be proved; "
                f"refusing install: {redact((preflight.stdout + preflight.stderr)[-1000:])}"
            )

    def cleanup_failed_install(original_error: Exception) -> None:
        if action != "install":
            return
        cleanup_issues = []
        try:
            cleanup_result = run_ssh(
                machine,
                remote_machine_sentinel_uninstall_script(machine),
                timeout=args.timeout,
                dry_run=False,
            )
            if cleanup_result.returncode != 0:
                cleanup_output = redact(
                    (cleanup_result.stdout[-1000:] + cleanup_result.stderr[-1000:])[-1000:]
                )
                cleanup_issues.append(
                    f"Sentinel-only cleanup exited {cleanup_result.returncode}: {cleanup_output}"
                )
        except Exception as cleanup_error:
            cleanup_issues.append(
                "Sentinel-only cleanup was unavailable: "
                f"{redact(str(cleanup_error)[-1000:])}"
            )
        try:
            post_cleanup = capture_agora_tmux_identity(machine, timeout=args.timeout)
            post_cleanup_preservation = compare_agora_tmux_identity(before, post_cleanup)
            if (
                not post_cleanup_preservation["sessionPreserved"]
                or not post_cleanup_preservation["panePidAlive"]
            ):
                cleanup_issues.append(
                    "post-cleanup agora_gpu identity changed: "
                    f"{post_cleanup_preservation['changedFields']}"
                )
        except Exception as identity_error:
            cleanup_issues.append(
                "post-cleanup agora_gpu identity proof was unavailable: "
                f"{redact(str(identity_error)[-1000:])}"
            )
        if cleanup_issues:
            original_detail = redact(str(original_error)[-1000:])
            raise FleetError(
                f"Original Machine Sentinel install failure: {original_detail}; "
                "failed-install cleanup could not be proved safe: "
                + "; ".join(cleanup_issues)
            ) from original_error

    def rollback_failed_repair(original_error: Exception) -> None:
        if action != "repair":
            return
        assert callable(remote_machine_sentinel_repair_rollback_script)
        rollback_issues = []
        try:
            rollback = run_ssh(
                machine,
                remote_machine_sentinel_repair_rollback_script(
                    machine, str(settings.get("bootstrapToken") or "")
                ),
                timeout=args.timeout,
                dry_run=False,
            )
            if rollback.returncode != 0:
                rollback_issues.append(
                    "source rollback failed: "
                    + redact((rollback.stdout + rollback.stderr)[-1000:])
                )
        except Exception as rollback_error:
            rollback_issues.append(
                "source rollback was unavailable: "
                + redact(str(rollback_error)[-1000:])
            )
        try:
            restored = capture_agora_tmux_identity(machine, timeout=args.timeout)
            restored_proof = compare_agora_tmux_identity(before, restored)
            if not restored_proof["sessionPreserved"] or not restored_proof["panePidAlive"]:
                rollback_issues.append(
                    "post-rollback agora_gpu identity changed: "
                    f"{restored_proof['changedFields']}"
                )
        except Exception as identity_error:
            rollback_issues.append(
                "post-rollback agora_gpu identity proof was unavailable: "
                + redact(str(identity_error)[-1000:])
            )
        try:
            old_status = run_ssh(
                machine,
                remote_machine_sentinel_status_script(),
                timeout=args.timeout,
                dry_run=False,
            )
            if old_status.returncode != 0 or "__AGORA_SENTINEL_ALIVE__=yes" not in old_status.stdout:
                rollback_issues.append("old agora_sentinel session was not restored")
        except Exception as status_error:
            rollback_issues.append(
                "old agora_sentinel status proof was unavailable: "
                + redact(str(status_error)[-1000:])
            )
        if rollback_issues:
            raise FleetError(
                "Original Machine Sentinel repair failure: "
                f"{redact(str(original_error)[-1000:])}; repair rollback could not be proved: "
                + "; ".join(rollback_issues)
            ) from original_error

    before = capture_agora_tmux_identity(machine, timeout=args.timeout)
    try:
        result = run_ssh(machine, script, timeout=args.timeout, dry_run=False)
    except Exception as error:
        cleanup_failed_install(error)
        rollback_failed_repair(error)
        raise
    try:
        after = capture_agora_tmux_identity(machine, timeout=args.timeout)
    except Exception as error:
        cleanup_failed_install(error)
        rollback_failed_repair(error)
        raise
    try:
        preservation = compare_agora_tmux_identity(before, after)
        training_preserved = (
            preservation["sessionPreserved"] and preservation["panePidAlive"]
        )
    except Exception as error:
        cleanup_failed_install(error)
        rollback_failed_repair(error)
        raise
    if not training_preserved:
        error = FleetError(
            "agora_gpu tmux identity changed during Machine Sentinel "
            f"{action}: {preservation['changedFields']}"
        )
        cleanup_failed_install(error)
        rollback_failed_repair(error)
        raise error
    if result.returncode != 0:
        error = FleetError(
            f"Machine Sentinel {action} failed with exit {result.returncode}: "
            f"{redact(result.stderr[-1000:])}"
        )
        cleanup_failed_install(error)
        rollback_failed_repair(error)
        raise error
    sentinel_alive = None
    if action in {"install", "repair"}:
        try:
            sentinel_status = run_ssh(
                machine,
                remote_machine_sentinel_status_script(),
                timeout=args.timeout,
                dry_run=False,
            )
        except Exception as error:
            cleanup_failed_install(error)
            rollback_failed_repair(error)
            raise
        sentinel_alive = (
            sentinel_status.returncode == 0
            and "__AGORA_SENTINEL_ALIVE__=yes" in sentinel_status.stdout
        )
        if not sentinel_alive:
            try:
                log_tail = run_ssh(
                    machine,
                    remote_machine_sentinel_log_tail_script(machine),
                    timeout=args.timeout,
                    dry_run=False,
                )
                diagnostic = redact(
                    (log_tail.stdout[-4000:] + log_tail.stderr[-1000:])[-4000:]
                )
            except Exception as error:
                diagnostic = redact(str(error)[-1000:])
            error = FleetError(
                "Machine Sentinel install did not leave agora_sentinel alive: "
                f"{redact(sentinel_status.stderr[-1000:])}; "
                f"sentinelLogTail={diagnostic}"
            )
            cleanup_failed_install(error)
            rollback_failed_repair(error)
            raise error
    if action == "repair":
        assert callable(remote_machine_sentinel_repair_finalize_script)
        try:
            finalized = run_ssh(
                machine,
                remote_machine_sentinel_repair_finalize_script(machine),
                timeout=args.timeout,
                dry_run=False,
            )
        except Exception as error:
            rollback_failed_repair(error)
            raise
        if finalized.returncode != 0:
            error = FleetError(
                "Machine Sentinel repair preservation finalization failed: "
                f"{redact((finalized.stdout + finalized.stderr)[-1000:])}"
            )
            rollback_failed_repair(error)
            raise error
    return {
        "ok": True,
        "machineId": machine["id"],
        "operation": action,
        "sentinelSession": "agora_sentinel",
        "artifacts": planned_artifacts,
        "returncode": result.returncode,
        "stdout": redact(result.stdout[-4000:]),
        "stderr": redact(result.stderr[-4000:]),
        "agoraGpu": preservation,
        "sentinelAlive": sentinel_alive,
        "dryRun": False,
    }

def install_machine_sentinel(args: argparse.Namespace, **dependencies) -> dict[str, Any]:
    return _machine_sentinel_operation(args, action="install", **dependencies)

def uninstall_machine_sentinel(args: argparse.Namespace, **dependencies) -> dict[str, Any]:
    return _machine_sentinel_operation(args, action="uninstall", **dependencies)

def repair_machine_sentinel(args: argparse.Namespace, **dependencies) -> dict[str, Any]:
    return _machine_sentinel_operation(args, action="repair", **dependencies)

def remote_heartbeat_install_body(machine: dict[str, Any], heartbeat: dict[str, Any] | None, *, DEFAULT_REMOTE_ROOT, FleetError, HEARTBEAT_AGENT_FILE, add_runtime_secret_for_redaction, credible_machine_join_role, derive_heartbeat_machine_secret, normalize_agora_role, sh_single) -> str:
    remote_root = machine.get("remoteRoot") or DEFAULT_REMOTE_ROOT
    if not heartbeat:
        return f"""printf '%s event=heartbeat_install_skipped reason=not_configured\\n' "$(date -Iseconds)" >> {sh_single(remote_root)}/progress.log
"""
    if not HEARTBEAT_AGENT_FILE.exists():
        raise FleetError(f"Heartbeat agent source is missing: {HEARTBEAT_AGENT_FILE}")
    agent_source = HEARTBEAT_AGENT_FILE.read_text(encoding="utf-8")
    machine_id = str(machine.get("id") or machine.get("label") or "")
    machine_secret = derive_heartbeat_machine_secret(str(heartbeat.get("masterSecret") or ""), machine_id)
    add_runtime_secret_for_redaction(machine_secret)
    role = normalize_agora_role(credible_machine_join_role(machine) or machine.get("provisioningRole"))
    token_label = str(machine.get("tokenLabel") or "")
    runpod_pod_id = str(machine.get("runpodId") or "")
    runpod_dc_id = str(machine.get("runpodDataCenterId") or "")
    interval = str(float(heartbeat.get("intervalSeconds", 10)))
    jitter = str(float(heartbeat.get("jitterSeconds", 3)))
    timeout = str(float(heartbeat.get("timeoutSeconds", 5)))
    return f"""ROOT={sh_single(remote_root)}
HEARTBEAT_DIR="$ROOT/heartbeat-agent"
mkdir -p "$HEARTBEAT_DIR" "$ROOT/logs"
printf '%s event=heartbeat_install_start machine=%s role=%s runpod_dc=%s\\n' "$(date -Iseconds)" {sh_single(machine_id)} {sh_single(role)} {sh_single(runpod_dc_id or "unknown")} >> "$ROOT/progress.log"
cat > "$HEARTBEAT_DIR/agora_heartbeat_agent.py" <<'PYEOF'
{agent_source}
PYEOF
chmod 700 "$HEARTBEAT_DIR/agora_heartbeat_agent.py"
cat > "$HEARTBEAT_DIR/heartbeat.env" <<ENVEOF
AGORA_HEARTBEAT_URL={sh_single(str(heartbeat["url"]))}
AGORA_HEARTBEAT_SECRET={sh_single(machine_secret)}
ENVEOF
chmod 600 "$HEARTBEAT_DIR/heartbeat.env"
cat > "$ROOT/start-agora-heartbeat.sh" <<'STARTEOF'
#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={sh_single(remote_root)}
HEARTBEAT_DIR="$ROOT/heartbeat-agent"
set -a
. "$HEARTBEAT_DIR/heartbeat.env"
set +a
if [ -x /opt/agora-venv/bin/python3 ]; then
  PYTHON_BIN=/opt/agora-venv/bin/python3
elif command -v python3.13 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3.13)"
else
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [ -z "$PYTHON_BIN" ]; then
  echo "Python 3 is required for heartbeat agent" >&2
  exit 65
fi
printf '%s event=heartbeat_agent_start\\n' "$(date -Iseconds)" >> "$ROOT/logs/heartbeat.log"
exec "$PYTHON_BIN" -u "$HEARTBEAT_DIR/agora_heartbeat_agent.py" \\
  --url "$AGORA_HEARTBEAT_URL" \\
  --secret-env-file "$HEARTBEAT_DIR/heartbeat.env" \\
  --machine-id {sh_single(machine_id)} \\
  --role {sh_single(role)} \\
  --token-label {sh_single(token_label)} \\
  --runpod-pod-id {sh_single(runpod_pod_id)} \\
  --runpod-dc-id {sh_single(runpod_dc_id)} \\
  --interval {sh_single(interval)} \\
  --jitter {sh_single(jitter)} \\
  --timeout {sh_single(timeout)} \\
  --state-file "$HEARTBEAT_DIR/state.json" \\
  >> "$ROOT/logs/heartbeat.log" 2>&1
STARTEOF
chmod 700 "$ROOT/start-agora-heartbeat.sh"
cat > "$ROOT/watchdog-heartbeat-tmux.sh" <<'CHECKEOF'
#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={sh_single(remote_root)}
LOCKDIR="$ROOT/heartbeat-watchdog.lock"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  exit 0
fi
trap 'rmdir "$LOCKDIR"' EXIT
if ! tmux has-session -t agora_heartbeat >/dev/null 2>&1; then
  printf '%s event=heartbeat_watchdog_restart reason=missing_tmux_session\\n' "$(date -Iseconds)" >> "$ROOT/progress.log"
  tmux new-session -d -s agora_heartbeat "$ROOT/start-agora-heartbeat.sh"
fi
CHECKEOF
chmod 700 "$ROOT/watchdog-heartbeat-tmux.sh"
cat > "$ROOT/watch-heartbeat-tmux-loop.sh" <<'LOOPEOF'
#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={sh_single(remote_root)}
WATCHDOG_BASE_SLEEP_SECONDS=3
WATCHDOG_JITTER_SECONDS=1
jittered_watchdog_sleep_seconds() {{
  awk -v base="$WATCHDOG_BASE_SLEEP_SECONDS" -v jitter="$WATCHDOG_JITTER_SECONDS" 'BEGIN {{ srand(); printf "%.3f", base + (jitter * rand()) }}'
}}
while true; do
  "$ROOT/watchdog-heartbeat-tmux.sh" >> "$ROOT/logs/heartbeat-watchdog-check.log" 2>&1 || true
  sleep "$(jittered_watchdog_sleep_seconds)"
done
LOOPEOF
chmod 700 "$ROOT/watch-heartbeat-tmux-loop.sh"
for pid in $(pgrep -f "$ROOT/watch-heartbeat-tmux-loop.sh" 2>/dev/null || true); do
  if [ "$pid" != "$$" ]; then
    kill "$pid" 2>/dev/null || true
  fi
done
nohup "$ROOT/watch-heartbeat-tmux-loop.sh" >> "$ROOT/heartbeat-watchdog.log" 2>&1 < /dev/null &
if ! command -v crontab >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
  apt-get update >/dev/null 2>&1 || true
  DEBIAN_FRONTEND=noninteractive apt-get install -y cron >/dev/null 2>&1 || true
fi
if command -v service >/dev/null 2>&1; then
  service cron start >/dev/null 2>&1 || true
elif command -v cron >/dev/null 2>&1; then
  pgrep -x cron >/dev/null 2>&1 || nohup cron >/dev/null 2>&1 &
fi
if command -v crontab >/dev/null 2>&1; then
  tmp_cron="$(mktemp)"
  crontab -l 2>/dev/null | grep -v 'watch-heartbeat-tmux-loop.sh' > "$tmp_cron" || true
  printf '@reboot pgrep -f "%s/watch-heartbeat-tmux-loop.sh" >/dev/null 2>&1 || nohup "%s/watch-heartbeat-tmux-loop.sh" >> "%s/heartbeat-watchdog.log" 2>&1 < /dev/null &\\n' "$ROOT" "$ROOT" "$ROOT" >> "$tmp_cron"
  printf '* * * * * pgrep -f "%s/watch-heartbeat-tmux-loop.sh" >/dev/null 2>&1 || nohup "%s/watch-heartbeat-tmux-loop.sh" >> "%s/heartbeat-watchdog.log" 2>&1 < /dev/null &\\n' "$ROOT" "$ROOT" "$ROOT" >> "$tmp_cron"
  crontab "$tmp_cron"
  rm -f "$tmp_cron"
fi
tmux kill-session -t agora_heartbeat >/dev/null 2>&1 || true
"$ROOT/watchdog-heartbeat-tmux.sh"
printf '%s event=heartbeat_installed machine=%s role=%s runpod_dc=%s\\n' "$(date -Iseconds)" {sh_single(machine_id)} {sh_single(role)} {sh_single(runpod_dc_id or "unknown")} >> "$ROOT/progress.log"
"""

def remote_heartbeat_install_script(machine: dict[str, Any], heartbeat: dict[str, Any] | None, *, remote_heartbeat_install_body) -> str:
    return "#!/usr/bin/env bash\nset -Eeuo pipefail\n" + remote_heartbeat_install_body(machine, heartbeat)

def remote_heartbeat_uninstall_script(machine: dict[str, Any], *, DEFAULT_REMOTE_ROOT, sh_single) -> str:
    remote_root = machine.get("remoteRoot") or DEFAULT_REMOTE_ROOT
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={sh_single(remote_root)}
HEARTBEAT_DIR="$ROOT/heartbeat-agent"
mkdir -p "$ROOT/logs"
printf '%s event=heartbeat_uninstall_start\\n' "$(date -Iseconds)" >> "$ROOT/progress.log"
for pid in $(pgrep -f "$ROOT/watch-heartbeat-tmux-loop.sh" 2>/dev/null || true); do
  if [ "$pid" != "$$" ]; then
    kill "$pid" 2>/dev/null || true
  fi
done
if command -v crontab >/dev/null 2>&1; then
  tmp_cron="$(mktemp)"
  crontab -l 2>/dev/null | grep -v 'watch-heartbeat-tmux-loop.sh' > "$tmp_cron" || true
  crontab "$tmp_cron" 2>/dev/null || true
  rm -f "$tmp_cron"
fi
tmux kill-session -t agora_heartbeat >/dev/null 2>&1 || true
if [ -d "$HEARTBEAT_DIR" ]; then
  find "$HEARTBEAT_DIR" -mindepth 1 -maxdepth 1 ! -name 'state.json' -exec rm -rf {{}} +
fi
rm -f "$ROOT/start-agora-heartbeat.sh" "$ROOT/watchdog-heartbeat-tmux.sh" "$ROOT/watch-heartbeat-tmux-loop.sh"
printf '%s event=heartbeat_uninstalled\\n' "$(date -Iseconds)" >> "$ROOT/progress.log"
"""

def install_heartbeat(args: argparse.Namespace, *, FleetError, get_machine, load_heartbeat_agent_settings, redact, refresh_fleet_runpod_metadata, remote_heartbeat_install_script, run_ssh, update_machine, utcnow) -> dict[str, Any]:
    fleet, machine = get_machine(args.machine_id)
    if getattr(args, "refresh_runpod_metadata", True):
        refresh = refresh_fleet_runpod_metadata(timeout=getattr(args, "api_timeout", 60))
        fleet, machine = get_machine(args.machine_id)
    else:
        refresh = {"updatedCount": 0, "updates": []}
    heartbeat = load_heartbeat_agent_settings()
    if not heartbeat:
        raise FleetError("Heartbeat agent settings are missing; configure ownMachineRecovery.statusUrl and AGORA_HEARTBEAT_SECRET")
    script = remote_heartbeat_install_script(machine, heartbeat)
    result = run_ssh(machine, script, timeout=args.timeout, dry_run=args.dry_run)
    if not args.dry_run and result.returncode == 0:
        update_machine(fleet, args.machine_id, lastHeartbeatInstallAt=utcnow())
    return {
        "machineId": args.machine_id,
        "tokenLabel": machine.get("tokenLabel"),
        "runpodId": machine.get("runpodId"),
        "runpodDataCenterId": machine.get("runpodDataCenterId"),
        "metadataRefresh": refresh,
        "returncode": result.returncode,
        "stdout": redact(result.stdout[-4000:]),
        "stderr": redact(result.stderr[-4000:]),
        "dryRun": args.dry_run,
    }

def heartbeat_install_safety_test(args: argparse.Namespace, *, FleetError, capture_agora_tmux_identity, compare_agora_tmux_identity, get_machine, load_heartbeat_agent_settings, redact, remote_heartbeat_install_script, remote_heartbeat_uninstall_script, run_ssh, update_machine, utcnow) -> dict[str, Any]:
    fleet, machine = get_machine(args.machine_id)
    cycles = int(getattr(args, "cycles", 3) or 3)
    timeout = int(getattr(args, "timeout", 600) or 600)
    dry_run = bool(getattr(args, "dry_run", False))
    if cycles < 1:
        raise FleetError("--cycles must be at least 1")
    heartbeat = load_heartbeat_agent_settings()
    if not heartbeat:
        raise FleetError("Heartbeat agent settings are missing; configure ownMachineRecovery.statusUrl and AGORA_HEARTBEAT_SECRET")
    baseline = capture_agora_tmux_identity(machine, timeout=timeout, dry_run=dry_run)
    cycle_results: list[dict[str, Any]] = []
    for index in range(1, cycles + 1):
        uninstall = run_ssh(machine, remote_heartbeat_uninstall_script(machine), timeout=timeout, dry_run=dry_run)
        if uninstall.returncode != 0:
            raise FleetError(f"heartbeat uninstall failed in cycle {index}: {redact(uninstall.stderr[-1000:])}")
        after_uninstall_identity = capture_agora_tmux_identity(machine, timeout=timeout, dry_run=dry_run)
        after_uninstall = compare_agora_tmux_identity(baseline, after_uninstall_identity)
        if not after_uninstall["sessionPreserved"]:
            raise FleetError(
                "agora_gpu tmux identity changed after heartbeat uninstall "
                f"in cycle {index}: {after_uninstall['changedFields']}"
            )
        install = run_ssh(machine, remote_heartbeat_install_script(machine, heartbeat), timeout=timeout, dry_run=dry_run)
        if install.returncode != 0:
            raise FleetError(f"heartbeat install failed in cycle {index}: {redact(install.stderr[-1000:])}")
        after_install_identity = capture_agora_tmux_identity(machine, timeout=timeout, dry_run=dry_run)
        after_install = compare_agora_tmux_identity(baseline, after_install_identity)
        if not after_install["sessionPreserved"]:
            raise FleetError(
                "agora_gpu tmux identity changed after heartbeat install "
                f"in cycle {index}: {after_install['changedFields']}"
            )
        cycle_results.append(
            {
                "cycle": index,
                "uninstallReturncode": uninstall.returncode,
                "installReturncode": install.returncode,
                "afterUninstall": after_uninstall,
                "afterInstall": after_install,
            }
        )
    if not dry_run:
        update_machine(fleet, args.machine_id, lastHeartbeatInstallAt=utcnow())
    return {
        "ok": True,
        "machineId": args.machine_id,
        "tokenLabel": machine.get("tokenLabel"),
        "cyclesRequested": cycles,
        "baseline": baseline,
        "cycles": cycle_results,
        "dryRun": dry_run,
    }
