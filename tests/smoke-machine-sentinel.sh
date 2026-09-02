#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime="$repo_root/machine-sentinel"

test -f "$runtime/start-machine-sentinel.sh"
for action in \
    prepare_setup \
    start_training \
    stop_training \
    cancel_training \
    repair_heartbeat \
    apply_configuration; do
    test -f "$runtime/actions/$action.sh"
done

PYTHONPATH="$runtime" python3 -m unittest discover -s "$repo_root/tests" -p 'test_*.py' -v
