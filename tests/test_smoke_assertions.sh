#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=smoke-assertions.sh
source "$SCRIPT_DIR/smoke-assertions.sh"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin"

cat > "$work/bin/tmux" <<'SH'
#!/usr/bin/env bash
exit "${FAKE_TMUX_STATUS:?}"
SH
cat > "$work/bin/ss" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "${FAKE_SS_OUTPUT-}"
exit "${FAKE_SS_STATUS:-0}"
SH
cat > "$work/bin/docker" <<'SH'
#!/usr/bin/env bash
exit "${FAKE_DOCKER_STATUS:?}"
SH
chmod +x "$work/bin/tmux" "$work/bin/ss" "$work/bin/docker"
PATH="$work/bin:$PATH"
export PATH

(exit 0) &
success_one=$!
(exit 0) &
success_two=$!
wait_for_children_success "two successful replay children" \
  "$success_one" "$success_two"

(exit 75) &
failed_first=$!
(sleep 30) &
cancelled_second=$!
if wait_for_children_success "first replay child failure" \
  "$failed_first" "$cancelled_second" 2>/dev/null; then
  printf '%s\n' 'first replay child failure was accepted' >&2
  exit 1
fi
if kill -0 "$cancelled_second" 2>/dev/null; then
  printf '%s\n' 'sibling replay child leaked after first failure' >&2
  exit 1
fi

(exit 0) &
successful_first=$!
(exit 75) &
failed_second=$!
if wait_for_children_success "second replay child failure" \
  "$successful_first" "$failed_second" 2>/dev/null; then
  printf '%s\n' 'second replay child failure was accepted' >&2
  exit 1
fi

FAKE_TMUX_STATUS=0
export FAKE_TMUX_STATUS
(exit 75) &
masked_failure_one=$!
(exit 75) &
masked_failure_two=$!
if wait_for_children_success "replay failure with prior trainer" \
  "$masked_failure_one" "$masked_failure_two" 2>/dev/null \
  && tmux has-session -t agora_gpu; then
  printf '%s\n' 'prior trainer state masked replay child failure' >&2
  exit 1
fi
tmux has-session -t agora_gpu

FAKE_TMUX_STATUS=1
export FAKE_TMUX_STATUS
assert_no_tmux_session "legitimate absent session" agora_gpu

FAKE_TMUX_STATUS=0
if assert_no_tmux_session "unexpected session" agora_gpu 2>/dev/null; then
  printf '%s\n' 'unexpected session negative control did not fail' >&2
  exit 1
fi

FAKE_TMUX_STATUS=2
if assert_no_tmux_session "session inspection error" agora_gpu 2>/dev/null; then
  printf '%s\n' 'session inspection error was mistaken for absence' >&2
  exit 1
fi

FAKE_DOCKER_STATUS=41
export FAKE_DOCKER_STATUS
assert_no_docker_tmux_session "legitimate absent container session" fixture agora_gpu
FAKE_DOCKER_STATUS=40
export FAKE_DOCKER_STATUS
if assert_no_docker_tmux_session "unexpected container session" \
  fixture agora_gpu 2>/dev/null; then
  printf '%s\n' 'container session negative control did not fail' >&2
  exit 1
fi
FAKE_DOCKER_STATUS=1
export FAKE_DOCKER_STATUS
if assert_no_docker_tmux_session "docker transport error" \
  fixture agora_gpu 2>/dev/null; then
  printf '%s\n' 'docker transport error was mistaken for absence' >&2
  exit 1
fi

FAKE_SS_STATUS=0
FAKE_SS_OUTPUT='LISTEN 0 128 127.0.0.1:7777 0.0.0.0:*'
export FAKE_SS_STATUS FAKE_SS_OUTPUT
assert_no_public_tcp_listener "loopback-only listener" 7777

FAKE_SS_OUTPUT='LISTEN 0 128 0.0.0.0:7777 0.0.0.0:*'
export FAKE_SS_OUTPUT
if assert_no_public_tcp_listener "unexpected public listener" 7777 2>/dev/null; then
  printf '%s\n' 'public-listener negative control did not fail' >&2
  exit 1
fi

FAKE_SS_STATUS=2
FAKE_SS_OUTPUT=''
export FAKE_SS_STATUS FAKE_SS_OUTPUT
if assert_no_public_tcp_listener "listener inspection error" 7777 2>/dev/null; then
  printf '%s\n' 'listener inspection error was mistaken for absence' >&2
  exit 1
fi

printf '%s\n' 'public data' > "$work/clean"
assert_no_fixed_text "legitimate absent secret" fixture-secret "$work/clean"
printf '%s\n' 'fixture-secret' > "$work/leak"
if assert_no_fixed_text "found secret" fixture-secret "$work/leak" 2>/dev/null; then
  printf '%s\n' 'secret-leak negative control did not fail' >&2
  exit 1
fi
if assert_no_fixed_text "missing inspection target" fixture-secret \
  "$work/missing" 2>/dev/null; then
  printf '%s\n' 'file inspection error was mistaken for absence' >&2
  exit 1
fi
