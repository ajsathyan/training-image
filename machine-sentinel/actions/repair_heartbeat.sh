#!/usr/bin/env bash
set -Eeuo pipefail

training_run_id="${1:?training run id is required}"
watchdog_revision="${2:?watchdog revision is required}"
root="${AGORA_WORKSPACE_ROOT:-/workspace}"

for value in "$training_run_id" "$watchdog_revision"; do
    [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$ ]]
done
grep -qxF "AGORA_TRAINING_RUN_ID=$training_run_id" "$root/agora.env"
tmux has-session -t agora_gpu >/dev/null 2>&1
test -x "$root/start-agora-heartbeat.sh"
"$root/start-agora-heartbeat.sh"
