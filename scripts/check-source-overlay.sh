#!/bin/sh
set -eu

fleet_root=${1:?fleet source root is required}
image_root=${2:?image source root is required}
work=$(mktemp -d "${TMPDIR:-/tmp}/agora-live-image-overlay.XXXXXX")
trap 'rm -rf "$work"' EXIT HUP INT TERM
cp -a "$fleet_root" "$work/fleet"
cp -a "$image_root" "$work/image"

cd "$work/fleet"
python3 scripts/render_machine_runtime_manifest.py >/tmp/agora-runtime-fingerprint.txt
python3 -m py_compile \
  scripts/agora_machine_sentinel_agent.py \
  scripts/agora_machine_sentinel_spool.py \
  scripts/machine_sentinel/event_spool.py \
  scripts/agora_control/provider/token_capacity_adapter.py \
  scripts/agora_control/execution/image_runtime.py
python3 -m unittest \
  tests.provider.test_global_capacity_wiring \
  tests.provider.test_prepared_assignment_bridge \
  tests.machine_sentinel.test_event_spool \
  tests.operations.test_px0_link

cd "$work/image"
bash -n start.sh
python3 -m py_compile image-runtime/*.py
python3 -m unittest discover -s tests -p 'test_*.py'

printf '%s\n' 'source-overlay: passed'
