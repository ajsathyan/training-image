#!/usr/bin/env bash
set -Eeuo pipefail

training_run_id="${1:?training run id is required}"
grace_seconds="${2:?grace seconds are required}"
reason_code="${3:?reason code is required}"
approval_receipt_id="${4:?approval receipt id is required}"
root="${AGORA_WORKSPACE_ROOT:-/workspace}"

for value in "$training_run_id" "$reason_code" "$approval_receipt_id"; do
    [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$ ]]
done
[[ "$grace_seconds" =~ ^[0-9]+$ ]]
(( grace_seconds >= 0 && grace_seconds <= 600 ))
grep -qxF "AGORA_TRAINING_RUN_ID=$training_run_id" "$root/agora.env"
tmux has-session -t agora_gpu >/dev/null 2>&1

umask 077
mkdir -p "$root/.agora/machine-sentinel"
printf '%s\n' "$training_run_id" > "$root/.agora/machine-sentinel/stopped-training-run"
for pid in $(pgrep -f "$root/watch-agora-tmux-loop.sh" 2>/dev/null || true); do
    if [[ "$pid" != "$$" ]]; then
        kill "$pid" 2>/dev/null || true
    fi
done
tmux send-keys -t agora_gpu C-c
deadline=$((SECONDS + grace_seconds))
while tmux has-session -t agora_gpu >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
        exit 75
    fi
    sleep 1
done
