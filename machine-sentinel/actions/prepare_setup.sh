#!/usr/bin/env bash
set -Eeuo pipefail

setup_profile_id="${1:?setup profile id is required}"
setup_revision="${2:?setup revision is required}"
root="${AGORA_WORKSPACE_ROOT:-/workspace}"

[[ "$setup_profile_id" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$ ]]
[[ "$setup_revision" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$ ]]
test -x "$root/launch-agora-gpu0.sh"
test -x "$root/watchdog-agora-tmux.sh"
test -x "$root/watch-agora-tmux-loop.sh"
test -f "$root/agora.env"
