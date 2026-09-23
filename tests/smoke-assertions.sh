#!/usr/bin/env bash

# Shared rejecting assertions for image smoke tests. A negative observation is
# proof only when the inspection command succeeds and the forbidden value is
# absent; inspection errors must fail instead of being mistaken for absence.

wait_for_children_success() {
  local label="$1"
  shift
  local pids=("$@")
  local first_failure=0
  local pid other status attempt

  if [ "${#pids[@]}" -eq 0 ]; then
    printf 'no child processes supplied: %s\n' "$label" >&2
    return 2
  fi

  for pid in "${pids[@]}"; do
    if wait "$pid"; then
      status=0
    else
      status=$?
    fi
    if [ "$status" -eq 0 ]; then
      continue
    fi
    if [ "$first_failure" -eq 0 ]; then
      first_failure="$status"
      printf 'required child failed: %s (pid %s, status %s)\n' \
        "$label" "$pid" "$status" >&2
      for other in "${pids[@]}"; do
        if [ "$other" = "$pid" ] || ! kill -0 "$other" 2>/dev/null; then
          continue
        fi
        kill -TERM "$other" 2>/dev/null || true
        for attempt in 1 2 3 4 5 6 7 8 9 10; do
          kill -0 "$other" 2>/dev/null || break
          sleep 0.05
        done
        if kill -0 "$other" 2>/dev/null; then
          kill -KILL "$other" 2>/dev/null || true
        fi
      done
    fi
  done

  if [ "$first_failure" -ne 0 ]; then
    return "$first_failure"
  fi
}

assert_child_exited() {
  local label="$1"
  local pid="$2"
  if kill -0 "$pid" 2>/dev/null; then
    printf 'child still running before retry: %s (pid %s)\n' "$label" "$pid" >&2
    return 1
  fi
}

terminate_and_reap_child() {
  local label="$1"
  local pid="$2"
  local child_status=0 watchdog_status=0 watchdog_pid

  if ! kill -TERM "$pid" 2>/dev/null; then
    printf 'could not terminate child: %s (pid %s)\n' "$label" "$pid" >&2
    return 1
  fi
  (
    sleep 2
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || ! kill -0 "$pid" 2>/dev/null
    fi
  ) &
  watchdog_pid=$!

  if wait "$pid" 2>/dev/null; then
    child_status=0
  else
    child_status=$?
  fi
  if kill -0 "$watchdog_pid" 2>/dev/null; then
    kill -TERM "$watchdog_pid" 2>/dev/null || true
  fi
  if wait "$watchdog_pid" 2>/dev/null; then
    watchdog_status=0
  else
    watchdog_status=$?
  fi
  case "$watchdog_status" in
    0|143) ;;
    *)
      printf 'child cleanup watchdog failed: %s (status %s)\n' \
        "$label" "$watchdog_status" >&2
      return "$watchdog_status"
      ;;
  esac
  case "$child_status" in
    137|143) ;;
    *)
      printf 'child did not exit from bounded cleanup: %s (status %s)\n' \
        "$label" "$child_status" >&2
      return 1
      ;;
  esac
  assert_child_exited "$label" "$pid"
}

assert_absent_status() {
  local label="$1"
  shift
  local status
  if "$@" >/dev/null 2>&1; then
    printf 'forbidden state present: %s\n' "$label" >&2
    return 1
  else
    status=$?
  fi
  if [ "$status" -eq 1 ]; then
    return 0
  fi
  printf 'could not inspect forbidden state: %s (status %s)\n' \
    "$label" "$status" >&2
  return "$status"
}

assert_no_tmux_session() {
  local label="$1"
  local session="$2"
  assert_absent_status "$label" tmux has-session -t "$session"
}

assert_no_docker_tmux_session() {
  local label="$1"
  local container="$2"
  local session="$3"
  local status
  if docker exec "$container" sh -c '
    tmux has-session -t "$1" >/dev/null 2>&1
    status=$?
    case "$status" in
      0) exit 40 ;;
      1) exit 41 ;;
      *) exit 42 ;;
    esac
  ' sh "$session"; then
    status=0
  else
    status=$?
  fi
  case "$status" in
    41) return 0 ;;
    40)
      printf 'forbidden state present: %s\n' "$label" >&2
      return 1
      ;;
    42)
      printf 'container inspection failed: %s\n' "$label" >&2
      return 1
      ;;
    *)
      printf 'docker transport failed while inspecting: %s (status %s)\n' \
        "$label" "$status" >&2
      return 1
      ;;
  esac
}

assert_docker_file_lacks_fixed_text() {
  local label="$1"
  local container="$2"
  local needle="$3"
  local path="$4"
  local status
  if docker exec "$container" sh -c '
    grep -aFq -- "$1" "$2"
    status=$?
    case "$status" in
      0) exit 40 ;;
      1) exit 41 ;;
      *) exit 42 ;;
    esac
  ' sh "$needle" "$path"; then
    status=0
  else
    status=$?
  fi
  case "$status" in
    41) return 0 ;;
    40)
      printf 'forbidden text present: %s\n' "$label" >&2
      return 1
      ;;
    42)
      printf 'container file inspection failed: %s\n' "$label" >&2
      return 1
      ;;
    *)
      printf 'docker transport failed while inspecting: %s (status %s)\n' \
        "$label" "$status" >&2
      return 1
      ;;
  esac
}

assert_output_lacks_ere() {
  local label="$1"
  local pattern="$2"
  shift 2
  local output status
  output="$(mktemp)"
  if "$@" >"$output" 2>&1; then
    :
  else
    status=$?
    rm -f "$output"
    printf 'could not inspect forbidden output: %s (status %s)\n' \
      "$label" "$status" >&2
    return "${status:-1}"
  fi
  if grep -Eq -- "$pattern" "$output"; then
    rm -f "$output"
    printf 'forbidden output present: %s\n' "$label" >&2
    return 1
  else
    status=$?
  fi
  rm -f "$output"
  if [ "$status" -eq 1 ]; then
    return 0
  fi
  printf 'could not search inspected output: %s (status %s)\n' \
    "$label" "$status" >&2
  return "$status"
}

assert_output_lacks_fixed_text() {
  local label="$1"
  local needle="$2"
  shift 2
  local output status
  output="$(mktemp)"
  if "$@" >"$output" 2>&1; then
    :
  else
    status=$?
    rm -f "$output"
    printf 'could not inspect forbidden output: %s (status %s)\n' \
      "$label" "$status" >&2
    return "${status:-1}"
  fi
  if grep -aFq -- "$needle" "$output"; then
    rm -f "$output"
    printf 'forbidden output present: %s\n' "$label" >&2
    return 1
  else
    status=$?
  fi
  rm -f "$output"
  if [ "$status" -eq 1 ]; then
    return 0
  fi
  printf 'could not search inspected output: %s (status %s)\n' \
    "$label" "$status" >&2
  return "$status"
}

assert_output_empty() {
  local label="$1"
  shift
  local output status
  output="$(mktemp)"
  if "$@" >"$output" 2>&1; then
    :
  else
    status=$?
    rm -f "$output"
    printf 'could not inspect output: %s (status %s)\n' "$label" "$status" >&2
    return "${status:-1}"
  fi
  if [ -s "$output" ]; then
    rm -f "$output"
    printf 'forbidden output present: %s\n' "$label" >&2
    return 1
  fi
  rm -f "$output"
}

assert_no_fixed_text() {
  local label="$1"
  local needle="$2"
  shift 2
  local status
  if grep -aFq -- "$needle" "$@"; then
    printf 'forbidden text present: %s\n' "$label" >&2
    return 1
  else
    status=$?
  fi
  if [ "$status" -eq 1 ]; then
    return 0
  fi
  printf 'could not inspect files for forbidden text: %s (status %s)\n' \
    "$label" "$status" >&2
  return "$status"
}

assert_no_fixed_text_recursive() {
  local label="$1"
  local needle="$2"
  shift 2
  local status
  if grep -R -aFq -- "$needle" "$@"; then
    printf 'forbidden text present: %s\n' "$label" >&2
    return 1
  else
    status=$?
  fi
  if [ "$status" -eq 1 ]; then
    return 0
  fi
  printf 'could not inspect tree for forbidden text: %s (status %s)\n' \
    "$label" "$status" >&2
  return "$status"
}

assert_no_public_tcp_listener() {
  local label="$1"
  local port="$2"
  assert_output_lacks_ere "$label" \
    "(^|[[:space:]])(0\\.0\\.0\\.0|\\[::\\]|\\*):${port}[[:space:]]" \
    ss -ltn
}
