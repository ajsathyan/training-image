"""Behavior-bearing remote setup, watchdog, heartbeat, and status script assets."""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union

try:
    from machine_sentinel.process_contract import (
        assignment_owned_process_discovery_shell,
    )
except ModuleNotFoundError:  # package import through the repository root
    from scripts.machine_sentinel.process_contract import (
        assignment_owned_process_discovery_shell,
    )

from ..fleet_read_model_bootstrap import migration_ids

InstallBody = Callable[[dict[str, Any], Optional[dict[str, Any]]], str]
ShellQuote = Callable[[Optional[Union[str, int]]], str]


@dataclass(frozen=True)
class ScriptRenderers:
    shell_quote: ShellQuote
    heartbeat_install_body: InstallBody
    sentinel_install_body: InstallBody
    default_remote_root: str
    default_agora_repo_url: str
    watchdog_script: Optional[Callable[[dict[str, Any]], str]] = None


OUTDATED_AGORA_LIBRARY_ERROR = (
    "Authorization failed: Please use the latest agora library. "
    "Make sure to pull the latest version from Github. Exiting run."
)
AGORA_CLIENT_REPAIR_COOLDOWN_SECONDS = 120
ASSIGNMENT_MANIFEST_SCHEMA_VERSION = 1


def assignment_activity_classifier_shell() -> str:
    """Return the canonical shell classifier for current assignment activity.

    This mirrors ``monitoring.network.latest_current_activity_signal``: only
    evidenced queue, join, state-download, and averaged-parameter lines count,
    and the newest supported line wins.  Generic words such as ``training`` do
    not prove that an attempt has started.
    """

    return r'''assignment_current_classification() {
  local classification
  classification="$({
    [ ! -f "$LAUNCH" ] || cat "$LAUNCH"
    [ ! -f "$LOG" ] || cat "$LOG"
  } | tail -n 1000 | awk '
    { lines[NR] = tolower($0) }
    END {
      for (i = NR; i >= 1; i--) {
        line = lines[i]
        if (line ~ /authorization queue|on waitlist/) {
          print "auth_queue"
          exit
        }
        if (line ~ /(joined|admitted|accepted|assigned|registered|selected)/ && line ~ /(tail|body|head)/) {
          print "joined"
          exit
        }
        if (line ~ /state download|downloaded in/) {
          print "state_download"
          exit
        }
        if (line ~ /averaged parameters with[[:space:]]+[0-9]+[[:space:]]+peers/) {
          print "training_progress"
          exit
        }
      }
    }
  ')"
  if [ -n "$classification" ]; then
    printf '%s' "$classification"
  elif [ -s "$LOG" ] || [ -s "$LAUNCH" ]; then
    printf unknown
  else
    printf never_started
  fi
}
'''


def assignment_owned_server_process_shell(
    *, training_source_root: str | None = None
) -> str:
    """Return exact-root discovery, stop, and absence proof for Agora servers."""

    return assignment_owned_process_discovery_shell(
        training_source_root=training_source_root
    ) + r'''assignment_owned_process_pids() {
  assignment_owned_server_inventory | awk '{print $1}'
}
assignment_owned_server_pids() {
  assignment_owned_server_inventory | awk '$4 == "server" {print $1}'
}
assignment_owned_server_identity_matches() {
  local expected_pid="$1" expected_pgid="$2" expected_started="$3" expected_kind="$4"
  local pid pgid started kind
  while read -r pid pgid started kind; do
    [ "$pid" = "$expected_pid" ] || continue
    [ "$pgid" = "$expected_pgid" ] || return 1
    [ "$started" = "$expected_started" ] || return 1
    [ "$kind" = "$expected_kind" ] || return 1
    return 0
  done < <(assignment_owned_server_inventory)
  return 1
}
assignment_owned_group_is_safe() {
  local expected_pgid="$1"
  local proc identity state pgid started found
  assignment_require_procfs
  found=0
  for proc in "$ASSIGNMENT_PROC_ROOT"/[0-9]*; do
    [ -r "$proc/stat" ] || continue
    kill -0 "${proc##*/}" 2>/dev/null || continue
    identity="$(assignment_proc_stat_identity "$proc")" || continue
    read -r state pgid started <<EOF
$identity
EOF
    [ "$state" != Z ] || continue
    [ "$pgid" = "$expected_pgid" ] || continue
    found=1
    assignment_process_kind "$proc" >/dev/null || return 1
  done
  [ "$found" = 1 ]
}
assignment_assert_no_owned_servers() {
  assignment_require_procfs
  [ -z "$(assignment_owned_process_pids)" ] || assignment_fail "an exact-root Agora process remains"
}
assignment_stop_owned_servers() {
  local pid pgid started kind term_deadline kill_deadline
  assignment_require_procfs
  ASSIGNMENT_PROCESS_PROOF_INCOMPLETE=0
  while read -r pid pgid started kind; do
    assignment_owned_server_identity_matches "$pid" "$pgid" "$started" "$kind" || continue
    if assignment_owned_group_is_safe "$pgid"; then
      kill -TERM -- "-$pgid" 2>/dev/null || true
    else
      kill -TERM "$pid" 2>/dev/null || true
      ASSIGNMENT_PROCESS_PROOF_INCOMPLETE=1
    fi
  done < <(assignment_owned_server_inventory)
  term_deadline=$((SECONDS + 2))
  while true; do
    [ -z "$(assignment_owned_process_pids)" ] && break
    [ "$SECONDS" -lt "$term_deadline" ] || break
    sleep 0.05
  done
  if [ -n "$(assignment_owned_process_pids)" ]; then
    while read -r pid pgid started kind; do
      assignment_owned_server_identity_matches "$pid" "$pgid" "$started" "$kind" || continue
      if assignment_owned_group_is_safe "$pgid"; then
        kill -KILL -- "-$pgid" 2>/dev/null || true
      else
        kill -KILL "$pid" 2>/dev/null || true
        ASSIGNMENT_PROCESS_PROOF_INCOMPLETE=1
      fi
    done < <(assignment_owned_server_inventory)
    kill_deadline=$((SECONDS + 1))
    while true; do
      [ -z "$(assignment_owned_process_pids)" ] && break
      [ "$SECONDS" -lt "$kill_deadline" ] || break
      sleep 0.05
    done
  fi
  assignment_assert_no_owned_servers
  [ "$ASSIGNMENT_PROCESS_PROOF_INCOMPLETE" = 0 ] || assignment_fail "Agora process-group ownership proof was incomplete"
}
assignment_start_owned_cli() {
  local child observed=0 rc=0
  assignment_require_procfs
  "$@" &
  child=$!
  for _ in $(seq 1 40); do
    if assignment_owned_server_inventory | awk -v child="$child" '
        ($1 == child && $4 == "cli") || $4 == "server" { found=1 }
        END { exit found ? 0 : 1 }
      '
    then
      observed=1
      break
    fi
    if ! kill -0 "$child" 2>/dev/null; then
      break
    fi
    sleep 0.05
  done
  if [ "$observed" != 1 ] && kill -0 "$child" 2>/dev/null; then
    kill -TERM "$child" 2>/dev/null || true
    sleep 0.25
    kill -KILL "$child" 2>/dev/null || true
    set +e
    wait "$child" 2>/dev/null
    set -e
    assignment_stop_owned_servers
    assignment_fail "Agora client start could not be proven before releasing assignment lock"
  fi
  assignment_guard_release
  set +e
  wait "$child"
  rc=$?
  set -e
  return "$rc"
}
'''


def _assignment_value(
    machine: dict[str, Any], explicit: Any, *keys: str
) -> Any:
    if explicit is not None:
        return explicit
    for key in keys:
        if machine.get(key) is not None:
            return machine[key]
    return None


def _validated_assignment_remote_root(machine: dict[str, Any], default: str) -> str:
    remote_root = str(machine.get("remoteRoot") or default).strip()
    components = [part for part in remote_root.split("/") if part]
    if (
        not remote_root.startswith("/")
        or posixpath.normpath(remote_root) != remote_root
        or len(components) < 2
        or remote_root in {"/", "/workspace"}
    ):
        raise ValueError("assignment remoteRoot must be a safe normalized absolute path")
    return remote_root


def _assignment_context(
    machine: dict[str, Any],
    *,
    operation_id: str | None = None,
    assignment_generation: int | None = None,
    token_label: str | None = None,
    token_instance: int | None = None,
    token_sha256: str | None = None,
) -> dict[str, Any] | None:
    """Return the canonical remote assignment identity, or legacy mode.

    A machine is legacy only when it has no assignment generation and no
    assignment operation.  A partially populated new assignment fails while
    rendering instead of producing a start path that silently skips fencing.
    """

    generation = _assignment_value(
        machine, assignment_generation, "assignmentGeneration"
    )
    operation = str(
        _assignment_value(machine, operation_id, "assignmentOperationId") or ""
    ).strip()
    if generation is None and not operation:
        return None
    label = str(_assignment_value(machine, token_label, "tokenLabel") or "").strip()
    instance = _assignment_value(machine, token_instance, "tokenInstance")
    machine_id = str(machine.get("id") or "").strip()
    provider = str(machine.get("provider") or "").strip().lower()
    account_scope = str(
        machine.get("accountScope") or machine.get("providerAccount") or ""
    ).strip()
    resource_id = str(
        machine.get("providerResourceId")
        or machine.get("cloudProviderResourceId")
        or machine.get("runpodId")
        or machine.get("vastId")
        or ""
    ).strip()
    digest = str(
        _assignment_value(
            machine,
            token_sha256,
            "assignmentTokenSha256",
            "targetTokenSha256",
            "tokenSha256",
        )
        or ""
    ).strip().lower()
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
        or not isinstance(instance, int)
        or isinstance(instance, bool)
        or instance < 1
        or any(not value for value in (operation, label, machine_id, provider, account_scope, resource_id))
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("assignment fence metadata is incomplete or invalid")
    return {
        "schemaVersion": ASSIGNMENT_MANIFEST_SCHEMA_VERSION,
        "operationId": operation,
        "assignmentGeneration": generation,
        "tokenLabel": label,
        "tokenInstance": instance,
        "machineId": machine_id,
        "provider": provider,
        "accountScope": account_scope,
        "providerResourceId": resource_id,
        "tokenSha256": digest,
    }


def _assignment_shell_contract(
    context: dict[str, Any] | None,
    *,
    render: ScriptRenderers,
    training_source_root: str | None = None,
) -> str:
    """Render the one lock and manifest contract used by all remote starts."""

    if context is None:
        return f"""ASSIGNMENT_FENCE_REQUIRED=0
ASSIGNMENT_MANIFEST="$ROOT/assignment.json"
ASSIGNMENT_LOCK="$ROOT/assignment.lock"
ASSIGNMENT_LOCK_HELD=0
assignment_fail() {{ printf 'assignment-fence: %s\\n' "$1" >&2; exit 76; }}
assignment_guard_acquire() {{
  [ "$ASSIGNMENT_LOCK_HELD" = 0 ] || return 0
  local attempt=0
  while ! mkdir "$ASSIGNMENT_LOCK" 2>/dev/null; do
    local owner=""
    owner="$(cat "$ASSIGNMENT_LOCK/pid" 2>/dev/null || true)"
    case "$owner" in
      ''|*[!0-9]*) ;;
      *)
        if ! kill -0 "$owner" 2>/dev/null; then
          rm -f "$ASSIGNMENT_LOCK/pid" 2>/dev/null || true
          rmdir "$ASSIGNMENT_LOCK" 2>/dev/null || true
          continue
        fi
        ;;
    esac
    attempt=$((attempt + 1))
    [ "$attempt" -lt 120 ] || assignment_fail "timed out acquiring assignment lock"
    sleep 0.25
  done
  printf '%s\\n' "$$" > "$ASSIGNMENT_LOCK/pid"
  ASSIGNMENT_LOCK_HELD=1
}}
assignment_guard_ready() {{
  assignment_guard_acquire
  [ ! -e "$ASSIGNMENT_MANIFEST" ] || assignment_fail "machine became assignment-generation managed"
}}
assignment_guard_release() {{
  if [ "$ASSIGNMENT_LOCK_HELD" = 1 ]; then
    rm -f "$ASSIGNMENT_LOCK/pid" 2>/dev/null || true
    rmdir "$ASSIGNMENT_LOCK" 2>/dev/null || true
    ASSIGNMENT_LOCK_HELD=0
  fi
}}
assignment_manifest_write() {{ :; }}
assignment_token_matches() {{ :; }}
{assignment_owned_server_process_shell(training_source_root=training_source_root)}
trap 'assignment_guard_release' EXIT
"""
    encoded = render.shell_quote(
        json.dumps(context, sort_keys=True, separators=(",", ":"))
    )
    return f"""ASSIGNMENT_FENCE_REQUIRED=1
ASSIGNMENT_EXPECTED_JSON={encoded}
ASSIGNMENT_MANIFEST="$ROOT/assignment.json"
ASSIGNMENT_LOCK="$ROOT/assignment.lock"
ASSIGNMENT_LOCK_HELD=0
assignment_fail() {{
  printf 'assignment-fence: %s\\n' "$1" >&2
  exit 76
}}
{assignment_owned_server_process_shell(training_source_root=training_source_root)}
assignment_guard_acquire() {{
  [ "$ASSIGNMENT_LOCK_HELD" = 0 ] || return 0
  local attempt=0
  while ! mkdir "$ASSIGNMENT_LOCK" 2>/dev/null; do
    local owner=""
    owner="$(cat "$ASSIGNMENT_LOCK/pid" 2>/dev/null || true)"
    case "$owner" in
      ''|*[!0-9]*) ;;
      *)
        if ! kill -0 "$owner" 2>/dev/null; then
          rm -f "$ASSIGNMENT_LOCK/pid" 2>/dev/null || true
          rmdir "$ASSIGNMENT_LOCK" 2>/dev/null || true
          continue
        fi
        ;;
    esac
    attempt=$((attempt + 1))
    [ "$attempt" -lt 120 ] || assignment_fail "timed out acquiring assignment lock"
    sleep 0.25
  done
  printf '%s\\n' "$$" > "$ASSIGNMENT_LOCK/pid"
  ASSIGNMENT_LOCK_HELD=1
}}
assignment_guard_release() {{
  if [ "$ASSIGNMENT_LOCK_HELD" = 1 ]; then
    rm -f "$ASSIGNMENT_LOCK/pid" 2>/dev/null || true
    rmdir "$ASSIGNMENT_LOCK" 2>/dev/null || true
    ASSIGNMENT_LOCK_HELD=0
  fi
}}
assignment_manifest_matches() {{
  local required_state="$1"
  [ -f "$ASSIGNMENT_MANIFEST" ] || return 1
  jq -e --arg state "$required_state" --argjson expected "$ASSIGNMENT_EXPECTED_JSON" '
    .schemaVersion == $expected.schemaVersion and
    .state == $state and
    .operationId == $expected.operationId and
    .assignmentGeneration == $expected.assignmentGeneration and
    .tokenLabel == $expected.tokenLabel and
    .tokenInstance == $expected.tokenInstance and
    .machineId == $expected.machineId and
    .provider == $expected.provider and
    .accountScope == $expected.accountScope and
    .providerResourceId == $expected.providerResourceId and
    .tokenSha256 == $expected.tokenSha256
  ' "$ASSIGNMENT_MANIFEST" >/dev/null 2>&1
}}
assignment_guard_ready() {{
  assignment_guard_acquire
  assignment_manifest_matches ready || assignment_fail "assignment is stale, incomplete, or not ready"
}}
assignment_token_matches() {{
  [ -f "$ROOT/agora.env" ] || assignment_fail "agora.env is missing"
  local actual
  actual="$(set -a; . "$ROOT/agora.env"; set +a; printf '%s' "${{HF_TOKEN:-}}" | sha256sum | awk '{{print $1}}')"
  local expected
  expected="$(printf '%s' "$ASSIGNMENT_EXPECTED_JSON" | jq -r '.tokenSha256')"
  [ "$actual" = "$expected" ] || assignment_fail "staged token digest does not match assignment"
}}
assignment_manifest_write() {{
  local state="$1"
  local temporary
  temporary="$(mktemp "$ROOT/.assignment.json.XXXXXX")"
  printf '%s' "$ASSIGNMENT_EXPECTED_JSON" | jq -c --arg state "$state" '. + {{state:$state}}' > "$temporary"
  printf '\\n' >> "$temporary"
  chmod 600 "$temporary"
  mv -f "$temporary" "$ASSIGNMENT_MANIFEST"
}}
trap 'assignment_guard_release' EXIT
"""


def _assignment_guard_for_machine(
    machine: dict[str, Any],
    *,
    render: ScriptRenderers,
    token_sha256: str | None = None,
    training_source_root: str | None = None,
) -> str:
    return _assignment_shell_contract(
        _assignment_context(machine, token_sha256=token_sha256),
        render=render,
        training_source_root=training_source_root,
    )


def render_assignment_start_guard(
    machine: dict[str, Any],
    *,
    sh_single: ShellQuote,
    token_sha256: str | None = None,
    training_source_root: str | None = None,
) -> str:
    """Public shared guard renderer for assets owned outside execution."""

    renderer = ScriptRenderers(
        shell_quote=sh_single,
        heartbeat_install_body=lambda _machine, _settings: "",
        sentinel_install_body=lambda _machine, _settings: "",
        default_remote_root="",
        default_agora_repo_url="",
    )
    return _assignment_shell_contract(
        _assignment_context(machine, token_sha256=token_sha256),
        render=renderer,
        training_source_root=training_source_root,
    )


def _assignment_fence_precondition(
    context: dict[str, Any],
    *,
    expected_assignment_generation: int,
    expected_assignment: Mapping[str, Any] | None,
    eligibility: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        not isinstance(expected_assignment_generation, int)
        or isinstance(expected_assignment_generation, bool)
        or expected_assignment_generation < 0
        or context["assignmentGeneration"] != expected_assignment_generation + 1
    ):
        raise ValueError("assignment fence expected generation is invalid")

    source: dict[str, Any] | None = None
    if expected_assignment is not None:
        label = str(expected_assignment.get("tokenLabel") or "").strip()
        instance = expected_assignment.get("tokenInstance")
        if (
            not label
            or not isinstance(instance, int)
            or isinstance(instance, bool)
            or instance < 1
            or expected_assignment_generation < 1
        ):
            raise ValueError("assignment fence source assignment is invalid")
        source = {"tokenLabel": label, "tokenInstance": instance}
    elif expected_assignment_generation != 0:
        raise ValueError("neutral assignment fence must start at generation zero")

    classification = str(eligibility.get("classification") or "").strip().lower()
    mode = str(eligibility.get("mode") or "").strip().lower()
    requires_approval = eligibility.get("requiresApproval")
    expected_mode = "default" if classification == "never_started" else "approved_sync"
    apply_authorized = (
        classification == "training_progress"
        and eligibility.get("authorizationKind") == "assignment_apply"
    )
    if (
        eligibility.get("eligible") is not True
        or eligibility.get("machineId") != context["machineId"]
        or eligibility.get("expectedAssignmentGeneration")
        != expected_assignment_generation
        or eligibility.get("operationId") != context["operationId"]
        or classification
        not in {"never_started", "auth_queue", "state_download", "training_progress"}
        or mode != expected_mode
        or requires_approval is not (classification != "never_started")
        or (classification == "training_progress" and not apply_authorized)
    ):
        raise ValueError("assignment fence eligibility binding is invalid")

    return {
        "operationId": context["operationId"],
        "machineId": context["machineId"],
        "provider": context["provider"],
        "accountScope": context["accountScope"],
        "providerResourceId": context["providerResourceId"],
        "expectedAssignmentGeneration": expected_assignment_generation,
        "targetAssignmentGeneration": context["assignmentGeneration"],
        "source": source,
        "classification": classification,
        "mode": mode,
        "requiresApproval": requires_approval,
        "authorizationKind": "assignment_apply" if apply_authorized else None,
    }


def remote_assignment_fence_script(
    machine: dict[str, Any],
    *,
    token_sha256: str,
    expected_assignment_generation: int,
    expected_assignment: Mapping[str, Any] | None,
    eligibility: Mapping[str, Any],
    render: ScriptRenderers,
    training_source_root: str | None = None,
) -> str:
    """Fence one exact assignment, stop its restart paths, and prove no child."""

    remote_root = _validated_assignment_remote_root(
        machine, render.default_remote_root
    )
    context = _assignment_context(machine, token_sha256=token_sha256)
    if context is None:
        raise ValueError("assignment fencing requires generation metadata")
    precondition = _assignment_fence_precondition(
        context,
        expected_assignment_generation=expected_assignment_generation,
        expected_assignment=expected_assignment,
        eligibility=eligibility,
    )
    encoded_precondition = render.shell_quote(
        json.dumps(precondition, sort_keys=True, separators=(",", ":"))
    )
    training_source = training_source_root or f"{remote_root}/agora-source"
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={render.shell_quote(remote_root)}
mkdir -p "$ROOT"
{_assignment_shell_contract(context, render=render, training_source_root=training_source_root)}
FENCE_PRECONDITION_JSON={encoded_precondition}
fence_source_assignment_matches() {{
  if [ -f "$ASSIGNMENT_MANIFEST" ]; then
    jq -e --argjson expected "$FENCE_PRECONDITION_JSON" '
      .schemaVersion == {ASSIGNMENT_MANIFEST_SCHEMA_VERSION} and
      .machineId == $expected.machineId and
      .provider == $expected.provider and
      .accountScope == $expected.accountScope and
      .providerResourceId == $expected.providerResourceId and
      if $expected.source == null then
        .state == "fenced" and
        .assignmentGeneration <= $expected.targetAssignmentGeneration
      else
        (.state == "ready" or .state == "fenced") and
        .assignmentGeneration == $expected.expectedAssignmentGeneration and
        .tokenLabel == $expected.source.tokenLabel and
        .tokenInstance == $expected.source.tokenInstance
      end
    ' "$ASSIGNMENT_MANIFEST" >/dev/null 2>&1 || return 1
    source_is_null="$(printf '%s' "$FENCE_PRECONDITION_JSON" | jq -r '.source == null')"
    if [ "$source_is_null" = true ]; then
      [ ! -e "$ROOT/token-label.txt" ] || return 1
      [ ! -s "$ROOT/private_gpu0.key" ] || return 1
      for runnable in launch-agora-gpu0.sh supervise-agora-gpu0.sh watchdog-agora-tmux.sh watch-agora-tmux-loop.sh sentinel-start-training.sh; do
        [ ! -e "$ROOT/$runnable" ] || return 1
      done
    else
      expected_label="$(printf '%s' "$FENCE_PRECONDITION_JSON" | jq -r '.source.tokenLabel')"
      [ -f "$ROOT/token-label.txt" ] && [ "$(cat "$ROOT/token-label.txt")" = "$expected_label" ] || return 1
      [ -f "$ROOT/machine.json" ] || return 1
      jq -e --argjson expected "$FENCE_PRECONDITION_JSON" '
        .machineId == $expected.machineId and
        .tokenLabel == $expected.source.tokenLabel and
        ((has("tokenInstance") | not) or .tokenInstance == $expected.source.tokenInstance)
      ' "$ROOT/machine.json" >/dev/null 2>&1 || return 1
      [ -f "$ROOT/agora.env" ] || return 1
      actual_digest="$(set -a; . "$ROOT/agora.env"; set +a; printf '%s' "${{HF_TOKEN:-}}" | sha256sum | awk '{{print $1}}')"
      manifest_digest="$(jq -er '.tokenSha256' "$ASSIGNMENT_MANIFEST" 2>/dev/null || true)"
      [ "$actual_digest" = "$manifest_digest" ] || return 1
    fi
    return 0
  fi

  if [ "$(printf '%s' "$FENCE_PRECONDITION_JSON" | jq -r '.source == null')" = true ]; then
    [ ! -e "$ROOT/token-label.txt" ] || return 1
    [ ! -s "$ROOT/private_gpu0.key" ] || return 1
    return 0
  fi
  expected_label="$(printf '%s' "$FENCE_PRECONDITION_JSON" | jq -r '.source.tokenLabel')"
  [ -f "$ROOT/token-label.txt" ] && [ "$(cat "$ROOT/token-label.txt")" = "$expected_label" ] || return 1
  [ -f "$ROOT/machine.json" ] || return 1
  jq -e --argjson expected "$FENCE_PRECONDITION_JSON" '
    .machineId == $expected.machineId and
    .tokenLabel == $expected.source.tokenLabel and
    ((has("tokenInstance") | not) or .tokenInstance == $expected.source.tokenInstance)
  ' "$ROOT/machine.json" >/dev/null 2>&1
}}
LOG="$ROOT/logs/server_gpu0.log"
LAUNCH="$ROOT/logs/launcher-gpu0.log"
{assignment_activity_classifier_shell()}
fence_fresh_eligibility_matches() {{
  local expected_classification current_classification agora_pattern
  expected_classification="$(printf '%s' "$FENCE_PRECONDITION_JSON" | jq -r '.classification')"
  current_classification="$(assignment_current_classification)"
  [ "$current_classification" = "$expected_classification" ] || return 1
  case "$current_classification" in
    training_progress)
      [ "$(printf '%s' "$FENCE_PRECONDITION_JSON" | jq -r '.authorizationKind // empty')" = assignment_apply ] || return 1
      ;;
    joined|unknown) return 1 ;;
  esac
  if [ "$expected_classification" = never_started ]; then
    agora_pattern="$ROOT/(supervise|launch)-agora-gpu0[.]sh"
    ! pgrep -f "$agora_pattern" >/dev/null 2>&1 || return 1
    [ -z "$(assignment_owned_process_pids)" ] || return 1
    ! tmux has-session -t agora_gpu >/dev/null 2>&1 || return 1
    ! pgrep -f "$ROOT/watch-agora-tmux-loop[.]sh" >/dev/null 2>&1 || return 1
    ! tmux has-session -t agora_sentinel >/dev/null 2>&1 || return 1
    [ ! -s "$ROOT/private_gpu0.key" ] || return 1
    [ ! -s "$ROOT/logs/server_gpu0.log" ] || return 1
    [ ! -s "$ROOT/logs/launcher-gpu0.log" ] || return 1
  fi
}}
assignment_guard_acquire
fence_source_assignment_matches || assignment_fail "remote source assignment changed before fence"
fence_fresh_eligibility_matches || assignment_fail "remote eligibility changed before fence"
assignment_manifest_write fenced
wrapper_template="$ROOT/.assignment-start-wrapper.next"
cat > "$wrapper_template" <<'ASSIGNMENT_WRAPPER_EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={render.shell_quote(remote_root)}
{_assignment_shell_contract(context, render=render, training_source_root=training_source_root)}
assignment_guard_ready
assignment_token_matches
case "${{0##*/}}" in
  launch-agora-gpu0.sh|supervise-agora-gpu0.sh)
    assignment_assert_no_owned_servers
    ;;
  watchdog-agora-tmux.sh|sentinel-start-training.sh)
    tmux has-session -t agora_gpu >/dev/null 2>&1 || assignment_assert_no_owned_servers
    ;;
esac
case "${{0##*/}}" in
  watchdog-agora-tmux.sh|sentinel-start-training.sh)
    set +e
    "$0.assignment-body" "$@"
    wrapper_rc=$?
    set -e
    assignment_guard_release
    exit "$wrapper_rc"
    ;;
  launch-agora-gpu0.sh)
    set +e
    assignment_start_owned_cli "$0.assignment-body" "$@"
    wrapper_rc=$?
    set -e
    exit "$wrapper_rc"
    ;;
  repair-agora-client.sh)
    assignment_guard_release
    assignment_fail "legacy repair is disabled after assignment fencing; regenerate assignment-aware assets"
    ;;
esac
set +e
"$0.assignment-body" "$@" &
wrapper_child=$!
assignment_guard_release
wait "$wrapper_child"
wrapper_rc=$?
set -e
exit "$wrapper_rc"
ASSIGNMENT_WRAPPER_EOF
chmod 700 "$wrapper_template"
for target in \
  "$ROOT/launch-agora-gpu0.sh" \
  "$ROOT/supervise-agora-gpu0.sh" \
  "$ROOT/watchdog-agora-tmux.sh" \
  "$ROOT/watch-agora-tmux-loop.sh" \
  "$ROOT/repair-agora-client.sh" \
  "$ROOT/sentinel-start-training.sh"
do
  if [ -f "$target" ]; then
    body="$target.assignment-body"
    if [ ! -f "$body" ]; then
      mv -f "$target" "$body"
      chmod 700 "$body"
    fi
    temporary="$target.assignment-next"
    cp "$wrapper_template" "$temporary"
    chmod 700 "$temporary"
    mv -f "$temporary" "$target"
  fi
done
rm -f "$wrapper_template"
if command -v crontab >/dev/null 2>&1; then
  tmp_cron="$(mktemp)"
  crontab -l 2>/dev/null | grep -Ev 'watch-(agora|heartbeat)-tmux-loop[.]sh' > "$tmp_cron" || true
  crontab "$tmp_cron" 2>/dev/null || true
  rm -f "$tmp_cron"
fi
for pattern in \
  "$ROOT/watch-agora-tmux-loop.sh" \
  "$ROOT/watchdog-agora-tmux.sh" \
  "$ROOT/watch-heartbeat-tmux-loop.sh" \
  "$ROOT/sentinel-start-training.sh" \
  "$ROOT/repair-agora-client.sh" \
  "$ROOT/(supervise|launch)-agora-gpu0.sh"
do
  for pid in $(pgrep -f "$pattern" 2>/dev/null || true); do
    if [ "$pid" != "$$" ]; then kill "$pid" 2>/dev/null || true; fi
  done
done
tmux kill-session -t agora_sentinel >/dev/null 2>&1 || true
tmux kill-session -t agora_heartbeat >/dev/null 2>&1 || true
tmux kill-session -t agora_gpu >/dev/null 2>&1 || true
assignment_stop_owned_servers
AGORA_TRAINING_SOURCE={render.shell_quote(training_source)}
AGORA_CHILD_PATTERN="$ROOT/(supervise|launch)-agora-gpu0[.]sh|$AGORA_TRAINING_SOURCE/.*(agora_cli|run_server)[.]py"
for _ in 1 2 3 4 5; do
  pane_alive=0
  child_alive=0
  tmux has-session -t agora_gpu >/dev/null 2>&1 && pane_alive=1
  pgrep -f "$AGORA_CHILD_PATTERN" >/dev/null 2>&1 && child_alive=1
  [ "$pane_alive" = 0 ] && [ "$child_alive" = 0 ] && break
  sleep 1
done
tmux has-session -t agora_gpu >/dev/null 2>&1 && assignment_fail "agora_gpu session remains after fence"
pgrep -f "$AGORA_CHILD_PATTERN" >/dev/null 2>&1 && assignment_fail "Agora child remains after fence"
assignment_assert_no_owned_servers
printf '__AGORA_ASSIGNMENT_FENCED__ generation=%s operation=%s stopped=yes\\n' \
  {context['assignmentGeneration']} {render.shell_quote(context['operationId'])}
assignment_guard_release
"""


def remote_assignment_stage_script(
    machine: dict[str, Any],
    token: str,
    *,
    token_sha256: str | None = None,
    render: ScriptRenderers,
) -> str:
    """Atomically stage assignment-bound config while leaving the machine stopped."""

    remote_root = _validated_assignment_remote_root(
        machine, render.default_remote_root
    )
    actual_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if token_sha256 is not None and token_sha256.lower() != actual_digest:
        raise ValueError("token SHA-256 does not match token bytes")
    context = _assignment_context(machine, token_sha256=actual_digest)
    if context is None:
        raise ValueError("assignment staging requires generation metadata")
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={render.shell_quote(remote_root)}
mkdir -p "$ROOT"
{_assignment_shell_contract(context, render=render)}
assignment_guard_acquire
if ! assignment_manifest_matches fenced && ! assignment_manifest_matches staged; then
  assignment_fail "matching fenced or staged assignment is required before staging"
fi
assignment_assert_no_owned_servers
env_tmp="$(mktemp "$ROOT/.agora.env.assignment.XXXXXX")"
if [ -f "$ROOT/agora.env" ]; then
  grep -Ev '^(HF_TOKEN|TOKEN_LABEL|MACHINE_ID)=' "$ROOT/agora.env" > "$env_tmp" || true
fi
printf '%s\\n' {render.shell_quote(f'HF_TOKEN={token}')} >> "$env_tmp"
printf '%s\\n' {render.shell_quote(f"TOKEN_LABEL={context['tokenLabel']}")} >> "$env_tmp"
printf '%s\\n' {render.shell_quote(f"MACHINE_ID={context['machineId']}")} >> "$env_tmp"
chmod 600 "$env_tmp"
mv -f "$env_tmp" "$ROOT/agora.env"
label_tmp="$(mktemp "$ROOT/.token-label.XXXXXX")"
printf '%s\\n' {render.shell_quote(context['tokenLabel'])} > "$label_tmp"
chmod 600 "$label_tmp"
mv -f "$label_tmp" "$ROOT/token-label.txt"
machine_tmp="$(mktemp "$ROOT/.machine.json.assignment.XXXXXX")"
printf '%s' "$ASSIGNMENT_EXPECTED_JSON" | jq -c '. | del(.schemaVersion,.operationId,.tokenSha256) + {{updatedAt:(now|todate)}}' > "$machine_tmp"
printf '\\n' >> "$machine_tmp"
chmod 600 "$machine_tmp"
mv -f "$machine_tmp" "$ROOT/machine.json"
assignment_token_matches
repair_tmp="$(mktemp "$ROOT/.repair-agora-client.assignment.XXXXXX")"
cat > "$repair_tmp" <<'ASSIGNMENT_REPAIR_EOF'
{_agora_client_repair_action(machine, render=render).rstrip()}
ASSIGNMENT_REPAIR_EOF
chmod 700 "$repair_tmp"
mv -f "$repair_tmp" "$ROOT/repair-agora-client.sh"
supervisor_tmp="$(mktemp "$ROOT/.supervise-agora-gpu0.assignment.XXXXXX")"
cat > "$supervisor_tmp" <<'ASSIGNMENT_SUPERVISOR_EOF'
{_agora_supervisor_script(machine, render=render).rstrip()}
ASSIGNMENT_SUPERVISOR_EOF
chmod 700 "$supervisor_tmp"
mv -f "$supervisor_tmp" "$ROOT/supervise-agora-gpu0.sh"
assignment_manifest_write staged
printf '__AGORA_ASSIGNMENT_STAGED__ generation=%s operation=%s\\n' \
  {context['assignmentGeneration']} {render.shell_quote(context['operationId'])}
assignment_guard_release
"""


def remote_assignment_image_bootstrap_script(
    machine: dict[str, Any],
    *,
    token_sha256: str,
    bootstrap_script: str,
    start_training: bool,
    render: ScriptRenderers,
    training_source_root: str,
) -> str:
    """Run the baked assignment bootstrap between exact fence checks."""

    remote_root = _validated_assignment_remote_root(
        machine, render.default_remote_root
    )
    context = _assignment_context(machine, token_sha256=token_sha256)
    if context is None:
        raise ValueError("image assignment bootstrap requires generation metadata")
    if not str(bootstrap_script or "").strip():
        raise ValueError("image assignment bootstrap script is required")
    if start_training:
        precondition = ""
        expected_state = "ready"
        marker = "__AGORA_ASSIGNMENT_START_REQUESTED__"
        postcondition = ""
    else:
        precondition = """if ! assignment_manifest_matches ready; then
  assignment_assert_no_owned_servers
fi"""
        expected_state = "staged_or_ready"
        marker = "__AGORA_ASSIGNMENT_STAGED__"
        postcondition = """if assignment_manifest_matches staged; then
  assignment_assert_no_owned_servers
elif ! assignment_manifest_matches ready; then
  assignment_fail "image bootstrap did not preserve staged or already-ready assignment state"
fi
"""
    state_check = (
        ""
        if expected_state == "staged_or_ready"
        else (
            f"assignment_manifest_matches {expected_state} || "
            'assignment_fail "image bootstrap did not preserve the exact assignment state"'
        )
    )
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={render.shell_quote(remote_root)}
mkdir -p "$ROOT"
{_assignment_shell_contract(context, render=render, training_source_root=training_source_root)}
assignment_guard_acquire
{precondition}
assignment_guard_release
{bootstrap_script.rstrip()}
assignment_guard_acquire
{state_check}
assignment_token_matches
{postcondition}printf '{marker} generation=%s operation=%s\n' \
  {context['assignmentGeneration']} {render.shell_quote(context['operationId'])}
assignment_guard_release
"""


def remote_assignment_image_restore_stopped_script(
    machine: dict[str, Any],
    *,
    token_sha256: str,
    fenced_machine: dict[str, Any],
    fenced_token_sha256: str,
    bootstrap_script: str,
    fenced_operation_id: str,
    fenced_assignment_generation: int,
    render: ScriptRenderers,
    training_source_root: str,
) -> str:
    """Restore baked prior config while retaining a stopped assignment fence."""

    remote_root = _validated_assignment_remote_root(
        machine, render.default_remote_root
    )
    context = _assignment_context(machine, token_sha256=token_sha256)
    if context is None:
        raise ValueError("image assignment restore requires generation metadata")
    fenced_context = _assignment_context(
        fenced_machine, token_sha256=fenced_token_sha256
    )
    if fenced_context is None:
        raise ValueError("image assignment restore requires current fence metadata")
    if (
        fenced_context["operationId"] != fenced_operation_id
        or fenced_context["assignmentGeneration"]
        != fenced_assignment_generation
    ):
        raise ValueError("image assignment restore current fence is inconsistent")
    if context["operationId"] != fenced_operation_id:
        raise ValueError("image assignment restore prior fence is inconsistent")
    if (
        not isinstance(fenced_assignment_generation, int)
        or isinstance(fenced_assignment_generation, bool)
        or fenced_assignment_generation < context["assignmentGeneration"]
    ):
        raise ValueError("fenced assignment generation is invalid")
    if not str(bootstrap_script or "").strip():
        raise ValueError("image assignment restore bootstrap script is required")
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={render.shell_quote(remote_root)}
{_assignment_shell_contract(context, render=render, training_source_root=training_source_root)}
assignment_guard_acquire
if assignment_manifest_matches fenced; then
  assignment_token_matches
  assignment_assert_no_owned_servers
  printf '__AGORA_ASSIGNMENT_RESTORED__ generation=%s operation=%s stopped=yes\n' \
    {context['assignmentGeneration']} {render.shell_quote(fenced_operation_id)}
  assignment_guard_release
  exit 0
fi
assignment_guard_release
{_assignment_shell_contract(fenced_context, render=render, training_source_root=training_source_root)}
assignment_guard_acquire
if assignment_manifest_matches staged; then
  assignment_assert_no_owned_servers
  assignment_manifest_write fenced
elif ! assignment_manifest_matches fenced; then
  assignment_fail "matching current image fence or stage is required before restore"
fi
assignment_assert_no_owned_servers
assignment_guard_release
{bootstrap_script.rstrip()}
{_assignment_shell_contract(context, render=render, training_source_root=training_source_root)}
assignment_guard_acquire
assignment_token_matches
assignment_assert_no_owned_servers
assignment_manifest_matches fenced || assignment_fail "image restore did not preserve the prior exact fence"
printf '__AGORA_ASSIGNMENT_RESTORED__ generation=%s operation=%s stopped=yes\n' \
  {context['assignmentGeneration']} {render.shell_quote(fenced_operation_id)}
assignment_guard_release
"""


def remote_assignment_restore_stopped_script(
    machine: dict[str, Any],
    token: str,
    *,
    fenced_operation_id: str,
    fenced_assignment_generation: int,
    token_sha256: str | None = None,
    render: ScriptRenderers,
) -> str:
    """Restore pre-commit config while preserving a non-runnable remote fence."""

    remote_root = _validated_assignment_remote_root(
        machine, render.default_remote_root
    )
    actual_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if token_sha256 is not None and token_sha256.lower() != actual_digest:
        raise ValueError("token SHA-256 does not match token bytes")
    restored = {
        **machine,
        "assignmentOperationId": fenced_operation_id,
        "tokenSha256": actual_digest,
    }
    context = _assignment_context(restored, token_sha256=actual_digest)
    if context is None:
        raise ValueError("assignment restore requires generation metadata")
    if (
        not isinstance(fenced_assignment_generation, int)
        or isinstance(fenced_assignment_generation, bool)
        or fenced_assignment_generation < context["assignmentGeneration"]
    ):
        raise ValueError("fenced assignment generation is invalid")
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={render.shell_quote(remote_root)}
{_assignment_shell_contract(context, render=render)}
assignment_guard_acquire
jq -e --arg operation {render.shell_quote(fenced_operation_id)} \
  --argjson generation {fenced_assignment_generation} \
  --arg machine {render.shell_quote(context['machineId'])} \
  --arg provider {render.shell_quote(context['provider'])} \
  --arg account {render.shell_quote(context['accountScope'])} \
  --arg resource {render.shell_quote(context['providerResourceId'])} '
    .schemaVersion == 1 and (.state == "fenced" or .state == "staged") and
    .operationId == $operation and .assignmentGeneration == $generation and
    .machineId == $machine and .provider == $provider and
    .accountScope == $account and .providerResourceId == $resource
  ' "$ASSIGNMENT_MANIFEST" >/dev/null 2>&1 || assignment_fail "matching operation fence or stage is required before restore"
env_tmp="$(mktemp "$ROOT/.agora.env.restore.XXXXXX")"
if [ -f "$ROOT/agora.env" ]; then
  grep -Ev '^(HF_TOKEN|TOKEN_LABEL|MACHINE_ID)=' "$ROOT/agora.env" > "$env_tmp" || true
fi
printf '%s\\n' {render.shell_quote(f'HF_TOKEN={token}')} >> "$env_tmp"
printf '%s\\n' {render.shell_quote(f"TOKEN_LABEL={context['tokenLabel']}")} >> "$env_tmp"
printf '%s\\n' {render.shell_quote(f"MACHINE_ID={context['machineId']}")} >> "$env_tmp"
chmod 600 "$env_tmp"
mv -f "$env_tmp" "$ROOT/agora.env"
label_tmp="$(mktemp "$ROOT/.token-label.restore.XXXXXX")"
printf '%s\\n' {render.shell_quote(context['tokenLabel'])} > "$label_tmp"
chmod 600 "$label_tmp"
mv -f "$label_tmp" "$ROOT/token-label.txt"
machine_tmp="$(mktemp "$ROOT/.machine.json.restore.XXXXXX")"
printf '%s' "$ASSIGNMENT_EXPECTED_JSON" | jq -c '. | del(.schemaVersion,.operationId,.tokenSha256) + {{updatedAt:(now|todate)}}' > "$machine_tmp"
printf '\\n' >> "$machine_tmp"
chmod 600 "$machine_tmp"
mv -f "$machine_tmp" "$ROOT/machine.json"
assignment_token_matches
assignment_manifest_write fenced
printf '__AGORA_ASSIGNMENT_RESTORED__ generation=%s operation=%s stopped=yes\\n' \
  {context['assignmentGeneration']} {render.shell_quote(fenced_operation_id)}
assignment_guard_release
"""


def remote_assignment_restore_prepared_script(
    machine: dict[str, Any],
    *,
    token_sha256: str,
    fenced_operation_id: str,
    fenced_assignment_generation: int,
    render: ScriptRenderers,
) -> str:
    """Restore a first-assignment cancellation to neutral prepared supply."""

    remote_root = _validated_assignment_remote_root(
        machine, render.default_remote_root
    )
    context = _assignment_context(
        {**machine, "assignmentOperationId": fenced_operation_id},
        assignment_generation=fenced_assignment_generation,
        token_sha256=token_sha256,
    )
    if context is None:
        raise ValueError("prepared assignment restore requires fence metadata")
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={render.shell_quote(remote_root)}
{_assignment_shell_contract(context, render=render)}
assignment_guard_acquire
if ! assignment_manifest_matches fenced && ! assignment_manifest_matches staged; then
  assignment_fail "matching operation fence or stage is required before prepared restore"
fi
if [ -f "$ROOT/agora.env" ]; then
  env_tmp="$(mktemp "$ROOT/.agora.env.prepared.XXXXXX")"
  grep -Ev '^(HF_TOKEN|TOKEN_LABEL|MACHINE_ID|ANNOUNCE_IP|ANNOUNCE_PORT|AGORA_TRAINING_RUN_ID|AGORA_TRAINING_PLAN_ID|AGORA_CONFIGURATION_REVISION)=' \
    "$ROOT/agora.env" > "$env_tmp" || true
  chmod 600 "$env_tmp"
  mv -f "$env_tmp" "$ROOT/agora.env"
fi
if command -v crontab >/dev/null 2>&1; then
  tmp_cron="$(mktemp)"
  crontab -l 2>/dev/null | grep -Ev 'watch-(agora|heartbeat)-tmux-loop[.]sh' > "$tmp_cron" || true
  crontab "$tmp_cron" 2>/dev/null || true
  rm -f "$tmp_cron"
fi
for pattern in "$ROOT/watch-agora-tmux-loop.sh" "$ROOT/watch-heartbeat-tmux-loop.sh"; do
  for pid in $(pgrep -f "$pattern" 2>/dev/null || true); do
    if [ "$pid" != "$$" ]; then kill "$pid" 2>/dev/null || true; fi
  done
done
tmux kill-session -t agora_sentinel >/dev/null 2>&1 || true
tmux kill-session -t agora_heartbeat >/dev/null 2>&1 || true
tmux kill-session -t agora_gpu >/dev/null 2>&1 || true
rm -rf -- "$ROOT/machine-sentinel" "$ROOT/heartbeat-agent" "$ROOT/controller-input"
rm -f -- \
  "$ROOT/bootstrap-receipt.json" \
  "$ROOT/token-label.txt" \
  "$ROOT/machine.json" \
  "$ROOT/private_gpu0.key" \
  "$ROOT/launch-agora-gpu0.sh"* \
  "$ROOT/supervise-agora-gpu0.sh"* \
  "$ROOT/watchdog-agora-tmux.sh"* \
  "$ROOT/watch-agora-tmux-loop.sh"* \
  "$ROOT/repair-agora-client.sh"* \
  "$ROOT/start-machine-sentinel.sh"* \
  "$ROOT/sentinel-verify-setup.sh"* \
  "$ROOT/sentinel-start-training.sh"* \
  "$ROOT/sentinel-stop-training.sh"* \
  "$ROOT/sentinel-cancel-training.sh"* \
  "$ROOT/sentinel-repair-heartbeat.sh"* \
  "$ROOT/start-agora-heartbeat.sh"* \
  "$ROOT/watchdog-heartbeat-tmux.sh"* \
  "$ROOT/watch-heartbeat-tmux-loop.sh"*
assignment_manifest_write fenced
assignment_manifest_matches fenced || assignment_fail "prepared restore changed assignment fence"
printf '__AGORA_ASSIGNMENT_PREPARED_RESTORED__ generation=%s operation=%s stopped=yes\\n' \
  {context['assignmentGeneration']} {render.shell_quote(context['operationId'])}
assignment_guard_release
"""


def remote_assignment_ready_start_script(
    machine: dict[str, Any],
    *,
    token_sha256: str,
    render: ScriptRenderers,
) -> str:
    """Mark a staged assignment ready and request its guarded watchdog start."""

    remote_root = _validated_assignment_remote_root(
        machine, render.default_remote_root
    )
    context = _assignment_context(machine, token_sha256=token_sha256)
    if context is None:
        raise ValueError("assignment start requires generation metadata")
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={render.shell_quote(remote_root)}
{_assignment_shell_contract(context, render=render)}
assignment_guard_acquire
if ! assignment_manifest_matches staged && ! assignment_manifest_matches ready; then
  assignment_fail "matching staged or ready assignment is required before ready"
fi
assignment_token_matches
assignment_manifest_write ready
assignment_guard_release
{remote_watchdog_script(machine, render=render)}
printf '__AGORA_ASSIGNMENT_START_REQUESTED__ generation=%s operation=%s\\n' \
  {context['assignmentGeneration']} {render.shell_quote(context['operationId'])}
"""


def remote_assignment_verify_script(
    machine: dict[str, Any],
    *,
    token_sha256: str,
    require_running: bool,
    render: ScriptRenderers,
    verification_body: str = "",
    training_source_root: str | None = None,
) -> str:
    """Prove exact manifest/config identity and optionally a live Agora child."""

    remote_root = _validated_assignment_remote_root(
        machine, render.default_remote_root
    )
    context = _assignment_context(machine, token_sha256=token_sha256)
    if context is None:
        raise ValueError("assignment verification requires generation metadata")
    running = """tmux has-session -t agora_gpu >/dev/null 2>&1 || assignment_fail "agora_gpu is not running"
pane_pid="$(tmux display-message -p -t agora_gpu '#{pane_pid}' 2>/dev/null || true)"
[ -n "$pane_pid" ] && pgrep -P "$pane_pid" >/dev/null 2>&1 || assignment_fail "Agora child is not running"
owned_server_count="$(assignment_owned_server_pids | wc -l | tr -d '[:space:]')"
[ "$owned_server_count" = 1 ] || assignment_fail "assignment does not have exactly one Agora server"
""" if require_running else ""
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={render.shell_quote(remote_root)}
{_assignment_shell_contract(context, render=render, training_source_root=training_source_root)}
assignment_guard_ready
assignment_token_matches
{running}{verification_body.rstrip()}
printf '__AGORA_ASSIGNMENT_VERIFIED__ generation=%s operation=%s running=%s\\n' \
  {context['assignmentGeneration']} {render.shell_quote(context['operationId'])} {render.shell_quote('yes' if require_running else 'unchecked')}
assignment_guard_release
"""

_AGORA_CLIENT_REPAIR_SCRIPT_TEMPLATE = r'''#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${AGORA_REPAIR_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)}"
SESSION="${AGORA_REPAIR_SESSION:-agora_gpu}"
AGORA_REPO_URL="${AGORA_REPAIR_REPO_URL:-}"
EXECUTE="${AGORA_REPAIR_EXECUTE:-0}"
AUTO_MODE="${AGORA_REPAIR_AUTO:-0}"
ENV_FILE="$ROOT/agora.env"
SOURCE_DIR=__AGORA_SOURCE_DIR__
IDENTITY="$ROOT/private_gpu0.key"
SUPERVISOR="$ROOT/supervise-agora-gpu0.sh"
STATE_FILE="$ROOT/agora-client-repair-auto.state"
LOCKDIR="$ROOT/agora-client-repair.lock"
PROVENANCE_FILE="$ROOT/agora-client-repair-provenance.json"

fail() { printf 'repair-agora-client: %s\n' "$1" >&2; exit 70; }
command -v git >/dev/null 2>&1 || fail "git is required"
command -v tmux >/dev/null 2>&1 || fail "tmux is required"
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is required"
command -v timeout >/dev/null 2>&1 || fail "timeout is required"
[ -d "$SOURCE_DIR/.git" ] || fail "existing Agora checkout is missing"
[ -f "$ENV_FILE" ] || fail "agora.env is missing"
[ -f "$IDENTITY" ] || fail "private_gpu0.key is missing"
[ -x "$SUPERVISOR" ] || fail "Agora supervisor is missing or not executable"
__ASSIGNMENT_REPAIR_CONTRACT__
tmux has-session -t "$SESSION" >/dev/null 2>&1 || fail "agora_gpu supervision is not running"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  fail "another Agora client repair is already running"
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true; assignment_guard_release' EXIT

# shellcheck disable=SC1090
source "$ENV_FILE"
[ -n "${PYTHON_BIN:-}" ] && [ -x "$PYTHON_BIN" ] || fail "configured Python is missing or not executable"
if [ -z "$AGORA_REPO_URL" ]; then
  AGORA_REPO_URL="$(git -C "$SOURCE_DIR" remote get-url origin 2>/dev/null || true)"
fi
[ -n "$AGORA_REPO_URL" ] || fail "Agora repository URL is unavailable"

CURRENT_COMMIT="$(git -C "$SOURCE_DIR" rev-parse HEAD)"
TARGET_COMMIT="$(timeout --signal=TERM 30 git ls-remote "$AGORA_REPO_URL" HEAD | awk 'NR == 1 {print $1}')"
[ -n "$TARGET_COMMIT" ] || fail "could not resolve latest Agora HEAD"
valid_commit() {
  [ "${#1}" = "40" ] || return 1
  case "$1" in *[!0-9a-f]*) return 1 ;; esac
}
valid_commit "$CURRENT_COMMIT" && valid_commit "$TARGET_COMMIT" || fail "Agora commit evidence is invalid"

IDENTITY_BEFORE="$(sha256sum "$IDENTITY" | awk '{print $1}')"
CONFIG_BEFORE="$(grep -v '^AGORA_COMMIT=' "$ENV_FILE" | sha256sum | awk '{print $1}')"
TMUX_SESSION_BEFORE="$(tmux display-message -p -t "$SESSION" '#{session_id}')"
PANE_PID_BEFORE="$(tmux display-message -p -t "$SESSION" '#{pane_pid}')"
PROCESS_PID_BEFORE="$(pgrep -P "$PANE_PID_BEFORE" 2>/dev/null | head -n 1 || true)"
OTHER_SESSIONS_BEFORE="$(tmux list-sessions -F '#{session_id}:#{session_name}:#{session_created}' 2>/dev/null | { grep -v ":$SESSION:" || true; } | sort | sha256sum | awk '{print $1}')"
TMUX_SESSION_AFTER="$TMUX_SESSION_BEFORE"
PANE_PID_AFTER="$PANE_PID_BEFORE"
PROCESS_PID_AFTER="$PROCESS_PID_BEFORE"
OWNED_SERVER_COUNT_AFTER="unchecked"
OWNED_SERVER_IDENTITIES_AFTER="unchecked"
OTHER_SESSIONS_AFTER="$OTHER_SESSIONS_BEFORE"
IDENTITY_AFTER="$IDENTITY_BEFORE"
CONFIG_AFTER="$CONFIG_BEFORE"
AFTER_COMMIT="$CURRENT_COMMIT"
STATUS="preview"
ROLLBACK_ATTEMPTED="no"
ROLLBACK_SUCCEEDED="no"
RESTART_ATTEMPTED="no"
RESTART_SCOPE="none"

emit_evidence() {
  printf '__AGORA_CLIENT_REPAIR__\n'
  printf 'mode=%s\n' "$(if [ "$EXECUTE" = "1" ]; then printf execute; else printf preview; fi)"
  printf 'status=%s\n' "$STATUS"
  printf 'before_commit=%s\n' "$CURRENT_COMMIT"
  printf 'target_commit=%s\n' "$TARGET_COMMIT"
  printf 'after_commit=%s\n' "$AFTER_COMMIT"
  printf 'identity_sha256_before=%s\n' "$IDENTITY_BEFORE"
  printf 'identity_sha256_after=%s\n' "$IDENTITY_AFTER"
  printf 'config_sha256_before=%s\n' "$CONFIG_BEFORE"
  printf 'config_sha256_after=%s\n' "$CONFIG_AFTER"
  printf 'tmux_session_before=%s\n' "$TMUX_SESSION_BEFORE"
  printf 'tmux_session_after=%s\n' "$TMUX_SESSION_AFTER"
  printf 'pane_pid_before=%s\n' "$PANE_PID_BEFORE"
  printf 'pane_pid_after=%s\n' "$PANE_PID_AFTER"
  printf 'process_pid_before=%s\n' "$PROCESS_PID_BEFORE"
  printf 'process_pid_after=%s\n' "$PROCESS_PID_AFTER"
  printf 'owned_server_count_after=%s\n' "$OWNED_SERVER_COUNT_AFTER"
  printf 'owned_server_identities_after=%s\n' "$OWNED_SERVER_IDENTITIES_AFTER"
  printf 'other_sessions_before=%s\n' "$OTHER_SESSIONS_BEFORE"
  printf 'other_sessions_after=%s\n' "$OTHER_SESSIONS_AFTER"
  printf 'rollback_attempted=%s\n' "$ROLLBACK_ATTEMPTED"
  printf 'rollback_succeeded=%s\n' "$ROLLBACK_SUCCEEDED"
  printf 'restart_attempted=%s\n' "$RESTART_ATTEMPTED"
  printf 'restart_scope=%s\n' "$RESTART_SCOPE"
  printf '__END_AGORA_CLIENT_REPAIR__\n'
}

if [ "$EXECUTE" != "1" ]; then
  emit_evidence
  exit 0
fi

if [ "$AUTO_MODE" = "1" ] && [ -f "$STATE_FILE" ]; then
  LAST_TARGET="$(awk 'NR == 1 {print $1}' "$STATE_FILE")"
  LAST_EPOCH="$(awk 'NR == 1 {print $2}' "$STATE_FILE")"
  LAST_RESULT="$(awk 'NR == 1 {print $3}' "$STATE_FILE")"
  if [ "$LAST_TARGET" = "$TARGET_COMMIT" ] && [ "$LAST_RESULT" = "success" ]; then
    STATUS="already_repaired_target"
    emit_evidence
    exit 75
  fi
  case "$LAST_EPOCH" in ''|*[!0-9]*) LAST_EPOCH=0 ;; esac
  if [ "$LAST_TARGET" = "$TARGET_COMMIT" ] && [ "$LAST_RESULT" != "success" ] && [ "$(( $(date +%s) - LAST_EPOCH ))" -lt __AGORA_CLIENT_REPAIR_COOLDOWN_SECONDS__ ]; then
    STATUS="repair_failure_cooldown"
    emit_evidence
    exit 75
  fi
fi

if [ -n "$(git -C "$SOURCE_DIR" status --porcelain --untracked-files=no)" ]; then
  fail "Agora checkout has tracked changes; refusing in-place update"
fi

install_editable_packages() {
  if [ "$PYTHON_BIN" = "/opt/agora-venv/bin/python" ]; then
    timeout --signal=TERM 300 "$PYTHON_BIN" -m pip install \
      --retries 2 --timeout 60 --no-build-isolation --no-deps \
      -e ./pithos -e ./agora_server -e ./agora
  else
    timeout --signal=TERM 300 "$PYTHON_BIN" -m pip install \
      --retries 2 --timeout 60 --build-constraint constraints.txt \
      -e ./pithos -e ./agora_server -e ./agora
  fi
}

ENV_BACKUP="$(mktemp "$ROOT/.agora.env.repair.XXXXXX")"
cp -p "$ENV_FILE" "$ENV_BACKUP"
MACHINE_FILE="$ROOT/machine.json"
MACHINE_BACKUP=""
if [ -f "$MACHINE_FILE" ]; then
  MACHINE_BACKUP="$(mktemp "$ROOT/.machine.json.repair.XXXXXX")"
  cp -p "$MACHINE_FILE" "$MACHINE_BACKUP"
fi

CHECKOUT_CHANGED="no"
INSTALL_ATTEMPTED="no"
UPDATE_FAILED=""
if [ "$CURRENT_COMMIT" != "$TARGET_COMMIT" ]; then
  if ! timeout --signal=TERM 90 git -C "$SOURCE_DIR" fetch --depth 1 "$AGORA_REPO_URL" "$TARGET_COMMIT"; then
    UPDATE_FAILED="fetch_failed"
  elif ! git -C "$SOURCE_DIR" checkout --detach "$TARGET_COMMIT"; then
    UPDATE_FAILED="checkout_failed"
  else
    CHECKOUT_CHANGED="yes"
  fi
fi

if [ -z "$UPDATE_FAILED" ]; then
  INSTALL_ATTEMPTED="yes"
  if ! (cd "$SOURCE_DIR" && install_editable_packages); then
    UPDATE_FAILED="editable_install_failed"
  fi
fi

if [ -z "$UPDATE_FAILED" ]; then
  ENV_TMP="$(mktemp "$ROOT/.agora.env.next.XXXXXX")"
  if ! awk -v commit="$TARGET_COMMIT" '
      BEGIN { seen = 0 }
      /^AGORA_COMMIT=/ { if (!seen) print "AGORA_COMMIT=" commit; seen = 1; next }
      { print }
      END { if (!seen) print "AGORA_COMMIT=" commit }
    ' "$ENV_FILE" > "$ENV_TMP"; then
    UPDATE_FAILED="env_update_failed"
  else
    chmod 600 "$ENV_TMP"
    mv -f "$ENV_TMP" "$ENV_FILE"
  fi
fi

if [ -z "$UPDATE_FAILED" ] && [ -f "$MACHINE_FILE" ]; then
  if ! "$PYTHON_BIN" - "$MACHINE_FILE" "$TARGET_COMMIT" <<'PYMACHINE'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["agoraCommit"] = sys.argv[2]
temporary = path.with_name(path.name + ".next")
temporary.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
temporary.replace(path)
PYMACHINE
  then
    UPDATE_FAILED="machine_metadata_update_failed"
  fi
fi

if [ -n "$UPDATE_FAILED" ]; then
  ROLLBACK_ATTEMPTED="yes"
  set +e
  ROLLBACK_RC=0
  if [ "$CHECKOUT_CHANGED" = "yes" ]; then
    git -C "$SOURCE_DIR" checkout --detach "$CURRENT_COMMIT" || ROLLBACK_RC=1
  fi
  if [ "$INSTALL_ATTEMPTED" = "yes" ] && [ "$ROLLBACK_RC" = "0" ]; then
    (cd "$SOURCE_DIR" && install_editable_packages) || ROLLBACK_RC=1
  fi
  cp -p "$ENV_BACKUP" "$ENV_FILE" || ROLLBACK_RC=1
  if [ -n "$MACHINE_BACKUP" ]; then cp -p "$MACHINE_BACKUP" "$MACHINE_FILE" || ROLLBACK_RC=1; fi
  set -e
  rm -f "$ENV_BACKUP" "$MACHINE_BACKUP"
  if [ "$ROLLBACK_RC" = "0" ]; then
    ROLLBACK_SUCCEEDED="yes"
    STATUS="failed_${UPDATE_FAILED}"
    AFTER_COMMIT="$(git -C "$SOURCE_DIR" rev-parse HEAD 2>/dev/null || printf unknown)"
    IDENTITY_AFTER="$(sha256sum "$IDENTITY" | awk '{print $1}')"
    CONFIG_AFTER="$(grep -v '^AGORA_COMMIT=' "$ENV_FILE" | sha256sum | awk '{print $1}')"
    if [ "$AUTO_MODE" = "1" ]; then
      printf '%s %s failed\n' "$TARGET_COMMIT" "$(date +%s)" > "$STATE_FILE"
    fi
    emit_evidence
    printf 'repair-agora-client: update failed (%s); rollback succeeded\n' "$UPDATE_FAILED" >&2
    exit 72
  fi
  STATUS="failed_${UPDATE_FAILED}_rollback_failed"
  AFTER_COMMIT="$(git -C "$SOURCE_DIR" rev-parse HEAD 2>/dev/null || printf unknown)"
  IDENTITY_AFTER="$(sha256sum "$IDENTITY" | awk '{print $1}')"
  CONFIG_AFTER="$(grep -v '^AGORA_COMMIT=' "$ENV_FILE" | sha256sum | awk '{print $1}')"
  if [ "$AUTO_MODE" = "1" ]; then
    printf '%s %s rollback_failed\n' "$TARGET_COMMIT" "$(date +%s)" > "$STATE_FILE"
  fi
  emit_evidence
  printf 'repair-agora-client: update failed (%s); rollback also failed\n' "$UPDATE_FAILED" >&2
  exit 73
fi

rm -f "$ENV_BACKUP" "$MACHINE_BACKUP"
AFTER_COMMIT="$(git -C "$SOURCE_DIR" rev-parse HEAD)"
[ "$AFTER_COMMIT" = "$TARGET_COMMIT" ] || fail "updated checkout does not match target commit"
IDENTITY_AFTER="$(sha256sum "$IDENTITY" | awk '{print $1}')"
CONFIG_AFTER="$(grep -v '^AGORA_COMMIT=' "$ENV_FILE" | sha256sum | awk '{print $1}')"
[ "$IDENTITY_AFTER" = "$IDENTITY_BEFORE" ] || fail "private_gpu0.key changed during repair"
[ "$CONFIG_AFTER" = "$CONFIG_BEFORE" ] || fail "Agora configuration changed beyond AGORA_COMMIT"
STATUS="$(if [ "$CURRENT_COMMIT" = "$TARGET_COMMIT" ]; then printf reinstalled_current; else printf updated; fi)"

assignment_guard_ready
assignment_token_matches
if [ "$AUTO_MODE" != "1" ]; then
  tmux kill-session -t "$SESSION"
fi
assignment_stop_owned_servers
assignment_assert_no_owned_servers
if [ "$AUTO_MODE" = "1" ]; then
  assignment_guard_release
  RESTART_SCOPE="supervisor_relaunch"
  printf '%s %s success\n' "$TARGET_COMMIT" "$(date +%s)" > "$STATE_FILE"
else
  RESTART_ATTEMPTED="yes"
  RESTART_SCOPE="agora_gpu"
  tmux new-session -d -s "$SESSION" "$SUPERVISOR"
  assignment_guard_release
  for _ in $(seq 1 20); do
    TMUX_SESSION_AFTER="$(tmux display-message -p -t "$SESSION" '#{session_id}' 2>/dev/null || true)"
    PANE_PID_AFTER="$(tmux display-message -p -t "$SESSION" '#{pane_pid}' 2>/dev/null || true)"
    PROCESS_PID_AFTER="$(pgrep -P "$PANE_PID_AFTER" 2>/dev/null | head -n 1 || true)"
    if [ -n "$PANE_PID_AFTER" ] && [ -n "$PROCESS_PID_AFTER" ] && ps -p "$PANE_PID_AFTER" >/dev/null 2>&1 && ps -p "$PROCESS_PID_AFTER" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  [ -n "$PANE_PID_AFTER" ] && ps -p "$PANE_PID_AFTER" >/dev/null 2>&1 || fail "restarted agora_gpu pane is not alive"
  [ -n "$PROCESS_PID_AFTER" ] && ps -p "$PROCESS_PID_AFTER" >/dev/null 2>&1 || fail "restarted Agora child process is not alive"
  [ "$PANE_PID_AFTER" != "$PANE_PID_BEFORE" ] || fail "agora_gpu pane did not restart"
  OWNED_SERVER_COUNT=0
  for _ in $(seq 1 20); do
    OWNED_SERVER_COUNT="$(assignment_owned_server_pids | wc -l | tr -d '[:space:]')"
    [ "$OWNED_SERVER_COUNT" = 0 ] || break
    sleep 0.25
  done
  OWNED_SERVER_IDENTITIES_AFTER="$(assignment_owned_server_inventory | awk '$4 == "server" { if (found) printf ","; printf "%s/%s/%s/%s", $1, $2, $3, $4; found=1 } END { print "" }')"
  if [ -n "$OWNED_SERVER_IDENTITIES_AFTER" ]; then
    OWNED_SERVER_COUNT_AFTER="$(awk -F, '{ print NF }' <<< "$OWNED_SERVER_IDENTITIES_AFTER")"
  else
    OWNED_SERVER_COUNT_AFTER=0
  fi
  [ "$OWNED_SERVER_COUNT_AFTER" = 1 ] || fail "restarted agora_gpu does not have exactly one Agora server"
fi

OTHER_SESSIONS_AFTER="$(tmux list-sessions -F '#{session_id}:#{session_name}:#{session_created}' 2>/dev/null | { grep -v ":$SESSION:" || true; } | sort | sha256sum | awk '{print $1}')"
[ "$OTHER_SESSIONS_AFTER" = "$OTHER_SESSIONS_BEFORE" ] || fail "a non-Agora tmux session changed during repair"
if [ "${ASSIGNMENT_FENCE_REQUIRED:-0}" = "1" ]; then
  [ -f "$MACHINE_FILE" ] || fail "assignment-bound repair requires machine.json"
  [ -f "$ASSIGNMENT_MANIFEST" ] || fail "assignment-bound repair requires assignment.json"
  if ! "$PYTHON_BIN" - \
      "$MACHINE_FILE" "$ASSIGNMENT_MANIFEST" "$PROVENANCE_FILE" \
      "$SOURCE_DIR" "$CURRENT_COMMIT" "$AFTER_COMMIT" <<'PYPROVENANCE'
import datetime as dt
import json
import os
from pathlib import Path
import sys

machine_path, assignment_path, output_path, source_path = map(Path, sys.argv[1:5])
before_commit, after_commit = sys.argv[5:7]
machine = json.loads(machine_path.read_text(encoding="utf-8"))
assignment = json.loads(assignment_path.read_text(encoding="utf-8"))
if machine.get("agoraCommit") != after_commit:
    raise SystemExit("machine.json does not bind the repaired commit")
for key in ("machineId", "provider", "accountScope", "providerResourceId", "assignmentGeneration"):
    if machine.get(key) != assignment.get(key):
        raise SystemExit(f"repair provenance binding mismatch: {key}")
operation_id = assignment.get("operationId")
if not isinstance(operation_id, str) or not operation_id:
    raise SystemExit("repair provenance has no assignment operation")
payload = {
    "schemaVersion": "agora.client-repair-provenance.v1",
    "beforeCommit": before_commit,
    "afterCommit": after_commit,
    "machineId": assignment["machineId"],
    "provider": assignment["provider"],
    "accountScope": assignment["accountScope"],
    "providerResourceId": assignment["providerResourceId"],
    "assignmentGeneration": assignment["assignmentGeneration"],
    "assignmentOperationId": operation_id,
    "sourcePath": str(source_path.resolve()),
    "recordedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
}
temporary = output_path.with_name(output_path.name + ".next")
with temporary.open("w", encoding="utf-8") as stream:
    json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.chmod(temporary, 0o600)
os.replace(temporary, output_path)
PYPROVENANCE
  then
    fail "could not persist assignment-bound repair provenance"
  fi
fi
emit_evidence
'''

AGORA_CLIENT_REPAIR_SCRIPT = _AGORA_CLIENT_REPAIR_SCRIPT_TEMPLATE.replace(
    "__AGORA_CLIENT_REPAIR_COOLDOWN_SECONDS__",
    str(AGORA_CLIENT_REPAIR_COOLDOWN_SECONDS),
).replace(
    "__AGORA_SOURCE_DIR__",
    '"$ROOT/agora-source"',
).replace(
    "__ASSIGNMENT_REPAIR_CONTRACT__",
    "ASSIGNMENT_FENCE_REQUIRED=0\n"
    "assignment_fail() { printf 'assignment-fence: %s\\n' \"$1\" >&2; exit 76; }\n"
    "assignment_guard_ready() { :; }\n"
    "assignment_token_matches() { :; }\n"
    "assignment_guard_release() { :; }\n"
    + assignment_owned_server_process_shell().rstrip(),
)


def _agora_client_repair_action(
    machine: dict[str, Any],
    *,
    render: ScriptRenderers,
    training_source_root: str | None = None,
) -> str:
    source = (
        '"$ROOT/agora-source"'
        if training_source_root is None
        else render.shell_quote(training_source_root)
    )
    return _AGORA_CLIENT_REPAIR_SCRIPT_TEMPLATE.replace(
        "__AGORA_CLIENT_REPAIR_COOLDOWN_SECONDS__",
        str(AGORA_CLIENT_REPAIR_COOLDOWN_SECONDS),
    ).replace(
        "__AGORA_SOURCE_DIR__",
        source,
    ).replace(
        "__ASSIGNMENT_REPAIR_CONTRACT__",
        _assignment_guard_for_machine(
            machine,
            render=render,
            training_source_root=training_source_root,
        ).rstrip(),
    )


def _agora_supervisor_script(
    machine: dict[str, Any],
    *,
    render: ScriptRenderers,
    training_source_root: str | None = None,
) -> str:
    """Render the one training supervisor used by setup and manual repair."""

    remote_root = machine.get("remoteRoot") or render.default_remote_root
    provider = str(machine.get("provider") or "").strip().lower()
    account_scope = str(machine.get("accountScope") or "").strip()
    provider_resource_id = str(
        machine.get("providerResourceId")
        or machine.get("runpodId")
        or machine.get("vastInstanceId")
        or machine.get("vastId")
        or ""
    ).strip()
    machine_generation_id = str(machine.get("machineGenerationId") or "").strip()
    if not machine_generation_id and provider and account_scope and provider_resource_id:
        machine_generation_id = migration_ids(
            "agora-fleet", provider, account_scope, provider_resource_id
        )["machineGenerationId"]
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={render.shell_quote(remote_root)}
LOG="$ROOT/progress.log"
SESSION={render.shell_quote(machine.get("tmuxSession") or "agora_gpu")}
MACHINE_ID={render.shell_quote(machine.get("id"))}
TOKEN_LABEL={render.shell_quote(machine.get("tokenLabel"))}
MACHINE_GENERATION_ID={render.shell_quote(machine_generation_id)}
ASSIGNMENT_GENERATION={render.shell_quote(machine.get("assignmentGeneration"))}
ASSIGNMENT_OPERATION_ID={render.shell_quote(machine.get("assignmentOperationId"))}
NODE_IDENTITY_FILE="$ROOT/agora-node-identity.json"
mkdir -p "$ROOT/logs"
{_assignment_guard_for_machine(machine, render=render, training_source_root=training_source_root)}
assignment_guard_ready
assignment_token_matches
assignment_guard_release
attempt=0

classify_repair_action() {{
  local attempt_log="$1"
  if grep -Fq {render.shell_quote(OUTDATED_AGORA_LIBRARY_ERROR)} "$attempt_log"; then
    printf 'update_client'
  fi
}}

last_repair_attempt_epoch=0
identity_scan_floor=0
identity_scan_offset=0

persist_current_node_identity_conflict() {{
  artifact_tmp="$(mktemp "$ROOT/.agora-node-identity.XXXXXX")"
  jq -cn \
    --arg machineId "$MACHINE_ID" --arg tokenLabel "$TOKEN_LABEL" \
    --arg observedAt "$(date -Iseconds)" \
    --arg bootId "$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)" \
    --arg machineGenerationId "$MACHINE_GENERATION_ID" \
    --arg assignmentGeneration "$ASSIGNMENT_GENERATION" \
    --arg assignmentOperationId "$ASSIGNMENT_OPERATION_ID" \
    --arg processPid "$server_pid" --arg processPgid "$server_pgid" \
    --arg processStartTicks "$server_start_ticks" --arg processKind "$server_kind" \
    '{{version:1,machineId:$machineId,tokenLabel:$tokenLabel,observedAt:$observedAt,bootId:$bootId,machineGenerationId:$machineGenerationId,assignmentGeneration:$assignmentGeneration,assignmentOperationId:$assignmentOperationId,conflict:true,conflictReason:"multiple_node_names_for_current_process",process:{{pid:$processPid,pgid:$processPgid,startTicks:$processStartTicks,kind:$processKind}}}}' \
    > "$artifact_tmp"
  chmod 600 "$artifact_tmp"
  mv -f "$artifact_tmp" "$NODE_IDENTITY_FILE"
}}

capture_current_node_identity() {{
  local server_log="$1" server_log_end scan_start node_name node_names node_name_count server_identity
  local server_pid server_pgid server_start_ticks server_kind artifact_tmp existing_process existing_node existing_conflict
  [ -f "$server_log" ] || return 0
  server_log_end="$(wc -c < "$server_log" | tr -d '[:space:]')"
  case "$server_log_end" in ''|*[!0-9]*) return 0 ;; esac
  if [ "$server_log_end" -lt "$identity_scan_offset" ]; then
    identity_scan_floor=0
    identity_scan_offset=0
  fi
  [ "$server_log_end" -gt "$identity_scan_offset" ] || return 0
  scan_start="$identity_scan_offset"
  if [ "$scan_start" -gt "$identity_scan_floor" ]; then
    scan_start=$((scan_start - 512))
    [ "$scan_start" -ge "$identity_scan_floor" ] || scan_start="$identity_scan_floor"
  fi
  node_names="$(tail -c "+$((scan_start + 1))" "$server_log" \
    | sed -nE 's/^.*Node[[:space:]]+name[[:space:]]*:[[:space:]]*((tail|body|head)-[0-9]+-[A-Za-z0-9._-]+-[0-9]+).*$/\\1/p' \
    | LC_ALL=C sort -u || true)"
  node_name_count="$(printf '%s\n' "$node_names" | sed '/^$/d' | wc -l | tr -d '[:space:]')"
  server_identity="$(assignment_current_owned_identity)" || return 0
  read -r server_pid server_pgid server_start_ticks server_kind <<EOF
$server_identity
EOF
  case "$server_kind" in server|cli) ;; *) return 0 ;; esac
  if [ "$node_name_count" != 1 ]; then
    if [ "$node_name_count" != 0 ]; then
      persist_current_node_identity_conflict
    fi
    identity_scan_offset="$server_log_end"
    return 0
  fi
  node_name="$node_names"
  if [ -f "$NODE_IDENTITY_FILE" ]; then
    existing_process="$(jq -r '(.process // {{}}) | [.pid,.pgid,.startTicks,.kind] | map(tostring) | join("/")' "$NODE_IDENTITY_FILE" 2>/dev/null || true)"
    if [ "$existing_process" = "$server_pid/$server_pgid/$server_start_ticks/$server_kind" ]; then
      existing_conflict="$(jq -r '.conflict // false' "$NODE_IDENTITY_FILE" 2>/dev/null || true)"
      [ "$existing_conflict" != true ] || {{ identity_scan_offset="$server_log_end"; return 0; }}
      existing_node="$(jq -r '.nodeName // empty' "$NODE_IDENTITY_FILE" 2>/dev/null || true)"
      if [ -n "$existing_node" ] && [ "$existing_node" != "$node_name" ]; then
        persist_current_node_identity_conflict
        identity_scan_offset="$server_log_end"
        return 0
      fi
    fi
  fi
  artifact_tmp="$(mktemp "$ROOT/.agora-node-identity.XXXXXX")"
  jq -cn \
    --arg machineId "$MACHINE_ID" \
    --arg tokenLabel "$TOKEN_LABEL" \
    --arg nodeName "$node_name" \
    --arg observedAt "$(date -Iseconds)" \
    --arg bootId "$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)" \
    --arg machineGenerationId "$MACHINE_GENERATION_ID" \
    --arg assignmentGeneration "$ASSIGNMENT_GENERATION" \
    --arg assignmentOperationId "$ASSIGNMENT_OPERATION_ID" \
    --arg processPid "$server_pid" \
    --arg processPgid "$server_pgid" \
    --arg processStartTicks "$server_start_ticks" \
    --arg processKind "$server_kind" \
    '{{version:1,machineId:$machineId,tokenLabel:$tokenLabel,nodeName:$nodeName,observedAt:$observedAt,bootId:$bootId,machineGenerationId:$machineGenerationId,assignmentGeneration:$assignmentGeneration,assignmentOperationId:$assignmentOperationId,process:{{pid:$processPid,pgid:$processPgid,startTicks:$processStartTicks,kind:$processKind}}}}' \
    > "$artifact_tmp"
  chmod 600 "$artifact_tmp"
  mv -f "$artifact_tmp" "$NODE_IDENTITY_FILE"
  identity_scan_offset="$server_log_end"
}}
while true; do
  assignment_guard_ready
  assignment_token_matches
  assignment_guard_release
  attempt=$((attempt + 1))
  start=$(date +%s)
  server_log="$ROOT/logs/server_gpu0.log"
  server_log_start=0
  if [ -f "$server_log" ]; then
    server_log_start="$(wc -c < "$server_log")"
  fi
  identity_scan_floor="$server_log_start"
  identity_scan_offset="$server_log_start"
  attempt_log="$(mktemp "$ROOT/logs/launcher-attempt.XXXXXX")"
  active_link_tmp="$ROOT/logs/.launcher-active.$$"
  rm -f -- "$active_link_tmp"
  ln -- "$attempt_log" "$active_link_tmp"
  mv -f -- "$active_link_tmp" "$ROOT/logs/launcher-active.log"
  printf '%s attempt=%s event=launch\\n' "$(date -Iseconds)" "$attempt" >> "$LOG"
  set +e
  "$ROOT/launch-agora-gpu0.sh" > "$attempt_log" 2>&1 &
  launch_pid=$!
  set -e
  while kill -0 "$launch_pid" 2>/dev/null; do
    capture_current_node_identity "$server_log"
    sleep 2
  done
  set +e
  wait "$launch_pid"
  rc=$?
  set -e
  capture_current_node_identity "$server_log"
  cat "$attempt_log" >> "$ROOT/logs/launcher-gpu0.log"
  if [ -f "$server_log" ]; then
    server_log_end="$(wc -c < "$server_log")"
    if [ "$server_log_end" -gt "$server_log_start" ]; then
      tail -c "+$((server_log_start + 1))" "$server_log" >> "$attempt_log"
    fi
  fi
  elapsed=$(( $(date +%s) - start ))
  if [ "$elapsed" -lt 600 ]; then
    printf '%s attempt=%s exit=%s elapsed=%s retryable=true\\n' "$(date -Iseconds)" "$attempt" "$rc" "$elapsed" >> "$LOG"
  else
    printf '%s attempt=%s exit=%s elapsed=%s retrying=true\\n' "$(date -Iseconds)" "$attempt" "$rc" "$elapsed" >> "$LOG"
  fi
  repair_action="$(classify_repair_action "$attempt_log")"
  attempt_inode="$(stat -c '%d:%i' "$attempt_log" 2>/dev/null || true)"
  active_inode="$(stat -c '%d:%i' "$ROOT/logs/launcher-active.log" 2>/dev/null || true)"
  if [ -n "$attempt_inode" ] && [ "$attempt_inode" = "$active_inode" ]; then
    rm -f -- "$ROOT/logs/launcher-active.log"
  fi
  rm -f "$attempt_log"
  if [ "$repair_action" = "update_client" ]; then
    printf '%s attempt=%s event=client_repair_detected pattern=outdated_library action=update_client\\n' "$(date -Iseconds)" "$attempt" >> "$LOG"
    repair_now="$(date +%s)"
    if [ "$((repair_now - last_repair_attempt_epoch))" -lt {AGORA_CLIENT_REPAIR_COOLDOWN_SECONDS} ]; then
      printf '%s attempt=%s event=client_repair_skipped action=update_client reason=supervisor_cooldown\\n' "$(date -Iseconds)" "$attempt" >> "$LOG"
      sleep 15
      continue
    fi
    last_repair_attempt_epoch="$repair_now"
    set +e
    assignment_guard_ready
    assignment_token_matches
    assignment_guard_release
    AGORA_REPAIR_ROOT="$ROOT" AGORA_REPAIR_EXECUTE=1 AGORA_REPAIR_AUTO=1 \
      "$ROOT/repair-agora-client.sh" >> "$ROOT/logs/client-repair.log" 2>&1
    repair_rc=$?
    set -e
    printf '%s attempt=%s event=client_repair_finished action=update_client exit=%s\\n' "$(date -Iseconds)" "$attempt" "$repair_rc" >> "$LOG"
    if [ "$repair_rc" = "0" ]; then
      sleep 1
      continue
    fi
  fi
  sleep 15
done
"""


def _agora_launch_script(
    machine: dict[str, Any],
    *,
    render: ScriptRenderers,
    training_source_root: str | None = None,
) -> str:
    remote_root = str(machine.get("remoteRoot") or render.default_remote_root)
    assignment_context = _assignment_context(machine)
    source = (
        '"$ROOT/agora-source"'
        if training_source_root is None
        else render.shell_quote(training_source_root)
    )
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={render.shell_quote(remote_root)}
{_assignment_shell_contract(assignment_context, render=render, training_source_root=training_source_root)}
assignment_guard_ready
assignment_token_matches
assignment_assert_no_owned_servers
source "$ROOT/agora.env"
export HF_HOME TRANSFORMERS_CACHE XDG_CACHE_HOME PIP_CACHE_DIR TMPDIR
mkdir -p "$ROOT/logs" "$ROOT/tmp"

port_busy() {{
  ss -ltn "sport = :$HOST_PORT" | awk 'NR > 1 {{found=1}} END {{exit found ? 0 : 1}}'
}}

for _ in $(seq 1 8); do
  if ! port_busy; then
    break
  fi
  sleep 1
done

if port_busy; then
  {{
    date -Iseconds
    echo "Port $HOST_PORT is still busy after waiting 8s"
    ss -ltnp "sport = :$HOST_PORT" || true
  }} >> "$ROOT/progress.log"
  exit 98
fi

cd {source}
assignment_start_owned_cli "$PYTHON_BIN" agora_cli.py   --gpu_id 0   --token "$HF_TOKEN"   --email ""   --host_port "$HOST_PORT"   --announce_port "$ANNOUNCE_PORT"   --log_file "$ROOT/logs/server_gpu0.log"   --identity_path "$ROOT/private_gpu0.key"   --skip_input
"""


def render_machine_runtime_bundle(
    machine: dict[str, Any],
    *,
    render: ScriptRenderers,
    training_source_root: str = "/opt/agora-source",
) -> dict[str, str]:
    """Render the canonical assignment-bound scripts used by baked images.

    The image owns immutable source and dependencies. These assets only bind
    the current controller assignment to launch, supervision, repair, and
    watchdog behavior; rendering them performs no clone, fetch, or install.
    """

    training_source = str(training_source_root).strip()
    if (
        not training_source.startswith("/")
        or posixpath.normpath(training_source) != training_source
        or training_source in {"/", "/opt", "/workspace"}
    ):
        raise ValueError("training_source_root must be a safe normalized absolute path")
    assignment_context = _assignment_context(machine)
    if assignment_context is None:
        raise ValueError("baked runtime requires a current assignment fence")
    guard = render_assignment_start_guard(
        machine,
        sh_single=render.shell_quote,
        token_sha256=str(machine.get("tokenSha256") or ""),
        training_source_root=training_source,
    )
    launch = _agora_launch_script(
        machine,
        render=render,
        training_source_root=training_source,
    )
    repair = _agora_client_repair_action(
        machine,
        render=render,
        training_source_root=training_source,
    )
    return {
        "assignment-start-guard.sh": guard,
        "launch-agora-gpu0.sh": launch,
        "repair-agora-client.sh": repair,
        "supervise-agora-gpu0.sh": _agora_supervisor_script(
            machine, render=render, training_source_root=training_source
        ),
        "install-watchdog.sh": remote_watchdog_script(
            machine, render=render, training_source_root=training_source
        ),
    }


def render_baked_heartbeat_runtime_bundle(
    machine: dict[str, Any],
    heartbeat: Mapping[str, Any],
    *,
    render: ScriptRenderers,
) -> dict[str, str]:
    """Render baked heartbeat launch assets from config and a derived secret file."""

    remote_root = _validated_assignment_remote_root(
        machine, render.default_remote_root
    )
    url = str(heartbeat.get("url") or "").strip()
    secret_file = str(heartbeat.get("secretFile") or "").strip()
    role = str(heartbeat.get("role") or "").strip().lower()
    if not url or role not in {"head", "body", "tail"}:
        raise ValueError("baked heartbeat requires a URL and canonical role")
    expected_prefix = f"{remote_root}/controller-input/"
    if (
        not secret_file.startswith(expected_prefix)
        or posixpath.normpath(secret_file) != secret_file
    ):
        raise ValueError("baked heartbeat secret file must stay under controller-input")

    def timing(name: str, default: float, *, allow_zero: bool = False) -> float:
        try:
            value = float(heartbeat.get(name, default))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"baked heartbeat {name} is invalid") from exc
        if (
            not math.isfinite(value)
            or value < 0
            or (not allow_zero and value == 0)
        ):
            raise ValueError(f"baked heartbeat {name} is invalid")
        return value

    interval = timing("intervalSeconds", 3)
    jitter = timing("jitterSeconds", 1, allow_zero=True)
    timeout = timing("timeoutSeconds", 5)
    q = render.shell_quote
    start = f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={q(remote_root)}
test -x /opt/agora-venv/bin/python || {{ echo 'baked heartbeat Python is missing' >&2; exit 65; }}
test -f /opt/agora-image-runtime/agora_heartbeat_agent.py || {{ echo 'baked heartbeat agent is missing' >&2; exit 65; }}
test -r {q(secret_file)} || {{ echo 'heartbeat credential is missing' >&2; exit 65; }}
exec /opt/agora-venv/bin/python -u /opt/agora-image-runtime/agora_heartbeat_agent.py \
  --url {q(url)} \
  --secret-env-file {q(secret_file)} \
  --machine-id {q(machine.get('id') or machine.get('machineId'))} \
  --role {q(role)} \
  --token-label {q(heartbeat.get('tokenLabel') or machine.get('tokenLabel'))} \
  --runpod-pod-id {q(heartbeat.get('runpodPodId') or '')} \
  --runpod-dc-id {q(heartbeat.get('runpodDcId') or '')} \
  --interval {q(str(interval))} --jitter {q(str(jitter))} --timeout {q(str(timeout))} \
  --state-file "$ROOT/heartbeat-agent/state.json" \
  >> "$ROOT/logs/heartbeat.log" 2>&1
"""
    watchdog = f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={q(remote_root)}
LOCKDIR="$ROOT/heartbeat-watchdog.lock"
mkdir -p "$ROOT/logs" "$ROOT/heartbeat-agent"
if ! mkdir "$LOCKDIR" 2>/dev/null; then exit 0; fi
trap 'rmdir "$LOCKDIR"' EXIT
if ! tmux has-session -t agora_heartbeat >/dev/null 2>&1; then
  tmux new-session -d -s agora_heartbeat "$ROOT/start-agora-heartbeat.sh"
fi
"""
    loop = f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={q(remote_root)}
while true; do
  "$ROOT/watchdog-heartbeat-tmux.sh" >> "$ROOT/logs/heartbeat-watchdog-check.log" 2>&1 || true
  sleep "$(awk -v base={q(str(interval))} -v jitter={q(str(jitter))} 'BEGIN {{ srand(); printf "%.3f", base + (jitter * rand()) }}')"
done
"""
    install = f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={q(remote_root)}
for pid in $(pgrep -f "$ROOT/watch-heartbeat-tmux-loop.sh" 2>/dev/null || true); do
  [ "$pid" = "$$" ] || kill "$pid" 2>/dev/null || true
done
nohup "$ROOT/watch-heartbeat-tmux-loop.sh" >> "$ROOT/heartbeat-watchdog.log" 2>&1 < /dev/null &
if command -v crontab >/dev/null 2>&1; then
  tmp_cron="$(mktemp)"
  crontab -l 2>/dev/null | grep -v 'watch-heartbeat-tmux-loop.sh' > "$tmp_cron" || true
  printf '@reboot pgrep -f "%s/watch-heartbeat-tmux-loop.sh" >/dev/null 2>&1 || nohup "%s/watch-heartbeat-tmux-loop.sh" >> "%s/heartbeat-watchdog.log" 2>&1 < /dev/null &\\n' "$ROOT" "$ROOT" "$ROOT" >> "$tmp_cron"
  printf '* * * * * pgrep -f "%s/watch-heartbeat-tmux-loop.sh" >/dev/null 2>&1 || nohup "%s/watch-heartbeat-tmux-loop.sh" >> "%s/heartbeat-watchdog.log" 2>&1 < /dev/null &\\n' "$ROOT" "$ROOT" "$ROOT" >> "$tmp_cron"
  crontab "$tmp_cron"
  rm -f "$tmp_cron"
fi
tmux kill-session -t agora_heartbeat >/dev/null 2>&1 || true
"$ROOT/watchdog-heartbeat-tmux.sh"
tmux has-session -t agora_heartbeat >/dev/null 2>&1
"""
    return {
        "start-agora-heartbeat.sh": start,
        "watchdog-heartbeat-tmux.sh": watchdog,
        "watch-heartbeat-tmux-loop.sh": loop,
        "install-heartbeat.sh": install,
    }


def remote_agora_tmux_identity_script(machine: dict[str, Any], *, render: ScriptRenderers) -> str:
    remote_root = machine.get("remoteRoot") or render.default_remote_root
    session = machine.get("tmuxSession") or "agora_gpu"
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={render.shell_quote(remote_root)}
SESSION={render.shell_quote(session)}
printf '__AGORA_TMUX_IDENTITY__\\n'
if tmux has-session -t "$SESSION" >/dev/null 2>&1; then
  printf 'exists=yes\\n'
  tmux display-message -p -t "$SESSION" 'session_id=#{{session_id}}
session_created=#{{session_created}}
window_id=#{{window_id}}
pane_id=#{{pane_id}}
pane_pid=#{{pane_pid}}' 2>/dev/null || true
  pane_pid="$(tmux display-message -p -t "$SESSION" '#{{pane_pid}}' 2>/dev/null || true)"
  if [ -n "$pane_pid" ] && ps -p "$pane_pid" >/dev/null 2>&1; then
    printf 'pane_pid_alive=yes\\n'
  else
    printf 'pane_pid_alive=no\\n'
  fi
else
  printf 'exists=no\\n'
fi
printf '__END_AGORA_TMUX_IDENTITY__\\n'
"""

def remote_setup_script(
    machine: dict[str, Any],
    token: str,
    heartbeat: dict[str, Any] | None = None,
    start_agora: bool = True,
    sentinel: dict[str, Any] | None = None,
    identity_backup_sha256: str | None = None,
    *,
    render: ScriptRenderers,
) -> str:
    remote_root = machine.get("remoteRoot") or render.default_remote_root
    agora_repo_url = machine.get("agoraRepoUrl") or render.default_agora_repo_url
    host_port = int(machine.get("hostPort") or 49200)
    announce_port = machine.get("announcePort")
    token_label = machine["tokenLabel"]
    machine_id = machine["id"]
    training_run_id = str(machine.get("runId") or "").strip()
    start_agora_flag = "1" if start_agora else "0"
    backup_sha256 = str(identity_backup_sha256 or "").strip()
    token_sha256 = hashlib.sha256(token.encode("utf-8")).hexdigest()
    assignment_context = _assignment_context(machine, token_sha256=token_sha256)
    guarded_machine = (
        {**machine, "tokenSha256": token_sha256}
        if assignment_context is not None
        else machine
    )
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={render.shell_quote(remote_root)}
HOST_PORT={render.shell_quote(host_port)}
ANNOUNCE_PORT_OVERRIDE={render.shell_quote(announce_port)}
TOKEN_LABEL={render.shell_quote(token_label)}
MACHINE_ID={render.shell_quote(machine_id)}
AGORA_TRAINING_RUN_ID={render.shell_quote(training_run_id)}
AGORA_REPO_URL={render.shell_quote(agora_repo_url)}
START_AGORA={start_agora_flag}
IDENTITY_BACKUP_SHA256={render.shell_quote(backup_sha256)}

umask 077
BASE_DIR="$(dirname "$ROOT")"
CACHE_ROOT="$BASE_DIR/pluralis-agora-cache"
mkdir -p "$BASE_DIR"
mkdir -p "$ROOT"
{_assignment_shell_contract(assignment_context, render=render)}
if [ "$ASSIGNMENT_FENCE_REQUIRED" = 1 ]; then
  assignment_guard_acquire
  if [ -f "$ASSIGNMENT_MANIFEST" ]; then
    current_generation="$(jq -er '.assignmentGeneration' "$ASSIGNMENT_MANIFEST" 2>/dev/null || true)"
    case "$current_generation" in ''|*[!0-9]*) assignment_fail "existing assignment manifest is invalid" ;; esac
    expected_generation="$(printf '%s' "$ASSIGNMENT_EXPECTED_JSON" | jq -r '.assignmentGeneration')"
    [ "$current_generation" -le "$expected_generation" ] || assignment_fail "newer assignment generation is already present"
  fi
else
  assignment_guard_ready
fi
if [ -f "$ROOT/private_gpu0.key" ]; then
  if [ -z "$IDENTITY_BACKUP_SHA256" ]; then
    echo "Refusing setup: existing Agora identity has no verified local backup" >&2
    exit 73
  fi
  REMOTE_IDENTITY_SHA256="$(sha256sum "$ROOT/private_gpu0.key" | awk '{{print $1}}')"
  if [ "$REMOTE_IDENTITY_SHA256" != "$IDENTITY_BACKUP_SHA256" ]; then
    echo "Refusing setup: Agora identity changed after local backup" >&2
    exit 74
  fi
fi
find "$ROOT" -mindepth 1 -maxdepth 1 \
  ! -name 'assignment.lock' ! -name 'assignment.json' -exec rm -rf -- {{}} +
mkdir -p "$ROOT/tmp" "$ROOT/logs" "$CACHE_ROOT/hf" "$CACHE_ROOT/pip" "$CACHE_ROOT/tmp"
assignment_guard_release

log_stage() {{
  printf '%s setup_stage=%s\\n' "$(date -Iseconds)" "$1" | tee -a "$ROOT/setup.log" >> "$ROOT/progress.log"
}}

read_runpod_var() {{
  local key="$1"
  if printenv "$key" >/dev/null 2>&1; then
    printenv "$key"
    return 0
  fi
  for env_file in "$HOME/.env_vars/env_vars.txt" "/root/.env_vars/env_vars.txt"; do
    if [ -f "$env_file" ]; then
      awk -F= -v k="$key" '$1 == k {{print $2; exit}}' "$env_file"
      return 0
    fi
  done
  return 1
}}

ANNOUNCE_PORT="$ANNOUNCE_PORT_OVERRIDE"
if [ -z "$ANNOUNCE_PORT" ]; then
  ANNOUNCE_PORT="$(read_runpod_var "RUNPOD_TCP_PORT_${{HOST_PORT}}" || true)"
fi
if [ -z "$ANNOUNCE_PORT" ] && [ "$START_AGORA" = "1" ]; then
  echo "Public TCP mapping for $HOST_PORT not found; cannot set --announce_port" >&2
  exit 64
fi

log_stage apt_check
missing_packages=()
for cmd in git curl jq tmux ss crontab timeout; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    missing_packages+=( "$cmd" )
  fi
done
if [ "${{#missing_packages[@]}}" -gt 0 ]; then
  log_stage apt_install_start
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y git curl jq tmux procps iproute2 ca-certificates cron coreutils
  log_stage apt_install_done
else
  log_stage apt_skipped
fi

log_stage agora_head_resolve_start
AGORA_EXPECTED_HEAD="$(git ls-remote "$AGORA_REPO_URL" HEAD | awk '{{print $1}}')"
if [ -z "$AGORA_EXPECTED_HEAD" ]; then
  echo "Could not resolve latest Agora HEAD from $AGORA_REPO_URL" >&2
  exit 66
fi
printf '%s agora_expected_head=%s\\n' "$(date -Iseconds)" "$AGORA_EXPECTED_HEAD" >> "$ROOT/progress.log"
log_stage agora_head_resolve_done

log_stage clone_start
git clone --depth 1 "$AGORA_REPO_URL" "$ROOT/agora-source"
log_stage clone_done
log_stage agora_head_verify_start
git -C "$ROOT/agora-source" fetch --depth 1 origin "$AGORA_EXPECTED_HEAD"
git -C "$ROOT/agora-source" checkout --detach "$AGORA_EXPECTED_HEAD"
AGORA_ACTUAL_HEAD="$(git -C "$ROOT/agora-source" rev-parse HEAD)"
if [ "$AGORA_ACTUAL_HEAD" != "$AGORA_EXPECTED_HEAD" ]; then
  echo "Agora checkout mismatch: expected $AGORA_EXPECTED_HEAD got $AGORA_ACTUAL_HEAD" >&2
  exit 67
fi
printf '%s agora_actual_head=%s\\n' "$(date -Iseconds)" "$AGORA_ACTUAL_HEAD" >> "$ROOT/progress.log"
log_stage agora_head_verified
cd "$ROOT/agora-source"

PYTHON_BIN=""
PREBUILT_PYTHON_SELECTED=0
log_stage prebuilt_python_check
if [ -x /opt/agora-venv/bin/python ] && /opt/agora-venv/bin/python - <<'PYCHECK' >/dev/null 2>&1
import sys
import torch
assert sys.version_info[:2] == (3, 13)
assert torch.__version__.split("+", 1)[0].startswith("2.11.")
PYCHECK
then
  PYTHON_BIN="/opt/agora-venv/bin/python"
  PREBUILT_PYTHON_SELECTED=1
  log_stage prebuilt_python_selected
elif command -v python3.13 >/dev/null 2>&1; then
  log_stage venv_create_start
  python3.13 -m venv "$ROOT/venv"
  PYTHON_BIN="$ROOT/venv/bin/python"
  log_stage venv_create_done
elif command -v conda >/dev/null 2>&1; then
  log_stage conda_create_start
  CONDA_BASE="$(conda info --base)"
  # shellcheck disable=SC1091
  source "$CONDA_BASE/etc/profile.d/conda.sh"
  conda create -y -p "$ROOT/conda-env" python=3.13
  PYTHON_BIN="$ROOT/conda-env/bin/python"
  log_stage conda_create_done
else
  echo "Python 3.13 or conda is required for native Agora install" >&2
  exit 65
fi

log_stage pip_upgrade_start
"$PYTHON_BIN" -m pip install --upgrade "pip>=25.3" hatchling editables
log_stage pip_upgrade_done
if "$PYTHON_BIN" - <<'PYCHECK' >/dev/null 2>&1
import torch
assert torch.__version__.split("+", 1)[0].startswith("2.11.")
PYCHECK
then
  log_stage torch_preinstalled
else
  log_stage torch_install_start
  "$PYTHON_BIN" -m pip install torch==2.11.0 --index-url https://download.pytorch.org/whl/cu128
  log_stage torch_install_done
fi
log_stage agora_install_start
if [ "$PREBUILT_PYTHON_SELECTED" = "1" ]; then
  "$PYTHON_BIN" -m pip install --no-build-isolation --no-deps -e ./pithos
  "$PYTHON_BIN" -m pip install --no-build-isolation --no-deps -e ./agora_server
  "$PYTHON_BIN" -m pip install --no-build-isolation --no-deps -e ./agora
else
  "$PYTHON_BIN" -m pip install --build-constraint constraints.txt -e ./pithos
  "$PYTHON_BIN" -m pip install --build-constraint constraints.txt -e ./agora_server
  "$PYTHON_BIN" -m pip install --build-constraint constraints.txt -e ./agora
fi
log_stage agora_install_done

if [ "$ASSIGNMENT_FENCE_REQUIRED" = 1 ]; then
  assignment_guard_acquire
  if [ -f "$ASSIGNMENT_MANIFEST" ]; then
    assignment_manifest_matches ready || assignment_fail "assignment changed or was fenced during setup"
  fi
else
  assignment_guard_ready
fi
cat > "$ROOT/agora.env" <<ENVEOF
HF_TOKEN={token}
TOKEN_LABEL=$TOKEN_LABEL
MACHINE_ID=$MACHINE_ID
AGORA_TRAINING_RUN_ID=$AGORA_TRAINING_RUN_ID
HOST_PORT=$HOST_PORT
ANNOUNCE_PORT=$ANNOUNCE_PORT
PYTHON_BIN=$PYTHON_BIN
AGORA_COMMIT=$AGORA_ACTUAL_HEAD
HF_HOME=$CACHE_ROOT/hf
TRANSFORMERS_CACHE=$CACHE_ROOT/hf
XDG_CACHE_HOME=$CACHE_ROOT
PIP_CACHE_DIR=$CACHE_ROOT/pip
TMPDIR=$CACHE_ROOT/tmp
ENVEOF
chmod 600 "$ROOT/agora.env"
printf '%s\\n' "$TOKEN_LABEL" > "$ROOT/token-label.txt"
cat > "$ROOT/machine.json" <<JSONEOF
{{"machineId":"$MACHINE_ID","tokenLabel":"$TOKEN_LABEL","hostPort":$HOST_PORT,"announcePort":$ANNOUNCE_PORT,"agoraCommit":"$AGORA_ACTUAL_HEAD","createdAt":"$(date -Iseconds)"}}
JSONEOF
if [ "$ASSIGNMENT_FENCE_REQUIRED" = 1 ]; then
  machine_tmp="$(mktemp "$ROOT/.machine.json.assignment.XXXXXX")"
  printf '%s' "$ASSIGNMENT_EXPECTED_JSON" | jq -c \
    --argjson hostPort "$HOST_PORT" --argjson announcePort "$ANNOUNCE_PORT" \
    --arg commit "$AGORA_ACTUAL_HEAD" \
    '. | del(.schemaVersion,.operationId,.tokenSha256) + {{hostPort:$hostPort,announcePort:$announcePort,agoraCommit:$commit,createdAt:(now|todate)}}' \
    > "$machine_tmp"
  printf '\\n' >> "$machine_tmp"
  chmod 600 "$machine_tmp"
  mv -f "$machine_tmp" "$ROOT/machine.json"
fi
printf '__AGORA_PROVENANCE__ agora_commit=%s\\n' "$AGORA_ACTUAL_HEAD"

cat > "$ROOT/launch-agora-gpu0.sh" <<'LAUNCHEOF'
{_agora_launch_script(guarded_machine, render=render).rstrip()}
LAUNCHEOF
chmod 700 "$ROOT/launch-agora-gpu0.sh"

cat > "$ROOT/repair-agora-client.sh" <<'REPAIREOF'
{_agora_client_repair_action(guarded_machine, render=render).rstrip()}
REPAIREOF
chmod 700 "$ROOT/repair-agora-client.sh"

cat > "$ROOT/supervise-agora-gpu0.sh" <<'SUPERVISEEOF'
{_agora_supervisor_script(guarded_machine, render=render).rstrip()}
SUPERVISEEOF
chmod 700 "$ROOT/supervise-agora-gpu0.sh"

log_stage watchdog_install_start
cat > "$ROOT/watchdog-agora-tmux.sh" <<'CHECKEOF'
#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={render.shell_quote(remote_root)}
{_assignment_shell_contract(assignment_context, render=render)}
LOCKDIR="$ROOT/watchdog.lock"
assignment_guard_ready
assignment_token_matches
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  assignment_guard_release
  exit 0
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true; assignment_guard_release' EXIT
if ! tmux has-session -t agora_gpu >/dev/null 2>&1; then
  printf '%s event=watchdog_restart reason=missing_tmux_session\\n' "$(date -Iseconds)" >> "$ROOT/progress.log"
  assignment_assert_no_owned_servers
  tmux new-session -d -s agora_gpu "$ROOT/supervise-agora-gpu0.sh"
fi
rmdir "$LOCKDIR" 2>/dev/null || true
assignment_guard_release
CHECKEOF
chmod 700 "$ROOT/watchdog-agora-tmux.sh"

cat > "$ROOT/watch-agora-tmux-loop.sh" <<'LOOPEOF'
#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={render.shell_quote(remote_root)}
WATCHDOG_BASE_SLEEP_SECONDS=3
WATCHDOG_JITTER_SECONDS=1
jittered_watchdog_sleep_seconds() {{
  awk -v base="$WATCHDOG_BASE_SLEEP_SECONDS" -v jitter="$WATCHDOG_JITTER_SECONDS" 'BEGIN {{ srand(); printf "%.3f", base + (jitter * rand()) }}'
}}
while true; do
  "$ROOT/watchdog-agora-tmux.sh" >> "$ROOT/logs/watchdog-check.log" 2>&1 || true
  sleep "$(jittered_watchdog_sleep_seconds)"
done
LOOPEOF
chmod 700 "$ROOT/watch-agora-tmux-loop.sh"

log_stage watchdog_install_done

# Machine Sentinel is observational and optional.  Keep its failure visible,
# but never let installation prevent the owned training launcher from starting.
set +e
(
  set -Eeuo pipefail
{render.sentinel_install_body(machine, sentinel)}
) 2>> "$ROOT/logs/machine-sentinel-install-error.log"
sentinel_install_rc=$?
set -e
if [ "$sentinel_install_rc" -ne 0 ]; then
  printf '%s event=machine_sentinel_install_failed exit=%s\n' \
    "$(date -Iseconds)" "$sentinel_install_rc" >> "$ROOT/progress.log"
  printf '__AGORA_SENTINEL_INSTALL_STATUS__=failed exit_code=%s\n' "$sentinel_install_rc"
else
  printf '__AGORA_SENTINEL_INSTALL_STATUS__=local_collecting\n'
fi

if [ "$START_AGORA" != "1" ]; then
  if [ "$ASSIGNMENT_FENCE_REQUIRED" = 1 ]; then
    assignment_manifest_write staged
  fi
  assignment_guard_release
  log_stage agora_start_deferred
  printf '%s event=agora_start_deferred machine=%s token_label=%s announce_port=%s\\n' "$(date -Iseconds)" "$MACHINE_ID" "$TOKEN_LABEL" "${{ANNOUNCE_PORT:-pending}}" >> "$ROOT/progress.log"
  printf '%s setup complete machine=%s token_label=%s announce_port=%s start_deferred=true\\n' "$(date -Iseconds)" "$MACHINE_ID" "$TOKEN_LABEL" "${{ANNOUNCE_PORT:-pending}}" >> "$ROOT/progress.log"
  exit 0
fi

if [ "$ASSIGNMENT_FENCE_REQUIRED" = 1 ]; then
  assignment_manifest_write ready
  assignment_token_matches
fi

for pid in $(pgrep -f "$ROOT/watch-agora-tmux-loop.sh" 2>/dev/null || true); do
  if [ "$pid" != "$$" ]; then
    kill "$pid" 2>/dev/null || true
  fi
done
nohup "$ROOT/watch-agora-tmux-loop.sh" >> "$ROOT/watchdog.log" 2>&1 < /dev/null &

if command -v crontab >/dev/null 2>&1; then
  tmp_cron="$(mktemp)"
  crontab -l 2>/dev/null | grep -v 'watch-agora-tmux-loop.sh' > "$tmp_cron" || true
  printf '@reboot pgrep -f "%s/watch-agora-tmux-loop.sh" >/dev/null 2>&1 || nohup "%s/watch-agora-tmux-loop.sh" >> "%s/watchdog.log" 2>&1 < /dev/null &\\n' "$ROOT" "$ROOT" "$ROOT" >> "$tmp_cron"
  printf '* * * * * pgrep -f "%s/watch-agora-tmux-loop.sh" >/dev/null 2>&1 || nohup "%s/watch-agora-tmux-loop.sh" >> "%s/watchdog.log" 2>&1 < /dev/null &\\n' "$ROOT" "$ROOT" "$ROOT" >> "$tmp_cron"
  crontab "$tmp_cron"
  rm -f "$tmp_cron"
fi

tmux kill-session -t agora_gpu >/dev/null 2>&1 || true
assignment_stop_owned_servers
if [ "$ASSIGNMENT_FENCE_REQUIRED" != 1 ]; then
  assignment_guard_ready
  assignment_token_matches
fi
assignment_assert_no_owned_servers
tmux new-session -d -s agora_gpu "$ROOT/supervise-agora-gpu0.sh"
assignment_guard_release
printf '%s event=watchdog_installed\\n' "$(date -Iseconds)" >> "$ROOT/progress.log"
{render.heartbeat_install_body(machine, heartbeat)}
printf '%s setup complete machine=%s token_label=%s announce_port=%s\\n' "$(date -Iseconds)" "$MACHINE_ID" "$TOKEN_LABEL" "$ANNOUNCE_PORT" >> "$ROOT/progress.log"
"""

def remote_deferred_agora_start_script(
    machine: dict[str, Any], heartbeat: dict[str, Any] | None = None, *, render: ScriptRenderers
) -> str:
    remote_root = machine.get("remoteRoot") or render.default_remote_root
    host_port = int(machine.get("hostPort") or 49200)
    announce_port = machine.get("announcePort")
    machine_id = str(machine.get("id") or "")
    token_label = str(machine.get("tokenLabel") or "")
    assignment_context = _assignment_context(machine)
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={render.shell_quote(remote_root)}
HOST_PORT={render.shell_quote(host_port)}
ANNOUNCE_PORT_OVERRIDE={render.shell_quote(announce_port)}
MACHINE_ID={render.shell_quote(machine_id)}
TOKEN_LABEL={render.shell_quote(token_label)}
mkdir -p "$ROOT/logs"
{_assignment_shell_contract(assignment_context, render=render)}
assignment_guard_ready
assignment_token_matches

read_provider_var() {{
  local key="$1"
  if printenv "$key" >/dev/null 2>&1; then
    printenv "$key"
    return 0
  fi
  for env_file in "$HOME/.env_vars/env_vars.txt" "/root/.env_vars/env_vars.txt"; do
    if [ -f "$env_file" ]; then
      awk -F= -v k="$key" '$1 == k {{print $2; exit}}' "$env_file"
      return 0
    fi
  done
  return 1
}}

ANNOUNCE_PORT="$ANNOUNCE_PORT_OVERRIDE"
if [ -z "$ANNOUNCE_PORT" ]; then
  ANNOUNCE_PORT="$(read_provider_var "RUNPOD_TCP_PORT_${{HOST_PORT}}" || true)"
fi
if [ -z "$ANNOUNCE_PORT" ]; then
  ANNOUNCE_PORT="$(read_provider_var "VAST_TCP_PORT_${{HOST_PORT}}" || true)"
fi
if [ -z "$ANNOUNCE_PORT" ]; then
  echo "Public TCP mapping for $HOST_PORT not found; cannot set --announce_port" >&2
  exit 64
fi
if [ ! -f "$ROOT/agora.env" ]; then
  echo "$ROOT/agora.env is missing; run setup before deferred start" >&2
  exit 65
fi
tmp_env="$(mktemp)"
grep -v '^ANNOUNCE_PORT=' "$ROOT/agora.env" > "$tmp_env" || true
printf 'ANNOUNCE_PORT=%s\\n' "$ANNOUNCE_PORT" >> "$tmp_env"
chmod 600 "$tmp_env"
mv "$tmp_env" "$ROOT/agora.env"
# shellcheck disable=SC1090
source "$ROOT/agora.env"
cat > "$ROOT/machine.json" <<JSONEOF
{{"machineId":"$MACHINE_ID","tokenLabel":"$TOKEN_LABEL","hostPort":$HOST_PORT,"announcePort":$ANNOUNCE_PORT,"agoraCommit":"${{AGORA_COMMIT:-}}","createdAt":"$(date -Iseconds)"}}
JSONEOF
printf '%s event=announce_port_ready machine=%s token_label=%s announce_port=%s\\n' "$(date -Iseconds)" "$MACHINE_ID" "$TOKEN_LABEL" "$ANNOUNCE_PORT" >> "$ROOT/progress.log"
assignment_token_matches
assignment_guard_release

{render.watchdog_script(machine) if render.watchdog_script else remote_watchdog_script(machine, render=render)}
{render.heartbeat_install_body(machine, heartbeat)}
"""


def remote_agora_client_repair_script(
    machine: dict[str, Any], *, execute: bool, render: ScriptRenderers
) -> str:
    """Render the provider-free in-place Agora client repair action."""

    remote_root = machine.get("remoteRoot") or render.default_remote_root
    session = machine.get("tmuxSession") or "agora_gpu"
    agora_repo_url = machine.get("agoraRepoUrl") or render.default_agora_repo_url
    assignment_context = _assignment_context(machine)
    if execute:
        repair_action = _agora_client_repair_action(machine, render=render)
        return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={render.shell_quote(remote_root)}
{_assignment_shell_contract(assignment_context, render=render)}
assignment_guard_ready
assignment_token_matches
SUPERVISOR="$ROOT/supervise-agora-gpu0.sh"
SUPERVISOR_BACKUP="$(mktemp "$ROOT/.supervise-agora-gpu0.repair.XXXXXX")"
cp -p "$SUPERVISOR" "$SUPERVISOR_BACKUP"
ACTION_TMP="$(mktemp "$ROOT/.repair-agora-client.XXXXXX")"
cat > "$ACTION_TMP" <<'REPAIREOF'
{repair_action.rstrip()}
REPAIREOF
chmod 700 "$ACTION_TMP"
mv -f "$ACTION_TMP" "$ROOT/repair-agora-client.sh"
SUPERVISOR_TMP="$(mktemp "$ROOT/.supervise-agora-gpu0.XXXXXX")"
cat > "$SUPERVISOR_TMP" <<'SUPERVISEEOF'
{_agora_supervisor_script(machine, render=render).rstrip()}
SUPERVISEEOF
chmod 700 "$SUPERVISOR_TMP"
mv -f "$SUPERVISOR_TMP" "$SUPERVISOR"
assignment_guard_release
set +e
AGORA_REPAIR_ROOT={render.shell_quote(remote_root)} \
AGORA_REPAIR_SESSION={render.shell_quote(session)} \
AGORA_REPAIR_REPO_URL={render.shell_quote(agora_repo_url)} \
AGORA_REPAIR_EXECUTE=1 AGORA_REPAIR_AUTO=0 \
  "$ROOT/repair-agora-client.sh"
rc=$?
set -e
if [ "$rc" != "0" ]; then
  cp -p "$SUPERVISOR_BACKUP" "$SUPERVISOR"
fi
rm -f "$SUPERVISOR_BACKUP"
exit "$rc"
"""
    repair_action = _agora_client_repair_action(machine, render=render)
    return f"""#!/usr/bin/env bash
AGORA_REPAIR_ROOT={render.shell_quote(remote_root)}
AGORA_REPAIR_SESSION={render.shell_quote(session)}
AGORA_REPAIR_REPO_URL={render.shell_quote(agora_repo_url)}
AGORA_REPAIR_EXECUTE=0
AGORA_REPAIR_AUTO=0
export AGORA_REPAIR_ROOT AGORA_REPAIR_SESSION AGORA_REPAIR_REPO_URL AGORA_REPAIR_EXECUTE AGORA_REPAIR_AUTO
{repair_action}
"""


def remote_vast_announce_repair_script(
    machine: dict[str, Any],
    announce_ip: str,
    announce_port: int,
    *,
    execute: bool,
    render: ScriptRenderers,
) -> str:
    """Render a fail-closed, provider-free announce endpoint repair."""

    remote_root = machine.get("remoteRoot") or render.default_remote_root
    session = machine.get("tmuxSession") or "agora_gpu"
    assignment_context = _assignment_context(machine)
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={render.shell_quote(remote_root)}
SESSION={render.shell_quote(session)}
REQUESTED_IP={render.shell_quote(announce_ip)}
REQUESTED_PORT={render.shell_quote(announce_port)}
EXECUTE={1 if execute else 0}
ENV_FILE="$ROOT/agora.env"
LAUNCHER="$ROOT/launch-agora-gpu0.sh"
SUPERVISOR="$ROOT/supervise-agora-gpu0.sh"
IDENTITY="$ROOT/private_gpu0.key"
{_assignment_shell_contract(assignment_context, render=render)}

fail() {{ printf 'repair-vast-announce: %s\\n' "$1" >&2; exit 70; }}
command -v python3 >/dev/null 2>&1 || fail "python3 is required"
command -v tmux >/dev/null 2>&1 || fail "tmux is required"
[ -f "$ENV_FILE" ] || fail "agora.env is missing"
[ -f "$LAUNCHER" ] || fail "launch-agora-gpu0.sh is missing"
[ -x "$SUPERVISOR" ] || fail "supervise-agora-gpu0.sh is missing or not executable"
[ -f "$IDENTITY" ] || fail "private_gpu0.key is missing"
tmux has-session -t "$SESSION" >/dev/null 2>&1 || fail "agora_gpu supervision is not running"

ENV_PORT_COUNT="$(grep -c '^ANNOUNCE_PORT=' "$ENV_FILE" || true)"
ENV_IP_COUNT="$(grep -c '^ANNOUNCE_IP=' "$ENV_FILE" || true)"
[ "$ENV_PORT_COUNT" = "1" ] || fail "agora.env must contain exactly one ANNOUNCE_PORT"
case "$ENV_IP_COUNT" in 0|1) ;; *) fail "agora.env contains duplicate ANNOUNCE_IP entries" ;; esac
BEFORE_PORT="$(sed -n 's/^ANNOUNCE_PORT=//p' "$ENV_FILE")"
ENV_BEFORE_IP="$(sed -n 's/^ANNOUNCE_IP=//p' "$ENV_FILE")"
python3 - "$REQUESTED_IP" "$REQUESTED_PORT" "$ENV_BEFORE_IP" "$BEFORE_PORT" <<'PYVALIDATE' || fail "announce endpoint validation failed"
import ipaddress
import sys

requested_ip, requested_port, before_ip, before_port = sys.argv[1:]
ipaddress.IPv4Address(requested_ip)
port = int(requested_port)
if not 1 <= port <= 65535:
    raise ValueError("requested announce port is outside 1..65535")
if before_ip:
    ipaddress.IPv4Address(before_ip)
old_port = int(before_port)
if not 1 <= old_port <= 65535:
    raise ValueError("stored announce port is outside 1..65535")
PYVALIDATE

LAUNCHER_IP_SHAPE="$(python3 - "$LAUNCHER" <<'PYSHAPE'
from pathlib import Path
import ipaddress
import re
import shlex
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8")
lines = text.splitlines()
launch_pattern = re.compile(r'^exec "\\$PYTHON_BIN" agora_cli\\.py \\\\$')
port_pattern = re.compile(r'^(?P<indent>[ \\t]*)--announce_port[ \\t]+"\\$ANNOUNCE_PORT"[ \\t]+\\\\$')
variable_pattern = re.compile(r'^(?P<indent>[ \\t]*)--announce_ip[ \\t]+"\\$ANNOUNCE_IP"[ \\t]+\\\\$')
literal_pattern = re.compile(r'^(?P<indent>[ \\t]*)--announce_ip[ \\t]+(?P<value>"[^"]+"|\\x27[^\\x27]+\\x27|[^"\\x27 \\t]+)[ \\t]+\\\\$')
end_pattern = re.compile(r'^[ \\t]*--skip_input[ \\t]*$')
launch = [index for index, line in enumerate(lines) if launch_pattern.fullmatch(line)]
port = [index for index, line in enumerate(lines) if port_pattern.fullmatch(line)]
end = [index for index, line in enumerate(lines) if end_pattern.fullmatch(line)]
variable = [index for index, line in enumerate(lines) if variable_pattern.fullmatch(line)]
exec_lines = [
    index for index, line in enumerate(lines)
    if line.startswith('exec "$PYTHON_BIN" agora_cli.py')
]
literal = []
literal_value = ""
for index, line in enumerate(lines):
    match = literal_pattern.fullmatch(line)
    if match is None or index in variable:
        continue
    raw_value = match.group("value")
    value = raw_value[1:-1] if raw_value.startswith(('"', "'")) else raw_value
    try:
        value = str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError:
        raise SystemExit(1)
    literal.append(index)
    literal_value = value
ip = variable + literal
style = ""
kind = ""
if len(launch) == 1:
    if len(exec_lines) != 1 or len(port) != 1 or len(end) != 1 or len(ip) not in (0, 1):
        raise SystemExit(1)
    if not launch[0] < port[0] < end[0]:
        raise SystemExit(1)
    if ip and not launch[0] < ip[0] < end[0]:
        raise SystemExit(1)
    style = "multiline"
    kind = "variable" if variable else "literal" if literal else "absent"
elif len(exec_lines) == 1:
    line = lines[exec_lines[0]]
    if chr(92) in line:
        raise SystemExit(1)
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = list(lexer)
    if tokens[:3] != ["exec", "$PYTHON_BIN", "agora_cli.py"]:
        raise SystemExit(1)
    if any(token in {{";", "&&", "||", "|", "<", ">", "(", ")"}} for token in tokens):
        raise SystemExit(1)
    values = {{}}
    standalone = []
    index = 3
    while index < len(tokens):
        flag = tokens[index]
        if not flag.startswith("--"):
            raise SystemExit(1)
        if flag == "--skip_input":
            standalone.append(flag)
            index += 1
            continue
        if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
            raise SystemExit(1)
        values.setdefault(flag, []).append(tokens[index + 1])
        index += 2
    if values.get("--announce_port") != ["$ANNOUNCE_PORT"]:
        raise SystemExit(1)
    ip_values = values.get("--announce_ip", [])
    if len(ip_values) > 1 or standalone.count("--skip_input") != 1:
        raise SystemExit(1)
    style = "single"
    if not ip_values:
        kind = "absent"
    elif ip_values[0] == "$ANNOUNCE_IP":
        kind = "variable"
    else:
        try:
            literal_value = str(ipaddress.IPv4Address(ip_values[0]))
        except ipaddress.AddressValueError:
            raise SystemExit(1)
        kind = "literal"
else:
    raise SystemExit(1)
expected_ip_count = 0 if kind == "absent" else 1
if text.count('exec "$PYTHON_BIN" agora_cli.py') != 1:
    raise SystemExit(1)
if text.count("--announce_port") != 1 or text.count("--announce_ip") != expected_ip_count:
    raise SystemExit(1)
print(f"{{style}}:{{kind}}:{{literal_value}}")
PYSHAPE
)" || fail "launcher shape is unsupported or ambiguous"
LAUNCHER_STYLE="${{LAUNCHER_IP_SHAPE%%:*}}"
LAUNCHER_IP_DETAIL="${{LAUNCHER_IP_SHAPE#*:}}"
LAUNCHER_IP_KIND="${{LAUNCHER_IP_DETAIL%%:*}}"
LAUNCHER_LITERAL_IP="${{LAUNCHER_IP_DETAIL#*:}}"
case "$LAUNCHER_IP_KIND" in
  variable)
    [ -n "$ENV_BEFORE_IP" ] || fail "launcher references ANNOUNCE_IP but agora.env does not define it"
    BEFORE_IP="$ENV_BEFORE_IP"
    ;;
  literal) BEFORE_IP="$LAUNCHER_LITERAL_IP" ;;
  absent) BEFORE_IP="" ;;
  *) fail "launcher shape is unsupported or ambiguous" ;;
esac

IDENTITY_BEFORE="$(sha256sum "$IDENTITY" | awk '{{print $1}}')"
TMUX_SESSION_BEFORE="$(tmux display-message -p -t "$SESSION" '#{{session_id}}')"
PANE_PID_BEFORE="$(tmux display-message -p -t "$SESSION" '#{{pane_pid}}')"
[ -n "$PANE_PID_BEFORE" ] && ps -p "$PANE_PID_BEFORE" >/dev/null 2>&1 || fail "agora_gpu pane process is not alive"
PROCESS_PID_BEFORE="$(pgrep -P "$PANE_PID_BEFORE" | head -n 1 || true)"
[ -n "$PROCESS_PID_BEFORE" ] && ps -p "$PROCESS_PID_BEFORE" >/dev/null 2>&1 || fail "Agora child process is not alive"
OTHER_SESSIONS_BEFORE="$(tmux list-sessions -F '#{{session_id}}:#{{session_name}}:#{{session_created}}' 2>/dev/null | {{ grep -v ":$SESSION:" || true; }} | sha256sum | awk '{{print $1}}')"

TMUX_SESSION_AFTER=""
PANE_PID_AFTER=""
PROCESS_PID_AFTER=""
IDENTITY_AFTER="$IDENTITY_BEFORE"
OTHER_SESSIONS_AFTER="$OTHER_SESSIONS_BEFORE"
if [ "$EXECUTE" = "1" ]; then
  assignment_guard_ready
  assignment_token_matches
  assignment_guard_release
  ENV_TMP="$(mktemp "$ROOT/.agora.env.announce-repair.XXXXXX")"
  LAUNCH_TMP="$(mktemp "$ROOT/.launch-agora-gpu0.announce-repair.XXXXXX")"
  ENV_BACKUP="$(mktemp "$ROOT/.agora.env.announce-backup.XXXXXX")"
  LAUNCH_BACKUP="$(mktemp "$ROOT/.launch-agora-gpu0.announce-backup.XXXXXX")"
  cp -p "$ENV_FILE" "$ENV_BACKUP"
  cp -p "$LAUNCHER" "$LAUNCH_BACKUP"
  MOVED=0
  committed=0
  cleanup_or_rollback() {{
    rc=$?
    trap - EXIT
    if [ "$committed" != "1" ] && [ "$MOVED" = "1" ]; then
      mv -f "$ENV_BACKUP" "$ENV_FILE" || true
      mv -f "$LAUNCH_BACKUP" "$LAUNCHER" || true
      tmux kill-session -t "$SESSION" >/dev/null 2>&1 || true
      assignment_stop_owned_servers
      tmux new-session -d -s "$SESSION" "$SUPERVISOR" >/dev/null 2>&1 || true
    fi
    rm -f "$ENV_TMP" "$LAUNCH_TMP" "$ENV_BACKUP" "$LAUNCH_BACKUP"
    assignment_guard_release
    exit "$rc"
  }}
  trap cleanup_or_rollback EXIT

  python3 - "$ENV_FILE" "$ENV_TMP" "$REQUESTED_IP" "$REQUESTED_PORT" <<'PYENV'
from pathlib import Path
import sys

source, target, announce_ip, announce_port = sys.argv[1:]
lines = Path(source).read_text(encoding="utf-8").splitlines()
output = []
seen_ip = False
for line in lines:
    if line.startswith("ANNOUNCE_IP="):
        output.append(f"ANNOUNCE_IP={{announce_ip}}")
        seen_ip = True
    elif line.startswith("ANNOUNCE_PORT="):
        if not seen_ip:
            output.append(f"ANNOUNCE_IP={{announce_ip}}")
            seen_ip = True
        output.append(f"ANNOUNCE_PORT={{announce_port}}")
    else:
        output.append(line)
Path(target).write_text("\\n".join(output) + "\\n", encoding="utf-8")
PYENV
  chmod 600 "$ENV_TMP"

  python3 - "$LAUNCHER" "$LAUNCH_TMP" "$LAUNCHER_STYLE" <<'PYLAUNCH'
from pathlib import Path
import re
import sys

source, target, style = sys.argv[1:]
text = Path(source).read_text(encoding="utf-8")
if style == "multiline":
    existing = re.compile(
        r'^(?P<indent>[ \\t]*)--announce_ip[ \\t]+'
        r'(?:"\\$ANNOUNCE_IP"|"(?:[0-9]{{1,3}}\\.){{3}}[0-9]{{1,3}}"|\\x27(?:[0-9]{{1,3}}\\.){{3}}[0-9]{{1,3}}\\x27|(?:[0-9]{{1,3}}\\.){{3}}[0-9]{{1,3}})'
        r'[ \\t]+\\\\$',
        re.MULTILINE,
    )
    if "--announce_ip" in text:
        text, count = existing.subn(
            lambda match: match.group("indent")
            + '--announce_ip "$ANNOUNCE_IP" '
            + chr(92),
            text,
        )
    else:
        port = re.compile(r'^(?P<indent>[ \\t]*)(--announce_port[ \\t]+"\\$ANNOUNCE_PORT"[ \\t]+\\\\)$', re.MULTILINE)
        text, count = port.subn(r'\\g<indent>--announce_ip "$ANNOUNCE_IP" \\\n\\g<indent>\\g<2>', text)
elif style == "single":
    launch = re.compile(r'^exec "\\$PYTHON_BIN" agora_cli\\.py[ \\t]+.*$', re.MULTILINE)
    matches = list(launch.finditer(text))
    if len(matches) != 1:
        raise SystemExit(1)
    line = matches[0].group(0)
    ip_arg = re.compile(
        r'(?P<lead>[ \\t]+)--announce_ip[ \\t]+'
        r'(?:"\\$ANNOUNCE_IP"|"(?:[0-9]{{1,3}}\\.){{3}}[0-9]{{1,3}}"|\\x27(?:[0-9]{{1,3}}\\.){{3}}[0-9]{{1,3}}\\x27|(?:[0-9]{{1,3}}\\.){{3}}[0-9]{{1,3}})'
        r'(?=[ \\t]+--|[ \\t]*$)'
    )
    if "--announce_ip" in line:
        line, count = ip_arg.subn(
            lambda match: match.group("lead") + '--announce_ip "$ANNOUNCE_IP"',
            line,
        )
    else:
        port_arg = re.compile(
            r'(?P<port>[ \\t]+--announce_port[ \\t]+(?:"\\$ANNOUNCE_PORT"|\\x27\\$ANNOUNCE_PORT\\x27|\\$ANNOUNCE_PORT))'
            r'(?=[ \\t]+--|[ \\t]*$)'
        )
        line, count = port_arg.subn(
            lambda match: match.group("port") + ' --announce_ip "$ANNOUNCE_IP"',
            line,
        )
    text = text[:matches[0].start()] + line + text[matches[0].end():]
else:
    raise SystemExit(1)
if count != 1:
    raise SystemExit(1)
Path(target).write_text(text, encoding="utf-8")
PYLAUNCH
  chmod 700 "$LAUNCH_TMP"

  assignment_guard_ready
  assignment_token_matches
  MOVED=1
  mv -f "$ENV_TMP" "$ENV_FILE"
  mv -f "$LAUNCH_TMP" "$LAUNCHER"
  tmux kill-session -t "$SESSION"
  assignment_stop_owned_servers
  tmux new-session -d -s "$SESSION" "$SUPERVISOR"

  for _ in $(seq 1 20); do
    TMUX_SESSION_AFTER="$(tmux display-message -p -t "$SESSION" '#{{session_id}}' 2>/dev/null || true)"
    PANE_PID_AFTER="$(tmux display-message -p -t "$SESSION" '#{{pane_pid}}' 2>/dev/null || true)"
    PROCESS_PID_AFTER="$(pgrep -P "$PANE_PID_AFTER" 2>/dev/null | head -n 1 || true)"
    if [ -n "$PANE_PID_AFTER" ] && [ -n "$PROCESS_PID_AFTER" ] && ps -p "$PANE_PID_AFTER" >/dev/null 2>&1 && ps -p "$PROCESS_PID_AFTER" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  [ -n "$PANE_PID_AFTER" ] && ps -p "$PANE_PID_AFTER" >/dev/null 2>&1 || fail "restarted agora_gpu pane is not alive"
  [ -n "$PROCESS_PID_AFTER" ] && ps -p "$PROCESS_PID_AFTER" >/dev/null 2>&1 || fail "restarted Agora child process is not alive"
  [ "$PANE_PID_AFTER" != "$PANE_PID_BEFORE" ] || fail "agora_gpu pane did not restart"
  [ "$PROCESS_PID_AFTER" != "$PROCESS_PID_BEFORE" ] || fail "Agora child process did not restart"
  IDENTITY_AFTER="$(sha256sum "$IDENTITY" | awk '{{print $1}}')"
  [ "$IDENTITY_AFTER" = "$IDENTITY_BEFORE" ] || fail "private_gpu0.key changed during repair"
  OTHER_SESSIONS_AFTER="$(tmux list-sessions -F '#{{session_id}}:#{{session_name}}:#{{session_created}}' 2>/dev/null | {{ grep -v ":$SESSION:" || true; }} | sha256sum | awk '{{print $1}}')"
  [ "$OTHER_SESSIONS_AFTER" = "$OTHER_SESSIONS_BEFORE" ] || fail "a non-Agora tmux session changed during repair"
  committed=1
  assignment_guard_release
  rm -f "$ENV_BACKUP" "$LAUNCH_BACKUP"
  trap - EXIT
fi

printf '__AGORA_ANNOUNCE_REPAIR__\\n'
printf 'mode=%s\\n' "$(if [ "$EXECUTE" = "1" ]; then printf execute; else printf preview; fi)"
printf 'before_announce_ip=%s\\n' "$BEFORE_IP"
printf 'before_announce_port=%s\\n' "$BEFORE_PORT"
printf 'after_announce_ip=%s\\n' "$REQUESTED_IP"
printf 'after_announce_port=%s\\n' "$REQUESTED_PORT"
printf 'identity_sha256_before=%s\\n' "$IDENTITY_BEFORE"
printf 'identity_sha256_after=%s\\n' "$IDENTITY_AFTER"
printf 'tmux_session_before=%s\\n' "$TMUX_SESSION_BEFORE"
printf 'tmux_session_after=%s\\n' "$TMUX_SESSION_AFTER"
printf 'pane_pid_before=%s\\n' "$PANE_PID_BEFORE"
printf 'pane_pid_after=%s\\n' "$PANE_PID_AFTER"
printf 'process_pid_before=%s\\n' "$PROCESS_PID_BEFORE"
printf 'process_pid_after=%s\\n' "$PROCESS_PID_AFTER"
printf 'other_sessions_before=%s\\n' "$OTHER_SESSIONS_BEFORE"
printf 'other_sessions_after=%s\\n' "$OTHER_SESSIONS_AFTER"
printf '__END_AGORA_ANNOUNCE_REPAIR__\\n'
"""

def remote_watchdog_script(
    machine: dict[str, Any],
    *,
    render: ScriptRenderers,
    training_source_root: str | None = None,
) -> str:
    remote_root = machine.get("remoteRoot") or render.default_remote_root
    assignment_context = _assignment_context(machine)
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={render.shell_quote(remote_root)}
mkdir -p "$ROOT/logs"
{_assignment_shell_contract(assignment_context, render=render, training_source_root=training_source_root)}
assignment_guard_ready
assignment_token_matches

cat > "$ROOT/watchdog-agora-tmux.sh" <<'CHECKEOF'
#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={render.shell_quote(remote_root)}
{_assignment_shell_contract(assignment_context, render=render, training_source_root=training_source_root)}
LOCKDIR="$ROOT/watchdog.lock"
assignment_guard_ready
assignment_token_matches
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  assignment_guard_release
  exit 0
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true; assignment_guard_release' EXIT
if ! tmux has-session -t agora_gpu >/dev/null 2>&1; then
  printf '%s event=watchdog_restart reason=missing_tmux_session\\n' "$(date -Iseconds)" >> "$ROOT/progress.log"
  assignment_assert_no_owned_servers
  tmux new-session -d -s agora_gpu "$ROOT/supervise-agora-gpu0.sh"
fi
rmdir "$LOCKDIR" 2>/dev/null || true
assignment_guard_release
CHECKEOF
chmod 700 "$ROOT/watchdog-agora-tmux.sh"

cat > "$ROOT/watch-agora-tmux-loop.sh" <<'LOOPEOF'
#!/usr/bin/env bash
set -Eeuo pipefail
ROOT={render.shell_quote(remote_root)}
WATCHDOG_BASE_SLEEP_SECONDS=3
WATCHDOG_JITTER_SECONDS=1
jittered_watchdog_sleep_seconds() {{
  awk -v base="$WATCHDOG_BASE_SLEEP_SECONDS" -v jitter="$WATCHDOG_JITTER_SECONDS" 'BEGIN {{ srand(); printf "%.3f", base + (jitter * rand()) }}'
}}
while true; do
  "$ROOT/watchdog-agora-tmux.sh" >> "$ROOT/logs/watchdog-check.log" 2>&1 || true
  sleep "$(jittered_watchdog_sleep_seconds)"
done
LOOPEOF
chmod 700 "$ROOT/watch-agora-tmux-loop.sh"

for pid in $(pgrep -f "$ROOT/watch-agora-tmux-loop.sh" 2>/dev/null || true); do
  if [ "$pid" != "$$" ]; then
    kill "$pid" 2>/dev/null || true
  fi
done
nohup "$ROOT/watch-agora-tmux-loop.sh" >> "$ROOT/watchdog.log" 2>&1 < /dev/null &

if command -v crontab >/dev/null 2>&1; then
  tmp_cron="$(mktemp)"
  crontab -l 2>/dev/null | grep -v 'watch-agora-tmux-loop.sh' > "$tmp_cron" || true
  printf '@reboot pgrep -f "%s/watch-agora-tmux-loop.sh" >/dev/null 2>&1 || nohup "%s/watch-agora-tmux-loop.sh" >> "%s/watchdog.log" 2>&1 < /dev/null &\\n' "$ROOT" "$ROOT" "$ROOT" >> "$tmp_cron"
  printf '* * * * * pgrep -f "%s/watch-agora-tmux-loop.sh" >/dev/null 2>&1 || nohup "%s/watch-agora-tmux-loop.sh" >> "%s/watchdog.log" 2>&1 < /dev/null &\\n' "$ROOT" "$ROOT" "$ROOT" >> "$tmp_cron"
  crontab "$tmp_cron"
  rm -f "$tmp_cron"
fi

if ! tmux has-session -t agora_gpu >/dev/null 2>&1; then
  printf '%s event=watchdog_restart reason=missing_tmux_session\\n' "$(date -Iseconds)" >> "$ROOT/progress.log"
  assignment_assert_no_owned_servers
  tmux new-session -d -s agora_gpu "$ROOT/supervise-agora-gpu0.sh"
fi
assignment_guard_release
printf '%s event=watchdog_installed\\n' "$(date -Iseconds)" >> "$ROOT/progress.log"
"""

def remote_status_script(machine: dict[str, Any], *, render: ScriptRenderers) -> str:
    root = machine.get("remoteRoot") or render.default_remote_root
    session = machine.get("tmuxSession") or "agora_gpu"
    return f"""#!/usr/bin/env bash
set +e
ROOT={render.shell_quote(root)}
SESSION={render.shell_quote(session)}
assignment_fail() {{ return 1; }}
{assignment_owned_server_process_shell().rstrip()}
echo "__AGORA_STATUS__"
printf 'token_label='
cat "$ROOT/token-label.txt" 2>/dev/null || true
printf '\\n'
printf 'machine_json='
cat "$ROOT/machine.json" 2>/dev/null || true
printf '\\n'
printf 'boot_id='
cat /proc/sys/kernel/random/boot_id 2>/dev/null || true
printf '\\n'
printf 'node_identity_json='
cat "$ROOT/agora-node-identity.json" 2>/dev/null || true
printf '\\n'
printf 'owned_server_identity='
assignment_current_owned_identity | awk '{{printf "%s/%s/%s/%s\\n", $1, $2, $3, $4}}'
printf 'remote_utc='
date -u +%Y-%m-%dT%H:%M:%S.%6NZ 2>/dev/null || true
printf 'uptime_seconds='
awk '{{print $1}}' /proc/uptime 2>/dev/null || true
printf 'clock_ticks_per_second='
getconf CLK_TCK 2>/dev/null || true
if tmux has-session -t agora_gpu >/dev/null 2>&1; then echo 'tmux_agora=yes'; else echo 'tmux_agora=no'; fi
if pgrep -f "$ROOT/watch-agora-tmux-loop.sh" >/dev/null 2>&1; then echo 'watchdog_loop=yes'; else echo 'watchdog_loop=no'; fi
echo "__PROGRESS_TAIL__"
tail -n 80 "$ROOT/progress.log" 2>/dev/null || true
echo "__AGORA_LOG_TAIL__"
tail -n 120 "$ROOT/logs/server_gpu0.log" 2>/dev/null || true
echo "__LAUNCHER_LOG_TAIL__"
tail -n 80 "$ROOT/logs/launcher-gpu0.log" 2>/dev/null || true
"""


def remote_runtime_identity_bootstrap_script(
    machine: dict[str, Any], *, execute: bool, render: ScriptRenderers
) -> str:
    """Render a bounded current-process identity recovery probe.

    Preview mode is byte-for-byte allowlistable and has no writes. Execute mode
    acquires the assignment fence only for final revalidation and the atomic
    public-artifact replacement.
    """

    root = machine.get("remoteRoot") or render.default_remote_root
    session = machine.get("tmuxSession") or "agora_gpu"
    provider = str(machine.get("provider") or "").strip().lower()
    account_scope = str(machine.get("accountScope") or "").strip()
    resource_id = str(
        machine.get("providerResourceId")
        or machine.get("runpodId")
        or machine.get("vastInstanceId")
        or machine.get("vastId")
        or ""
    ).strip()
    machine_generation_id = str(machine.get("machineGenerationId") or "").strip()
    if not machine_generation_id and provider and account_scope and resource_id:
        machine_generation_id = migration_ids(
            "agora-fleet", provider, account_scope, resource_id
        )["machineGenerationId"]
    identity_sha256 = str(
        machine.get("identityBackupSha256")
        or machine.get("identityBackupId")
        or ""
    ).strip().lower()
    assignment_guard = (
        _assignment_guard_for_machine(machine, render=render).rstrip()
        if execute
        else assignment_owned_server_process_shell().rstrip()
    )
    artifact_write = (
        r'''ARTIFACT_TMP="$(mktemp "$ROOT/.agora-node-identity.XXXXXX")" || emit_unavailable artifact_tempfile_failed
jq -cn \
  --arg machineId "$MACHINE_ID" --arg tokenLabel "$TOKEN_LABEL" \
  --arg nodeName "$NODE_NAME" --arg observedAt "$NODE_OBSERVED_AT" \
  --arg bootId "$BOOT_ID_BEFORE" --arg machineGenerationId "$MACHINE_GENERATION_ID" \
  --arg assignmentGeneration "$ASSIGNMENT_GENERATION" --arg assignmentOperationId "$ASSIGNMENT_OPERATION_ID" \
  --arg processPid "$SERVER_PID" --arg processPgid "$SERVER_PGID" \
  --arg processStartTicks "$SERVER_START_TICKS" --arg processKind "$SERVER_KIND" \
  '{version:1,machineId:$machineId,tokenLabel:$tokenLabel,nodeName:$nodeName,observedAt:$observedAt,bootId:$bootId,machineGenerationId:$machineGenerationId,assignmentGeneration:$assignmentGeneration,assignmentOperationId:$assignmentOperationId,process:{pid:$processPid,pgid:$processPgid,startTicks:$processStartTicks,kind:$processKind}}' \
  > "$ARTIFACT_TMP" || { rm -f "$ARTIFACT_TMP"; emit_unavailable artifact_render_failed; }
chmod 600 "$ARTIFACT_TMP" || { rm -f "$ARTIFACT_TMP"; emit_unavailable artifact_chmod_failed; }
mv -f "$ARTIFACT_TMP" "$NODE_IDENTITY_FILE" || { rm -f "$ARTIFACT_TMP"; emit_unavailable artifact_replace_failed; }
ARTIFACT_WRITTEN=true
assignment_guard_release'''
        if execute
        else "ARTIFACT_WRITTEN=false"
    )
    return f"""#!/usr/bin/env bash
set +e
ROOT={render.shell_quote(root)}
SESSION={render.shell_quote(session)}
MODE={render.shell_quote("execute" if execute else "preview")}
MACHINE_ID={render.shell_quote(machine.get("id"))}
TOKEN_LABEL={render.shell_quote(machine.get("tokenLabel"))}
EXPECTED_IDENTITY_SHA256={render.shell_quote(identity_sha256)}
MACHINE_GENERATION_ID={render.shell_quote(machine_generation_id)}
ASSIGNMENT_GENERATION={render.shell_quote(machine.get("assignmentGeneration"))}
ASSIGNMENT_OPERATION_ID={render.shell_quote(machine.get("assignmentOperationId"))}
NODE_IDENTITY_FILE="$ROOT/agora-node-identity.json"
MAX_HISTORY_FILES=8
MAX_BYTES_PER_FILE=8388608
{assignment_guard}

emit_unavailable() {{
  jq -cn --arg machineId "$MACHINE_ID" --arg reason "$1" \
    '{{machineId:$machineId,status:"unavailable",reason:$reason,artifactWritten:false}}'
  exit 0
}}

current_server_identity() {{
  assignment_current_owned_identity
}}

current_token_label() {{
  cat "$ROOT/token-label.txt" 2>/dev/null || true
}}

current_identity_sha256() {{
  sha256sum -- "$ROOT/private_gpu0.key" 2>/dev/null | awk '{{print $1}}'
}}

[ -n "$MACHINE_ID" ] || emit_unavailable machine_id_unavailable
[ -n "$TOKEN_LABEL" ] || emit_unavailable token_label_unavailable
[ -n "$MACHINE_GENERATION_ID" ] || emit_unavailable machine_generation_unavailable
case "$EXPECTED_IDENTITY_SHA256" in
  [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
  *) emit_unavailable verified_identity_backup_unavailable ;;
esac
[ "${{#EXPECTED_IDENTITY_SHA256}}" = 64 ] || emit_unavailable verified_identity_backup_unavailable

SERVER_IDENTITY_BEFORE="$(current_server_identity)" || emit_unavailable owned_server_not_unique
read -r SERVER_PID SERVER_PGID SERVER_START_TICKS SERVER_KIND <<EOF
$SERVER_IDENTITY_BEFORE
EOF
case "$SERVER_KIND" in server|cli) ;; *) emit_unavailable owned_server_not_unique ;; esac
BOOT_ID_BEFORE="$(cat "$ASSIGNMENT_PROC_ROOT/sys/kernel/random/boot_id" 2>/dev/null || true)"
[ -n "$BOOT_ID_BEFORE" ] || emit_unavailable boot_identity_unavailable
[ "$(current_token_label)" = "$TOKEN_LABEL" ] || emit_unavailable token_label_mismatch
IDENTITY_SHA256_BEFORE="$(current_identity_sha256)"
[ "$IDENTITY_SHA256_BEFORE" = "$EXPECTED_IDENTITY_SHA256" ] || emit_unavailable identity_backup_mismatch
assignment_owned_server_identity_matches "$SERVER_PID" "$SERVER_PGID" "$SERVER_START_TICKS" "$SERVER_KIND" || emit_unavailable owned_server_changed
PROCESS_PYTHON="$(readlink "$ASSIGNMENT_PROC_ROOT/$SERVER_PID/exe" 2>/dev/null || true)"
[ -n "$PROCESS_PYTHON" ] && [ -x "$PROCESS_PYTHON" ] || emit_unavailable python_runtime_unavailable
"$PROCESS_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info.major == 3 else 1)' >/dev/null 2>&1 || emit_unavailable python_runtime_unavailable
BOOT_EPOCH="$(awk '$1 == "btime" {{print $2; exit}}' "$ASSIGNMENT_PROC_ROOT/stat" 2>/dev/null)"
CLOCK_TICKS="$(getconf CLK_TCK 2>/dev/null || true)"
for numeric_value in "$BOOT_EPOCH" "$CLOCK_TICKS" "$SERVER_START_TICKS"; do
  case "$numeric_value" in ''|*[!0-9]*) emit_unavailable process_start_unavailable ;; esac
done
[ "$CLOCK_TICKS" -gt 0 ] || emit_unavailable process_start_unavailable
PROCESS_START_EPOCH="$(awk -v boot="$BOOT_EPOCH" -v ticks="$SERVER_START_TICKS" -v hz="$CLOCK_TICKS" 'BEGIN {{printf "%.6f", boot + (ticks / hz)}}')"

SCAN_RESULT="$("$PROCESS_PYTHON" - "$ROOT" "$PROCESS_START_EPOCH" "$MAX_HISTORY_FILES" "$MAX_BYTES_PER_FILE" <<'PY'
import datetime as dt
import glob
import gzip
import json
import os
import re
import sys

root, process_start_raw, max_files_raw, max_bytes_raw = sys.argv[1:]
process_start = float(process_start_raw)
max_files = int(max_files_raw)
max_bytes = int(max_bytes_raw)
node_pattern = re.compile(
    r"\\bNode\\s+name\\s*:\\s*((tail|body|head)-[0-9]+-[A-Za-z0-9._-]+-[0-9]+)\\b",
    re.IGNORECASE,
)
syslog_pattern = re.compile(
    r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\\s+(\\d{{1,2}})\\s+(\\d{{2}}):(\\d{{2}}):(\\d{{2}}(?:\\.\\d+)?)"
)
iso_pattern = re.compile(r"^(\\d{{4}}-\\d{{2}}-\\d{{2}}[T ]\\d{{2}}:\\d{{2}}:\\d{{2}}(?:\\.\\d+)?(?:Z|[+-]\\d{{2}}:\\d{{2}})?)")
months = {{name: index for index, name in enumerate(("Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"), 1)}}
now = dt.datetime.now().astimezone()

def timestamp(line):
    match = iso_pattern.match(line)
    if match:
        raw = match.group(1).replace("Z", "+00:00")
        try:
            value = dt.datetime.fromisoformat(raw)
            if value.tzinfo is None:
                value = value.replace(tzinfo=now.tzinfo)
            return value.timestamp()
        except ValueError:
            return None
    match = syslog_pattern.match(line)
    if not match:
        return None
    month, day, hour, minute, second = match.groups()
    seconds = float(second)
    candidates = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            value = dt.datetime(
                year, months[month], int(day), int(hour), int(minute),
                int(seconds), int((seconds % 1) * 1_000_000), tzinfo=now.tzinfo,
            )
            candidates.append(value)
        except ValueError:
            pass
    return min(candidates, key=lambda value: abs(value.timestamp() - now.timestamp())).timestamp() if candidates else None

paths = []
for pattern in ("logs/server_gpu0.log*", "logs/launcher-gpu0.log*"):
    paths.extend(glob.glob(os.path.join(root, pattern)))
paths = [path for path in set(paths) if os.path.isfile(path)]
paths.sort(key=lambda path: os.path.getmtime(path), reverse=True)
paths = paths[:max_files]
candidates = []
scanned_bytes = 0
for path in paths:
    try:
        if path.endswith(".gz"):
            with gzip.open(path, "rb") as handle:
                data = handle.read(max_bytes)
        else:
            size = os.path.getsize(path)
            with open(path, "rb") as handle:
                if size <= max_bytes:
                    data = handle.read(max_bytes)
                else:
                    half = max_bytes // 2
                    data = handle.read(half)
                    handle.seek(max(0, size - half))
                    data += b"\\n" + handle.read(half)
        scanned_bytes += len(data)
    except OSError:
        continue
    for raw_line in data.decode("utf-8", "replace").splitlines():
        match = node_pattern.search(raw_line)
        if not match:
            continue
        observed_at = timestamp(raw_line)
        if observed_at is None or observed_at < process_start or observed_at > now.timestamp() + 300:
            continue
        candidates.append((observed_at, match.group(1)))
names = sorted({{name for _, name in candidates}})
if not names:
    result = {{"status": "none", "files": len(paths), "bytes": scanned_bytes}}
elif len(names) != 1:
    result = {{"status": "conflict", "files": len(paths), "bytes": scanned_bytes, "identityCount": len(names)}}
else:
    latest = max(observed_at for observed_at, name in candidates if name == names[0])
    result = {{
        "status": "ready", "nodeName": names[0],
        "observedAt": dt.datetime.fromtimestamp(latest, dt.timezone.utc).isoformat(),
        "processStartedAt": dt.datetime.fromtimestamp(process_start, dt.timezone.utc).isoformat(),
        "files": len(paths), "bytes": scanned_bytes,
    }}
print(json.dumps(result, separators=(",", ":")))
PY
)" || emit_unavailable log_scan_failed
SCAN_STATUS="$(printf '%s' "$SCAN_RESULT" | jq -r '.status // empty' 2>/dev/null)"
case "$SCAN_STATUS" in
  none) emit_unavailable current_process_node_name_not_found ;;
  conflict) emit_unavailable conflicting_current_process_node_names ;;
  ready) ;;
  *) emit_unavailable invalid_log_scan_result ;;
esac
NODE_NAME="$(printf '%s' "$SCAN_RESULT" | jq -r '.nodeName // empty')"
NODE_OBSERVED_AT="$(printf '%s' "$SCAN_RESULT" | jq -r '.observedAt // empty')"
PROCESS_STARTED_AT="$(printf '%s' "$SCAN_RESULT" | jq -r '.processStartedAt // empty')"
FILES_SCANNED="$(printf '%s' "$SCAN_RESULT" | jq -r '.files // 0')"
BYTES_SCANNED="$(printf '%s' "$SCAN_RESULT" | jq -r '.bytes // 0')"
[ -n "$NODE_NAME" ] || emit_unavailable current_process_node_name_not_found

if [ "$MODE" = execute ]; then
  assignment_guard_ready
  assignment_token_matches
fi
SERVER_IDENTITY_AFTER="$(current_server_identity)" || emit_unavailable owned_server_changed
[ "$SERVER_IDENTITY_AFTER" = "$SERVER_IDENTITY_BEFORE" ] || emit_unavailable owned_server_changed
[ "$(cat "$ASSIGNMENT_PROC_ROOT/sys/kernel/random/boot_id" 2>/dev/null || true)" = "$BOOT_ID_BEFORE" ] || emit_unavailable boot_identity_changed
[ "$(current_token_label)" = "$TOKEN_LABEL" ] || emit_unavailable token_label_changed
[ "$(current_identity_sha256)" = "$IDENTITY_SHA256_BEFORE" ] || emit_unavailable remote_identity_changed

{artifact_write}

jq -cn \
  --arg machineId "$MACHINE_ID" --arg nodeName "$NODE_NAME" \
  --arg observedAt "$NODE_OBSERVED_AT" --arg processStartedAt "$PROCESS_STARTED_AT" \
  --arg processPid "$SERVER_PID" --arg processPgid "$SERVER_PGID" \
  --arg processStartTicks "$SERVER_START_TICKS" --arg processKind "$SERVER_KIND" --arg bootId "$BOOT_ID_BEFORE" \
  --argjson filesScanned "$FILES_SCANNED" --argjson bytesScanned "$BYTES_SCANNED" \
  --argjson artifactWritten "$ARTIFACT_WRITTEN" \
  '{{machineId:$machineId,status:"ready",nodeName:$nodeName,observedAt:$observedAt,processStartedAt:$processStartedAt,process:{{pid:$processPid,pgid:$processPgid,startTicks:$processStartTicks,kind:$processKind}},bootId:$bootId,proof:{{tokenLabelMatched:true,identityBackupMatched:true,processUnchanged:true}},coverage:{{filesScanned:$filesScanned,bytesScanned:$bytesScanned,maxFiles:8,maxBytesPerFile:8388608}},artifactWritten:$artifactWritten}}'
"""
