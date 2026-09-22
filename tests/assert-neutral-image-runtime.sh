#!/usr/bin/env bash
set -Eeuo pipefail

status_file="${AGORA_NEUTRAL_STATUS_FILE:-/run/agora-image-bootstrap.status}"
status_json="${AGORA_NEUTRAL_STATUS_JSON:-/run/agora-image-bootstrap.status.json}"
bootstrap_log="${AGORA_NEUTRAL_BOOTSTRAP_LOG:-/var/log/agora-image-bootstrap.log}"
receipt_file="${AGORA_NEUTRAL_RECEIPT_FILE:-/workspace/agora-run/bootstrap-receipt.json}"

test "$(cat "$status_file")" = 0
jq -e '
  .state == "waiting_for_controller_config" and
  .reason == "no active runtime root is configured"
' "$status_json" >/dev/null
grep -q "no active runtime root" "$bootstrap_log"

for session in agora_gpu agora_sentinel agora_px0; do
  if tmux has-session -t "$session" 2>/dev/null; then
    printf 'neutral image unexpectedly started tmux session %s\n' "$session" >&2
    exit 1
  fi
done

test ! -e "$receipt_file"
pgrep -x sshd >/dev/null
pgrep -x cron >/dev/null
