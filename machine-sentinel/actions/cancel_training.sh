#!/usr/bin/env bash
set -Eeuo pipefail

training_run_id="${1:?training run id is required}"
reason_code="${2:?reason code is required}"
approval_receipt_id="${3:?approval receipt id is required}"
root="${AGORA_WORKSPACE_ROOT:-/workspace}"

for value in "$training_run_id" "$reason_code" "$approval_receipt_id"; do
    [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$ ]]
done
if tmux has-session -t agora_gpu >/dev/null 2>&1; then
    exit 75
fi

umask 077
mkdir -p "$root/.agora/machine-sentinel"
printf '%s\n' "$training_run_id" > "$root/.agora/machine-sentinel/cancelled-training-run"
