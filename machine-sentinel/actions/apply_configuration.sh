#!/usr/bin/env bash
set -Eeuo pipefail

# The canonical Python dispatcher applies this action atomically after exact
# schema and revision validation. This fixed asset exists as an explicit
# structural boundary and must never accept or evaluate shell command text.
exit 64
