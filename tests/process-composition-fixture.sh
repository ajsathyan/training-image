#!/usr/bin/env bash
set -Eeuo pipefail

command_name="${1:-}"
case "$command_name" in
  start|restart|describe|stop) ;;
  *) printf 'usage: %s {start|restart|describe|stop}\n' "$0" >&2; exit 64 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PLATFORM="${PLATFORM:-linux/amd64}"
BASE_MODE="${BASE_MODE:-image}"
FIXTURE_ID="${FIXTURE_ID:-}"
OUTPUT_FILE="${OUTPUT_FILE:-}"
FIXTURE_TIMEOUT_SECONDS="${FIXTURE_TIMEOUT_SECONDS:-120}"
FIXTURE_STATE_ROOT="${FIXTURE_STATE_ROOT:-${TMPDIR:-/tmp}/agora-image-process-fixtures}"
BOOT_ENV_FILE="${BOOT_ENV_FILE:-}"
FAKE_TRAINING="${FAKE_TRAINING:-0}"
CLEANUP_TOKEN="${CLEANUP_TOKEN:-}"

fail() { printf 'process composition fixture: %s\n' "$*" >&2; exit 64; }
[[ "$PLATFORM" == "linux/amd64" ]] || fail 'PLATFORM must be linux/amd64'
[[ "$BASE_MODE" == image || "$BASE_MODE" == minimal ]] || fail 'BASE_MODE must be image or minimal'
[[ "$FIXTURE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]] || fail 'FIXTURE_ID is required and must be safe'
[[ "$OUTPUT_FILE" == /* ]] || fail 'OUTPUT_FILE must be an absolute path'
[[ "$FIXTURE_TIMEOUT_SECONDS" =~ ^[1-9][0-9]{0,3}$ ]] || fail 'FIXTURE_TIMEOUT_SECONDS must be 1..9999'
[[ "$FAKE_TRAINING" == 0 || "$FAKE_TRAINING" == 1 ]] || fail 'FAKE_TRAINING must be 0 or 1'

mkdir -p "$FIXTURE_STATE_ROOT"
chmod 700 "$FIXTURE_STATE_ROOT"
state_dir="$FIXTURE_STATE_ROOT/$FIXTURE_ID"
state_descriptor="$state_dir/descriptor.json"
token_path="$state_dir/cleanup-token"
case "$OUTPUT_FILE" in
  "$state_dir"|"$state_dir"/*) fail 'OUTPUT_FILE must be outside the disposable fixture state' ;;
esac

atomic_copy_descriptor() {
  local source="$1" output_parent temporary
  output_parent="$(dirname "$OUTPUT_FILE")"
  mkdir -p "$output_parent"
  temporary="$(mktemp "$output_parent/.process-fixture.XXXXXX")"
  chmod 600 "$temporary"
  cp "$source" "$temporary"
  mv -f "$temporary" "$OUTPUT_FILE"
  cat "$OUTPUT_FILE"
}

descriptor_field() {
  python3 - "$state_descriptor" "$1" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
for part in sys.argv[2].split("."):
    value = value[part]
print(value)
PY
}

validate_owned_container() {
  [[ -f "$state_descriptor" && ! -L "$state_descriptor" ]] || return 1
  [[ -f "$token_path" && ! -L "$token_path" ]] || return 1
  local expected_token container container_id label_id label_token token_hash actual_id
  expected_token="$(descriptor_field cleanup.token)"
  if [[ -n "$CLEANUP_TOKEN" && "$CLEANUP_TOKEN" != "$expected_token" ]]; then
    fail 'provided CLEANUP_TOKEN does not match descriptor'
  fi
  [[ "$(cat "$token_path")" == "$expected_token" ]] || fail 'cleanup token mismatch'
  container="$(descriptor_field containerName)"
  container_id="$(descriptor_field containerId)"
  docker inspect "$container" >/dev/null 2>&1 || return 2
  actual_id="$(docker inspect -f '{{.Id}}' "$container")"
  label_id="$(docker inspect -f '{{index .Config.Labels "io.agora.fixture.id"}}' "$container")"
  label_token="$(docker inspect -f '{{index .Config.Labels "io.agora.fixture.token-sha256"}}' "$container")"
  token_hash="$(printf '%s' "$expected_token" | sha256sum | awk '{print $1}')"
  [[ "$actual_id" == "$container_id" && "$label_id" == "$FIXTURE_ID" && "$label_token" == "$token_hash" ]] || \
    fail 'container ownership proof does not match descriptor'
}

if [[ "$command_name" == describe ]]; then
  validate_owned_container || fail 'fixture is not running'
  atomic_copy_descriptor "$state_descriptor"
  exit 0
fi

if [[ "$command_name" == stop ]]; then
  if [[ ! -e "$state_dir" ]]; then
    if [[ -f "$OUTPUT_FILE" ]] && python3 - "$OUTPUT_FILE" "$FIXTURE_ID" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
raise SystemExit(0 if value.get("fixtureId") == sys.argv[2] and value.get("status") == "stopped" else 1)
PY
    then
      cat "$OUTPUT_FILE"
      exit 0
    fi
    mkdir -p "$(dirname "$OUTPUT_FILE")"
    python3 - "$OUTPUT_FILE" "$FIXTURE_ID" <<'PY'
import datetime as dt, json, sys
value = {
    "schemaVersion": "agora.image-process-fixture.v1",
    "status": "stopped",
    "fixtureId": sys.argv[2],
    "stoppedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
}
with open(sys.argv[1], "w", encoding="utf-8") as stream:
    json.dump(value, stream, indent=2, sort_keys=True)
    stream.write("\n")
PY
    chmod 600 "$OUTPUT_FILE"
    cat "$OUTPUT_FILE"
    exit 0
  fi
  validation_rc=0
  validate_owned_container || validation_rc=$?
  if [[ "$validation_rc" == 0 ]]; then
    container="$(descriptor_field containerName)"
    docker rm -f "$container" >/dev/null
  elif [[ "$validation_rc" != 2 ]]; then
    fail 'fixture ownership could not be validated'
  fi
  stopped="$(mktemp "$(dirname "$OUTPUT_FILE")/.process-fixture-stopped.XXXXXX")"
  python3 - "$state_descriptor" "$stopped" <<'PY'
import datetime as dt, json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
value["status"] = "stopped"
value["stoppedAt"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
json.dump(value, open(sys.argv[2], "w", encoding="utf-8"), indent=2, sort_keys=True)
open(sys.argv[2], "a", encoding="utf-8").write("\n")
PY
  chmod 600 "$stopped"
  rm -rf -- "$state_dir"
  mv -f "$stopped" "$OUTPUT_FILE"
  cat "$OUTPUT_FILE"
  exit 0
fi

wait_until_ready() {
  local container="$1" port="$2" key="$3" known_hosts="$4" deadline="$5"
  while (( $(date +%s) < deadline )); do
    if ! docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null | grep -qx true; then
      docker logs "$container" >&2 || true
      return 1
    fi
    if ssh -i "$key" -o BatchMode=yes -o ConnectTimeout=2 \
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -p "$port" root@127.0.0.1 \
      'test -s /run/agora-image-bootstrap.status && pgrep -x sshd >/dev/null' \
      >/dev/null 2>&1; then
      ssh-keyscan -H -p "$port" 127.0.0.1 > "$known_hosts" 2>/dev/null
      chmod 600 "$known_hosts"
      return 0
    fi
    sleep 1
  done
  docker logs "$container" >&2 || true
  return 1
}

if [[ "$command_name" == restart ]]; then
  validate_owned_container || fail 'fixture is not running'
  container="$(descriptor_field containerName)"
  port="$(descriptor_field ssh.port)"
  key="$(descriptor_field ssh.privateKeyPath)"
  known_hosts="$(descriptor_field ssh.knownHostsPath)"
  deadline=$(( $(date +%s) + FIXTURE_TIMEOUT_SECONDS ))
  docker exec "$container" rm -f /run/agora-image-bootstrap.status
  docker restart --time 2 "$container" >/dev/null
  port="$(docker inspect -f '{{(index (index .NetworkSettings.Ports "22/tcp") 0).HostPort}}' "$container")"
  wait_until_ready "$container" "$port" "$key" "$known_hosts" "$deadline" || fail 'restart did not become ready before deadline'
  python3 - "$state_descriptor" "$deadline" "$port" <<'PY'
import datetime as dt, json, sys
path, deadline, port = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
value = json.load(open(path, encoding="utf-8"))
value["deadlineAt"] = dt.datetime.fromtimestamp(deadline, tz=dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
value["restartCount"] = int(value.get("restartCount", 0)) + 1
value["ssh"]["port"] = port
json.dump(value, open(path, "w", encoding="utf-8"), indent=2, sort_keys=True)
open(path, "a", encoding="utf-8").write("\n")
PY
  atomic_copy_descriptor "$state_descriptor"
  exit 0
fi

IMAGE="${IMAGE:-}"
if [[ "$BASE_MODE" == image && -z "$IMAGE" ]]; then fail 'IMAGE is required for image mode'; fi
[[ ! -e "$state_dir" ]] || fail 'fixture state already exists; use describe or stop'
if [[ -n "$BOOT_ENV_FILE" ]]; then
  [[ "$BOOT_ENV_FILE" == /* && -f "$BOOT_ENV_FILE" && ! -L "$BOOT_ENV_FILE" ]] || fail 'BOOT_ENV_FILE must be an absolute regular file'
fi
for required in docker jq python3 ssh ssh-keygen ssh-keyscan sha256sum; do
  command -v "$required" >/dev/null || fail "required command is unavailable: $required"
done

mkdir -m 700 "$state_dir"
start_succeeded=0
container=""
cleanup_failed_start() {
  rc="$1"
  if [[ "$start_succeeded" != 1 ]]; then
    [[ -z "$container" ]] || docker rm -f "$container" >/dev/null 2>&1 || true
    rm -rf -- "$state_dir"
  fi
  exit "$rc"
}
trap 'cleanup_failed_start "$?"' EXIT

cleanup_token="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
printf '%s\n' "$cleanup_token" > "$token_path"
chmod 600 "$token_path"
ssh-keygen -q -t ed25519 -N '' -f "$state_dir/id_ed25519"
chmod 600 "$state_dir/id_ed25519" "$state_dir/id_ed25519.pub"
touch "$state_dir/known_hosts"
chmod 600 "$state_dir/known_hosts"

if [[ "$BASE_MODE" == minimal ]]; then
  minimal_definition_hash="$(
    find "$REPO_ROOT/start.sh" "$REPO_ROOT/image-runtime" "$REPO_ROOT/machine-runtime" \
      "$SCRIPT_DIR/process-composition-minimal.Dockerfile" \
      "$SCRIPT_DIR/process-composition-training-source" -type f \
      ! -path '*/__pycache__/*' ! -name '*.pyc' -print0 \
      | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16
  )"
  IMAGE="agora-process-fixture-minimal:$minimal_definition_hash"
  if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker build --platform "$PLATFORM" \
      -f "$SCRIPT_DIR/process-composition-minimal.Dockerfile" \
      -t "$IMAGE" "$REPO_ROOT" >/dev/null
  fi
elif ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker pull --platform "$PLATFORM" "$IMAGE" >/dev/null
fi
read -r image_os image_arch <<<"$(docker image inspect -f '{{.Os}} {{.Architecture}}' "$IMAGE")"
[[ "$image_os/$image_arch" == "$PLATFORM" ]] || fail "IMAGE platform is $image_os/$image_arch, expected $PLATFORM"
if [[ "$BASE_MODE" == image ]]; then
  mkdir -m 700 "$state_dir/base-image-runtime"
  base_copy="$(docker create --platform "$PLATFORM" "$IMAGE")"
  docker cp "$base_copy:/opt/agora-image-runtime/." "$state_dir/base-image-runtime/"
  docker rm "$base_copy" >/dev/null
  python3 "$SCRIPT_DIR/prepare_process_composition_fixture.py" \
    --repo "$REPO_ROOT" \
    --base-image-runtime "$state_dir/base-image-runtime" \
    --output "$state_dir/image-runtime" \
    > "$state_dir/overlay-evidence.json"
  rm -rf -- "$state_dir/base-image-runtime"
  chmod 600 "$state_dir/overlay-evidence.json"
fi

short_token="${cleanup_token:0:12}"
container="agora-image-fixture-${FIXTURE_ID}-${short_token}"
provider_resource_id="fixture-${FIXTURE_ID}"
token_hash="$(printf '%s' "$cleanup_token" | sha256sum | awk '{print $1}')"
docker_args=(
  run -d --name "$container" --platform "$PLATFORM" --stop-timeout 2
  --label io.agora.fixture.kind=process-composition
  --label "io.agora.fixture.id=$FIXTURE_ID"
  --label "io.agora.fixture.token-sha256=$token_hash"
  -e "PUBLIC_KEY=$(cat "$state_dir/id_ed25519.pub")"
  -e "AGORA_BOOT_METADATA_WAIT_SECONDS=${AGORA_BOOT_METADATA_WAIT_SECONDS:-5}"
  -e "RUNPOD_POD_ID=$provider_resource_id"
  -e "RUNPOD_TCP_PORT_49200=49200"
  -p 127.0.0.1::22
)
if [[ "$BASE_MODE" == image ]]; then
  docker_args+=(
    --mount "type=bind,src=$REPO_ROOT/start.sh,dst=/start.sh,readonly"
    --mount "type=bind,src=$state_dir/image-runtime,dst=/opt/agora-image-runtime,readonly"
    --mount "type=bind,src=$REPO_ROOT/machine-runtime,dst=/opt/agora-machine-runtime,readonly"
  )
fi
if [[ -n "$BOOT_ENV_FILE" ]]; then docker_args+=(--env-file "$BOOT_ENV_FILE"); fi
docker_args+=("$IMAGE")
docker "${docker_args[@]}" >/dev/null
container_id="$(docker inspect -f '{{.Id}}' "$container")"
ssh_port="$(docker inspect -f '{{(index (index .NetworkSettings.Ports "22/tcp") 0).HostPort}}' "$container")"
deadline=$(( $(date +%s) + FIXTURE_TIMEOUT_SECONDS ))
wait_until_ready "$container" "$ssh_port" "$state_dir/id_ed25519" "$state_dir/known_hosts" "$deadline" || \
  fail 'container did not become ready before deadline'

scenario=neutral
[[ -z "$BOOT_ENV_FILE" ]] || scenario=boot-env
image_id="$(docker image inspect -f '{{.Id}}' "$IMAGE")"
image_repo_digest="$(docker image inspect -f '{{index .RepoDigests 0}}' "$IMAGE" 2>/dev/null || true)"
source_commit="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || printf unknown)"
source_tree="$(git -C "$REPO_ROOT" rev-parse HEAD^{tree} 2>/dev/null || printf unknown)"
boot_state="$(docker exec "$container" sh -c 'jq -r .state /run/agora-image-bootstrap.status.json 2>/dev/null || printf unknown')"
receipt_status="$(docker exec "$container" sh -c 'jq -r .status /workspace/agora-run/bootstrap-receipt.json 2>/dev/null || printf absent')"
tmux_sessions="$(docker exec "$container" sh -c 'tmux list-sessions -F "#{session_name}" 2>/dev/null || true')"
px0_listening=false
if docker exec "$container" /opt/agora-venv/bin/python -c \
  'import socket; client=socket.socket(); client.settimeout(0.2); raise SystemExit(client.connect_ex(("127.0.0.1", 7777)))' \
  >/dev/null 2>&1; then px0_listening=true; fi
sentinel_spool=false
if docker exec "$container" test -f /workspace/agora-run/machine-sentinel/events.jsonl; then sentinel_spool=true; fi

python3 - "$state_descriptor" "$FIXTURE_ID" "$scenario" "$container" \
  "$container_id" "$provider_resource_id" "$IMAGE" "$image_id" "$image_repo_digest" "$PLATFORM" \
  "$ssh_port" "$state_dir" "$px0_listening" "$boot_state" "$receipt_status" \
  "$tmux_sessions" "$sentinel_spool" "$source_commit" "$source_tree" \
  "$FAKE_TRAINING" "$cleanup_token" "$deadline" "$BASE_MODE" <<'PY'
import datetime as dt, json, sys
(
  output, fixture_id, scenario, container, container_id, provider_resource_id, image, image_id,
  image_repo_digest, platform, ssh_port, state_dir, px0_listening, boot_state,
  receipt_status, tmux_sessions, sentinel_spool, source_commit, source_tree,
  fake_training, cleanup_token, deadline, base_mode,
) = sys.argv[1:]
value = {
  "schemaVersion": "agora.image-process-fixture.v1",
  "status": "ready",
  "fixtureId": fixture_id,
  "scenario": scenario,
  "baseMode": base_mode,
  "containerName": container,
  "containerId": container_id,
  "providerResourceId": provider_resource_id,
  "image": image,
  "imageId": image_id,
  "imageRepoDigest": image_repo_digest,
  "platform": platform,
  "ssh": {
    "host": "127.0.0.1", "port": int(ssh_port), "user": "root",
    "privateKeyPath": state_dir + "/id_ed25519",
    "knownHostsPath": state_dir + "/known_hosts"
  },
  "remoteRoot": "/workspace/agora-run",
  "px0": {"remotePort": 7777, "listening": px0_listening == "true"},
  "observed": {
    "bootState": boot_state, "receiptStatus": receipt_status,
    "tmuxSessions": tmux_sessions.splitlines(), "sentinelSpoolPresent": sentinel_spool == "true"
  },
  "sources": {"imageRepo": {"commit": source_commit, "tree": source_tree}},
  "fakeTraining": base_mode == "minimal" or bool(int(fake_training)),
  "cleanup": {"token": cleanup_token, "path": state_dir},
  "deadlineAt": dt.datetime.fromtimestamp(int(deadline), tz=dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
  "restartCount": 0,
}
with open(output, "w", encoding="utf-8") as stream:
    json.dump(value, stream, indent=2, sort_keys=True)
    stream.write("\n")
PY
chmod 600 "$state_descriptor"
atomic_copy_descriptor "$state_descriptor"
start_succeeded=1
trap - EXIT
