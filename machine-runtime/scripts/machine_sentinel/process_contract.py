"""Exact read-only ownership proof for the assignment Agora process."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any


def assignment_owned_process_discovery_shell(
    *, training_source_root: str | None = None
) -> str:
    """Return exact-root process discovery without mutation helpers."""

    source_root = (
        '"$ROOT/agora-source"'
        if training_source_root is None
        else shlex.quote(training_source_root)
    )
    return r'''ASSIGNMENT_PROC_ROOT="${ASSIGNMENT_PROC_ROOT:-/proc}"
ASSIGNMENT_SOURCE_ROOT=''' + source_root + r'''
assignment_require_procfs() {
  [ -d "$ASSIGNMENT_PROC_ROOT" ] || assignment_fail "Linux procfs is required for exact Agora process proof"
}
assignment_proc_stat_identity() {
  local stat rest
  stat="$(cat "$1/stat" 2>/dev/null || true)"
  case "$stat" in *') '*) ;; *) return 1 ;; esac
  rest="${stat##*) }"
  set -- $rest
  [ "$#" -ge 20 ] || return 1
  printf '%s %s %s\n' "$1" "$3" "${20}"
}
assignment_python_executable() {
  local executable basename
  executable="$(readlink "$1/exe" 2>/dev/null || true)"
  basename="${executable##*/}"
  case "$basename" in python|python[0-9]*|pypy|pypy[0-9]*) return 0 ;; esac
  return 1
}
assignment_process_kind() {
  local proc="$1" cwd basename argument candidate="" module="" index=0 skip_value=0 expect_module=0
  [ -r "$proc/cmdline" ] || return 1
  cwd="$(readlink "$proc/cwd" 2>/dev/null || true)"
  case "$cwd" in
    "$ASSIGNMENT_SOURCE_ROOT"|"$ASSIGNMENT_SOURCE_ROOT/"*)
      while IFS= read -r -d '' argument; do
        index=$((index + 1))
        if [ "$index" = 1 ]; then
          basename="${argument##*/}"
          case "$basename" in python|python[0-9]*|pypy|pypy[0-9]*) continue ;; *) return 1 ;; esac
        fi
        if [ "$skip_value" = 1 ]; then
          skip_value=0
          continue
        fi
        if [ "$expect_module" = 1 ]; then
          module="$argument"
          break
        fi
        case "$argument" in
          -c) return 1 ;;
          -m) expect_module=1; continue ;;
          -W|-X|--check-hash-based-pycs) skip_value=1; continue ;;
          -W*|-X*|--check-hash-based-pycs=*) continue ;;
          --) continue ;;
          -*) continue ;;
          *) candidate="$argument"; break ;;
        esac
      done < "$proc/cmdline"
      ;;
  esac
  if [ -n "$module" ]; then
    [ "$module" = "agora.run_server" ] || return 1
    printf server
    return 0
  fi
  [ -n "$candidate" ] || return 1
  while case "$candidate" in ./*) true ;; *) false ;; esac; do
    candidate="${candidate#./}"
  done
  case "$candidate" in
    ../*|*/../*|*/..|*/./*|*/.|*//*) return 1 ;;
  esac
  case "$candidate" in
    /*) ;;
    *) candidate="$cwd/${candidate#./}" ;;
  esac
  [ "$candidate" = "$ASSIGNMENT_SOURCE_ROOT/agora/src/agora/run_server.py" ] && { printf server; return 0; }
  [ "$candidate" = "$ASSIGNMENT_SOURCE_ROOT/agora_cli.py" ] && { printf cli; return 0; }
  return 1
}
assignment_owned_server_inventory() {
  local proc pid identity state pgid started kind
  assignment_require_procfs
  for proc in "$ASSIGNMENT_PROC_ROOT"/[0-9]*; do
    pid="${proc##*/}"
    kill -0 "$pid" 2>/dev/null || continue
    kind="$(assignment_process_kind "$proc")" || continue
    identity="$(assignment_proc_stat_identity "$proc")" || continue
    read -r state pgid started <<EOF
$identity
EOF
    [ "$state" != Z ] || continue
    case "$(ps -p "$pid" -o state= 2>/dev/null | tr -d '[:space:]')" in Z*) continue ;; esac
    case "$pid:$pgid:$started" in *[!0-9:]*|:*|*:|*::*) continue ;; esac
    printf '%s %s %s %s\n' "$pid" "$pgid" "$started" "$kind"
  done
}
assignment_tmux_owned_runtime_identity() {
  local pane_pid pane_identity pane_state pane_pgid pane_started
  local proc pid identity state pgid started kind count=0 row=""
  assignment_require_procfs
  pane_pid="$(tmux display-message -p -t "${SESSION:-agora_gpu}" '#{pane_pid}' 2>/dev/null || true)"
  case "$pane_pid" in ''|*[!0-9]*) return 1 ;; esac
  pane_identity="$(assignment_proc_stat_identity "$ASSIGNMENT_PROC_ROOT/$pane_pid")" || return 1
  read -r pane_state pane_pgid pane_started <<EOF
$pane_identity
EOF
  for proc in "$ASSIGNMENT_PROC_ROOT"/[0-9]*; do
    pid="${proc##*/}"
    [ "$pid" != "$pane_pid" ] || continue
    kill -0 "$pid" 2>/dev/null || continue
    identity="$(assignment_proc_stat_identity "$proc")" || continue
    read -r state pgid started <<EOF
$identity
EOF
    [ "$state" != Z ] || continue
    [ "$pgid" = "$pane_pgid" ] || continue
    kind="$(assignment_process_kind "$proc")" || continue
    count=$((count + 1))
    row="$pid $pgid $started $kind"
  done
  [ "$count" = 1 ] || return 1
  printf '%s\n' "$row"
}
assignment_current_owned_identity() {
  local exact
  exact="$(assignment_owned_server_inventory | awk '
    { total += 1; last=$0 }
    $4 == "server" { servers += 1; server=$0 }
    END {
      if (servers == 1) print server
      else if (servers == 0 && total == 1) print last
      else exit 1
    }
  ')" && { printf '%s\n' "$exact"; return 0; }
  assignment_tmux_owned_runtime_identity
}
'''


def owned_process_probe_command(root: Path, pane_pid: int) -> list[str]:
    """Build a read-only exact-root descendant probe for one tmux pane."""

    if pane_pid < 1:
        raise ValueError("tmux pane PID must be positive")
    script = f"""
set -euo pipefail
ROOT={shlex.quote(str(root))}
assignment_fail() {{ return 1; }}
{assignment_owned_process_discovery_shell()}
pane_pid={pane_pid}
assignment_descends_from_pane() {{
  local pid="$1" stat rest parent depth=0
  while [ "$depth" -lt 128 ]; do
    [ "$pid" = "$pane_pid" ] && return 0
    stat="$(cat "$ASSIGNMENT_PROC_ROOT/$pid/stat" 2>/dev/null || true)"
    case "$stat" in *') '*) ;; *) return 1 ;; esac
    rest="${{stat##*) }}"
    set -- $rest
    [ "$#" -ge 20 ] || return 1
    parent="$2"
    case "$parent" in ''|*[!0-9]*) return 1 ;; esac
    [ "$parent" -gt 1 ] || return 1
    pid="$parent"
    depth=$((depth + 1))
  done
  return 1
}}
owned_rows="$(assignment_owned_server_inventory | while read -r pid pgid started kind; do
  assignment_python_executable "$ASSIGNMENT_PROC_ROOT/$pid" || continue
  assignment_descends_from_pane "$pid" || continue
  printf '%s %s %s %s\n' "$pid" "$pgid" "$started" "$kind"
done)"
exact="$(printf '%s\n' "$owned_rows" | awk '
  NF == 4 {{ total += 1; last=$0 }}
  NF == 4 && $4 == "server" {{ servers += 1; server=$0 }}
  END {{
    if (servers == 1) print server
    else if (servers == 0 && total == 1) print last
    else exit 1
  }}
')"
[ -n "$exact" ] || exit 1
printf '%s\n' "$exact"
"""
    return ["bash", "-c", script]


def parse_owned_process_identity(output: str) -> dict[str, Any] | None:
    """Parse the single exact process identity produced by the probe."""

    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    fields = lines[0].split()
    if len(fields) != 4 or fields[3] not in {"server", "cli"}:
        return None
    if any(not value.isdigit() or int(value) < 1 for value in fields[:3]):
        return None
    return {
        "pid": int(fields[0]),
        "processGroupId": int(fields[1]),
        "startedTicks": int(fields[2]),
        "kind": fields[3],
    }
