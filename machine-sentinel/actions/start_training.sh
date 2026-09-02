#!/usr/bin/env bash
set -Eeuo pipefail

training_run_id="${1:?training run id is required}"
training_plan_id="${2:?training plan id is required}"
configuration_revision="${3:?configuration revision is required}"
announce_port="${4:?announce port is required}"
root="${AGORA_WORKSPACE_ROOT:-/workspace}"

for value in "$training_run_id" "$training_plan_id" "$configuration_revision"; do
    [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$ ]]
done
[[ "$announce_port" =~ ^[0-9]+$ ]]
(( announce_port >= 1 && announce_port <= 65535 ))
test -x "$root/watchdog-agora-tmux.sh"
test -x "$root/watch-agora-tmux-loop.sh"
test -f "$root/agora.env"
if [[ -f "$root/.agora/machine-sentinel/cancelled-training-run" ]] \
    && grep -qxF "$training_run_id" "$root/.agora/machine-sentinel/cancelled-training-run"; then
    exit 75
fi
if [[ -f "$root/.agora/machine-sentinel/stopped-training-run" ]] \
    && grep -qxF "$training_run_id" "$root/.agora/machine-sentinel/stopped-training-run"; then
    exit 75
fi

if tmux has-session -t agora_gpu >/dev/null 2>&1; then
    exit 0
fi

temporary="$(mktemp "$root/.agora.env.XXXXXX")"
grep -Ev '^(ANNOUNCE_PORT|AGORA_TRAINING_RUN_ID|AGORA_TRAINING_PLAN_ID|AGORA_CONFIGURATION_REVISION)=' \
    "$root/agora.env" > "$temporary" || true
printf 'ANNOUNCE_PORT=%s\nAGORA_TRAINING_RUN_ID=%s\nAGORA_TRAINING_PLAN_ID=%s\nAGORA_CONFIGURATION_REVISION=%s\n' \
    "$announce_port" "$training_run_id" "$training_plan_id" "$configuration_revision" >> "$temporary"
chmod 600 "$temporary"
mv "$temporary" "$root/agora.env"

nohup "$root/watch-agora-tmux-loop.sh" >> "$root/watchdog.log" 2>&1 < /dev/null &
"$root/watchdog-agora-tmux.sh"
