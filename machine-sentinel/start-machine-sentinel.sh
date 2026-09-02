#!/usr/bin/env bash
set -Eeuo pipefail

if [[ -z "${AGORA_SENTINEL_BOOTSTRAP_TOKEN:-}" ]]; then
    exit 0
fi

required=(
    AGORA_SENTINEL_URL
    AGORA_FLEET_ID
    AGORA_LAUNCH_ID
    AGORA_RESERVATION_ID
    AGORA_SLOT_ID
    AGORA_SLOT_GENERATION
    AGORA_MACHINE_GENERATION_ID
    AGORA_MACHINE_ID
    AGORA_TOKEN_LABEL
    AGORA_NODE_TYPE
    AGORA_GPU_MODEL
    AGORA_SETUP_REVISION
    AGORA_AUTHORITY_EPOCH
    RUNPOD_POD_ID
)
for name in "${required[@]}"; do
    if [[ -z "${!name:-}" ]]; then
        printf 'Machine Sentinel identity is missing %s\n' "$name" >&2
        exit 64
    fi
done

case "$AGORA_SENTINEL_URL" in
    https://*/api/machine-sentinel/observe) ;;
    *)
        printf 'AGORA_SENTINEL_URL must be the HTTPS public observation endpoint\n' >&2
        exit 64
        ;;
esac
case "$AGORA_SLOT_GENERATION:$AGORA_AUTHORITY_EPOCH" in
    *[!0-9:]*|:*|*:) exit 64 ;;
esac
if (( AGORA_SLOT_GENERATION < 1 || AGORA_AUTHORITY_EPOCH < 1 )); then
    exit 64
fi
case "$AGORA_NODE_TYPE" in
    head|body|tail) ;;
    *) exit 64 ;;
esac

workspace_root="${AGORA_WORKSPACE_ROOT:-/workspace}"
state_dir="${AGORA_SENTINEL_STATE_DIR:-$workspace_root/.agora/machine-sentinel}"
runtime_dir="${AGORA_SENTINEL_RUNTIME_DIR:-/opt/agora-machine-sentinel}"
python_bin="${AGORA_SENTINEL_PYTHON:-/opt/agora-venv/bin/python}"
credential_file="$state_dir/credential.env"
identity_file="$state_dir/identity.json"
state_file="$state_dir/state.json"

umask 077
mkdir -p "$state_dir" "$workspace_root/logs"
chmod 700 "$state_dir"

export AGORA_SENTINEL_IDENTITY_FILE="$identity_file"
"$python_bin" - <<'PY'
import json
import os
import tempfile
from pathlib import Path

path = Path(os.environ["AGORA_SENTINEL_IDENTITY_FILE"])
pod_id = os.environ["RUNPOD_POD_ID"]
identity = {
    "fleetId": os.environ["AGORA_FLEET_ID"],
    "launchId": os.environ["AGORA_LAUNCH_ID"],
    "reservationId": os.environ["AGORA_RESERVATION_ID"],
    "slotId": os.environ["AGORA_SLOT_ID"],
    "slotGeneration": int(os.environ["AGORA_SLOT_GENERATION"]),
    "machineGenerationId": os.environ["AGORA_MACHINE_GENERATION_ID"],
    "machineId": os.environ["AGORA_MACHINE_ID"],
    "tokenLabel": os.environ["AGORA_TOKEN_LABEL"],
    "provider": "runpod",
    "providerResourceId": pod_id,
    "providerMachineId": pod_id,
    "providerMachineName": os.environ.get("RUNPOD_POD_NAME") or pod_id,
    "providerIdentitySource": "provider_api",
    "nodeType": os.environ["AGORA_NODE_TYPE"],
    "gpuModel": os.environ["AGORA_GPU_MODEL"],
    "bootId": "__HOST_BOOT_ID__",
    "authorityEpoch": int(os.environ["AGORA_AUTHORITY_EPOCH"]),
}
encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n"
path.parent.mkdir(parents=True, exist_ok=True)
if path.exists():
    existing = json.loads(path.read_text(encoding="utf-8"))
    if existing != identity:
        raise SystemExit("Machine Sentinel durable identity does not match this boot")
    raise SystemExit(0)
descriptor, temporary = tempfile.mkstemp(prefix=".identity.", dir=path.parent, text=True)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY
chmod 600 "$identity_file"

ln -sfn "$runtime_dir/actions/prepare_setup.sh" "$workspace_root/sentinel-verify-setup.sh"
ln -sfn "$runtime_dir/actions/start_training.sh" "$workspace_root/sentinel-start-training.sh"
ln -sfn "$runtime_dir/actions/stop_training.sh" "$workspace_root/sentinel-stop-training.sh"
ln -sfn "$runtime_dir/actions/cancel_training.sh" "$workspace_root/sentinel-cancel-training.sh"
ln -sfn "$runtime_dir/actions/repair_heartbeat.sh" "$workspace_root/sentinel-repair-heartbeat.sh"

exec "$python_bin" -u "$runtime_dir/agora_machine_sentinel_agent.py" \
    --url "$AGORA_SENTINEL_URL" \
    --identity-file "$identity_file" \
    --credential-env-file "$credential_file" \
    --state-file "$state_file" \
    --root "$workspace_root" \
    --setup-revision "$AGORA_SETUP_REVISION" \
    --authority-epoch "$AGORA_AUTHORITY_EPOCH" \
    --fleet-size "${AGORA_FLEET_SIZE:-1}" \
    --timeout "${AGORA_SENTINEL_TIMEOUT_SECONDS:-5}"
