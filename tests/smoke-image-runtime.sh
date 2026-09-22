#!/usr/bin/env bash
set -Eeuo pipefail

IMAGE="${IMAGE:?IMAGE is required}"
EVIDENCE="${SMOKE_EVIDENCE:-image-smoke-evidence.json}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=smoke-assertions.sh
source "$SCRIPT_DIR/smoke-assertions.sh"
work="$(mktemp -d)"
neutral="agora-image-neutral-$RANDOM"
configured="agora-image-configured-$RANDOM"
training="agora-image-training-fixture-$RANDOM"
invalid="agora-image-invalid-autostart-$RANDOM"
tunnel_pid=""
failure_root="${SMOKE_FAILURE_ROOT:-$REPO_ROOT/.smoke-failures}"
failed_line="unknown"
failed_command="unknown"

remember_failure() {
  failed_line="$1"
  failed_command="$2"
}
trap 'remember_failure "$LINENO" "$BASH_COMMAND"' ERR

capture_failure() {
  local name="$1"
  local destination="$2"
  if ! docker inspect "$name" >/dev/null 2>&1; then return 0; fi
  mkdir -p "$destination/$name"
  docker image inspect "$IMAGE" \
    --format '{"id":"{{.Id}}","repoDigests":{{json .RepoDigests}}}' \
    > "$destination/image-identity.json" || true
  docker logs "$name" 2>&1 \
    | sed -E 's/(hf_|heartbeat_fixture_|sentinel_fixture_)[A-Za-z0-9_.:-]+/[REDACTED]/g' \
    > "$destination/$name/container.log" || true
  docker exec "$name" sh -c '
    for path in /run/agora-image-bootstrap.status /run/agora-image-bootstrap.status.json \
      /workspace/agora-run/bootstrap-receipt.json; do
      if test -f "$path"; then printf "== %s ==\n" "$path"; cat "$path"; fi
    done
    printf "== processes ==\n"
    ps -eo pid=,ppid=,comm=,args= | grep -E "(sshd|cron|tmux|agora|python)" | head -80
    printf "== ports ==\n"
    ss -ltnp 2>/dev/null | head -80
    printf "== bounded runtime logs ==\n"
    for path in /var/log/agora-image-bootstrap.log \
      /workspace/agora-run/progress.log /workspace/agora-run/watchdog.log \
      /workspace/agora-run/logs/server_gpu0.log \
      /workspace/agora-run/logs/launcher-gpu0.log \
      /workspace/agora-run/logs/launcher-active.log; do
      if test -f "$path"; then printf "%s\n" "-- $path --"; tail -n 120 "$path"; fi
    done
  ' 2>&1 \
    | sed -E 's/(hf_|heartbeat_fixture_|sentinel_fixture_)[A-Za-z0-9_.:-]+/[REDACTED]/g' \
    > "$destination/$name/runtime.txt" || true
}

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    failure_dir="$failure_root/$(date -u +%Y%m%dT%H%M%SZ)-exit-$status"
    mkdir -p "$failure_dir"
    printf '%s\n' "$status" > "$failure_dir/original-exit-status"
    printf '%s\n' "$failed_line" > "$failure_dir/failed-line"
    printf '%s\n' "$failed_command" \
      | sed -E 's/(hf_|heartbeat_fixture_|sentinel_fixture_)[A-Za-z0-9_.:-]+/[REDACTED]/g' \
      > "$failure_dir/failed-command"
    for container in "$neutral" "$configured" "$training" "$invalid"; do
      capture_failure "$container" "$failure_dir"
    done
    printf 'redacted smoke evidence retained at %s\n' "$failure_dir" >&2
  fi
  if [ -n "$tunnel_pid" ]; then kill "$tunnel_pid" >/dev/null 2>&1 || true; fi
  # Runtime privacy leaves nested fixture directories root-owned and 0700.
  # Remove only the exact bind-mount contents as container root before the
  # containers disappear; every cleanup command is best-effort so it cannot
  # replace the original smoke result.
  docker exec "$configured" sh -c \
    'find /workspace/agora-run -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +' \
    >/dev/null 2>&1 || true
  docker exec "$training" sh -c \
    'find /workspace/agora-run /run/agora-inspection -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +' \
    >/dev/null 2>&1 || true
  docker rm -f "$neutral" "$configured" "$training" "$invalid" >/dev/null 2>&1 || true
  rm -rf "$work" >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

ssh-keygen -q -t ed25519 -N '' -f "$work/id_ed25519"
ssh_options=(
  -i "$work/id_ed25519"
  -o BatchMode=yes
  -o ConnectTimeout=2
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
)

pull_start="$(date +%s)"
image_was_local=false
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  image_was_local=true
else
  docker pull "$IMAGE" >/dev/null
fi
pull_finished="$(date +%s)"

host_port() {
  docker inspect -f '{{(index (index .NetworkSettings.Ports "22/tcp") 0).HostPort}}' "$1"
}

wait_for_ssh() {
  local port="$1"
  for _ in $(seq 1 60); do
    if ssh "${ssh_options[@]}" -p "$port" root@127.0.0.1 true 2>/dev/null; then return 0; fi
    sleep 1
  done
  return 1
}

wait_for_bootstrap() {
  local name="$1"
  for _ in $(seq 1 120); do
    if docker exec "$name" test -s /run/agora-image-bootstrap.status; then return 0; fi
    sleep 1
  done
  return 1
}

boot_start="$(date +%s)"
docker run -d --name "$neutral" \
  -e PUBLIC_KEY="$(cat "$work/id_ed25519.pub")" \
  -p 127.0.0.1::22 \
  "$IMAGE" >/dev/null
neutral_port="$(host_port "$neutral")"
wait_for_ssh "$neutral_port"
wait_for_bootstrap "$neutral"
neutral_ready="$(date +%s)"

ssh "${ssh_options[@]}" -p "$neutral_port" root@127.0.0.1 \
  'bash -s' < "$SCRIPT_DIR/assert-neutral-image-runtime.sh"

invalid_secret="hf_invalid_smoke_secret_must_not_escape"
docker run -d --name "$invalid" \
  -e PUBLIC_KEY="$(cat "$work/id_ed25519.pub")" \
  -e AGORA_BOOT_AUTOSTART=1 \
  -e AGORA_BOOT_LAUNCH_B64=not-base64 \
  -e AGORA_BOOT_HF_TOKEN="$invalid_secret" \
  -p 127.0.0.1::22 \
  "$IMAGE" >/dev/null
invalid_port="$(host_port "$invalid")"
wait_for_ssh "$invalid_port"
wait_for_bootstrap "$invalid"
docker exec "$invalid" jq -e \
  '.state == "invalid_input" and (.reason | contains("launch envelope"))' \
  /run/agora-image-bootstrap.status.json >/dev/null
assert_output_lacks_fixed_text "invalid-launch container logs exclude HF token" \
  "$invalid_secret" docker logs "$invalid"
invalid_sshd_pid="$(docker exec "$invalid" pgrep -xo sshd)"
assert_docker_file_lacks_fixed_text \
  "invalid-launch sshd environment excludes HF token" \
  "$invalid" "$invalid_secret" "/proc/$invalid_sshd_pid/environ"
assert_no_docker_tmux_session "invalid launch has no trainer session" \
  "$invalid" agora_gpu
capture_failure "$invalid" "$work/failure-capture-rehearsal"
test -s "$work/failure-capture-rehearsal/image-identity.json"
test -s "$work/failure-capture-rehearsal/$invalid/runtime.txt"
assert_no_fixed_text_recursive "failure evidence excludes invalid HF token" \
  "$invalid_secret" "$work/failure-capture-rehearsal"

state="$work/state"
mkdir -p "$state/controller-input"
chmod 700 "$state" "$state/controller-input"
printf '%s' 'hf_fixture_training_token_123' > "$state/controller-input/hf-token"
printf '%s' 'sentinel_fixture_machine_token_123' > "$state/controller-input/sentinel-machine-token"
chmod 600 "$state/controller-input/hf-token" "$state/controller-input/sentinel-machine-token"
token_hash="$(sha256sum "$state/controller-input/hf-token" | awk '{print $1}')"
declared_runtime_fingerprint="$(docker exec "$neutral" jq -r .runtimeExport.artifactFingerprint /opt/agora-image-runtime/capability.json)"
jq -n \
  --arg runtime_fingerprint "$declared_runtime_fingerprint" \
  '{
    id:"machine-smoke", provider:"runpod", accountScope:"account-smoke",
    providerResourceId:"pod-smoke", tokenLabel:"smoke-user", tokenInstance:3,
    assignmentGeneration:4, assignmentOperationId:"assignment-operation-smoke",
    launchId:"launch-smoke", reservationId:"reservation-smoke",
    slotId:"slot-smoke", slotGeneration:1,
    machineGenerationId:"machine-generation-smoke",
    runId:"run-smoke", gpuModel:"NVIDIA RTX PRO 6000 Blackwell Server Edition",
    agoraJoinRole:"tail", provisioningOrigin:"existing_rental",
    hostPort:49200, announcePort:55001, remoteRoot:"/workspace/agora-run",
    px0Enabled:true, fleetId:"fleet-smoke", authorityEpoch:5,
    imageCapability:{contractVersion:"agora.machine-image-capability.v1",runtimeArtifactFingerprint:$runtime_fingerprint}
  }' > "$work/configured-machine.json"
jq -n '{
  url:"https://127.0.0.1:9/api/machine-sentinel/observe",
  exportEnabled:true, fleetId:"fleet-smoke", authorityEpoch:5,
  machineToken:"sentinel_fixture_machine_token_123", timeoutSeconds:0.2
}' > "$work/configured-sentinel.json"
python3 "$REPO_ROOT/tests/generate_machine_image_config.py" config \
  --machine "$work/configured-machine.json" \
  --token-sha256 "$token_hash" \
  --output "$state/controller-input/machine-config.json" \
  --sentinel "$work/configured-sentinel.json" \
  --transition-kind stage \
  --allow-absent
config_hash="$(sha256sum "$state/controller-input/machine-config.json" | awk '{print $1}')"
printf '%s\n' 'permitted-inspection-marker' > "$state/progress.log"
active_root_state="$work/active-root-state"
mkdir -p "$active_root_state"
chmod 700 "$active_root_state"
python3 - "$state/controller-input/machine-config.json" \
  "$active_root_state/active-root.json" \
  "$REPO_ROOT/image-runtime/agora_boot_start.py" <<'PY'
import importlib.util
import json
import pathlib
import sys

config_path, pointer_path, boot_start_path = map(pathlib.Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location("smoke_agora_boot_start", boot_start_path)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot load production boot helper: {boot_start_path}")
boot_start = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boot_start)
config = json.loads(config_path.read_text(encoding="utf-8"))
identity_sha256 = boot_start._root_identity_digest(config)
pointer = {
    "schemaVersion": "agora.active-runtime-root.v1",
    "remoteRoot": config["remoteRoot"],
    "assignmentGeneration": config["assignmentGeneration"],
    "assignmentOperationId": config["assignmentOperationId"],
    "identitySha256": identity_sha256,
}
pointer_path.write_text(
    json.dumps(pointer, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
pointer_path.chmod(0o600)
PY

configured_start="$(date +%s)"
docker run -d --name "$configured" \
  -e PUBLIC_KEY="$(cat "$work/id_ed25519.pub")" \
  -e RUNPOD_POD_ID=pod-smoke \
  -e RUNPOD_TCP_PORT_49200=55001 \
  -v "$state:/workspace/agora-run" \
  -v "$active_root_state:/var/lib/agora" \
  -p 127.0.0.1::22 \
  "$IMAGE" >/dev/null
docker cp "$SCRIPT_DIR/smoke-assertions.sh" \
  "$configured:/tmp/smoke-assertions.sh"
configured_port="$(host_port "$configured")"
wait_for_ssh "$configured_port"
wait_for_bootstrap "$configured"
configured_ready="$(date +%s)"

ssh "${ssh_options[@]}" -p "$configured_port" root@127.0.0.1 '
  set -Eeuo pipefail
  source /tmp/smoke-assertions.sh
  trap '\''rc=$?; failed_line=$LINENO; failed_command=$BASH_COMMAND; set +e; \
    printf "configured smoke failed at line %s: %s\n" "$failed_line" "$failed_command" >&2; \
    jq -c "{status,training,optional,assignmentTransition,runtimeTrainingSource}" \
      /workspace/agora-run/bootstrap-receipt.json >&2; \
    tmux list-sessions -F "session=#{session_name} panes=#{session_panes}" >&2; \
    ss -ltnp | grep -E "(:7777|:22)[[:space:]]" >&2; \
    pgrep -af "px0|refresh_inspection|machine_sentinel" >&2; \
    tail -n 40 /var/log/agora-image-bootstrap.log \
      /workspace/agora-run/logs/px0.log \
      /workspace/agora-run/logs/machine-sentinel.log >&2; \
    exit "$rc"'\'' ERR
  root=/workspace/agora-run
  test "$(cat /run/agora-image-bootstrap.status)" = 0
  jq -e '\''
    .schemaVersion == "agora.machine-image-bootstrap-receipt.v1" and
    .status == "ready" and
    .machineId == "machine-smoke" and
    .assignmentOperationId == "assignment-operation-smoke" and
    .launchId == "launch-smoke" and
    .reservationId == "reservation-smoke" and
    .slotId == "slot-smoke" and
    .slotGeneration == 1 and
    .machineGenerationId == "machine-generation-smoke" and
    .training.requested == false and
    .training.status == "staged" and
    .optional.sentinel.status == "started" and
    .optional.inspection.status == "ready" and
    .optional.px0.status == "ready"
  '\'' /workspace/agora-run/bootstrap-receipt.json >/dev/null
  test "$(stat -c %a /workspace/agora-run)" = 700
  test "$(stat -c %a /workspace/agora-run/controller-input/machine-config.json)" = 600
  test "$(stat -c %a /workspace/agora-run/controller-input/hf-token)" = 600
  test "$(stat -c %a /workspace/agora-run/agora.env)" = 600
  test "$(stat -c %a /workspace/agora-run/bootstrap-receipt.json)" = 600
  test "$(stat -c %a /var/lib/agora)" = 700
  test "$(stat -c %a /var/lib/agora/active-root.json)" = 600
  jq -e '\''
    .schemaVersion == "agora.active-runtime-root.v1" and
    .remoteRoot == "/workspace/agora-run" and
    .assignmentGeneration == 4 and
    .assignmentOperationId == "assignment-operation-smoke"
  '\'' /var/lib/agora/active-root.json >/dev/null
  test -x /workspace/agora-run/repair-agora-client.sh
  test -x /workspace/agora-run/supervise-agora-gpu0.sh
  test -x /workspace/agora-run/install-watchdog.sh
  test ! -e /run/agora-inspection/agora.env
  test ! -e /run/agora-inspection/hf-token
  assert_output_empty "inspection export contains no symlinks" \
    find /run/agora-inspection -type l -print -quit
  # Bootstrap launches the inspection watcher and px0 asynchronously. Wait for
  # their exact postconditions instead of racing the first scheduler tick.
  for _ in $(seq 1 50); do
    if tmux has-session -t agora_px0 2>/dev/null \
      && tmux has-session -t agora_inspection 2>/dev/null \
      && pgrep -x px0 >/dev/null \
      && ss -ltn | grep -Eq '\''127\.0\.0\.1:7777[[:space:]]'\''; then
      break
    fi
    sleep 0.2
  done
  test "$(tmux list-sessions -F "#{session_name}" | grep -xc agora_sentinel)" = 1
  test "$(tmux list-sessions -F "#{session_name}" | grep -xc agora_px0)" = 1
  test "$(tmux list-sessions -F "#{session_name}" | grep -xc agora_inspection)" = 1
  assert_no_tmux_session "configured staged boot has no trainer" agora_gpu
  ss -ltn | grep -Eq '\''127\.0\.0\.1:7777[[:space:]]'\''
  assert_no_public_tcp_listener "px0 is not publicly exposed" 7777
  px0_pid="$(pgrep -x px0)"
  test "$(ps -o user= -p "$px0_pid" | xargs)" = agora-inspection
  tr '\''\0'\'' '\'' '\'' < "/proc/$px0_pid/cmdline" | grep -F -- "-no-agent" >/dev/null
  tr '\''\0'\'' '\'' '\'' < "/proc/$px0_pid/cmdline" | grep -F -- "-no-lsp" >/dev/null
  /opt/agora-venv/bin/python - <<'\''PY'\''
import pathlib
import agora, agora_server, pithos
root = pathlib.Path("/opt/agora-source").resolve()
for module in (pithos, agora_server, agora):
    pathlib.Path(module.__file__).resolve().relative_to(root)
PY
  # The image CAS must reject an exact-root orphan process while staging, even
  # when no tmux session represents it. The source file is restored before any
  # repair check, and the fixture makes no network or GPU request.
  server_path=/opt/agora-source/agora/src/agora/run_server.py
  cp -p "$server_path" /tmp/run_server.py.image-smoke-backup
  cat > "$server_path" <<'\''PY'\''
import time
while True:
    time.sleep(1)
PY
  (
    cd /opt/agora-source
    /opt/agora-venv/bin/python "$server_path" > /tmp/image-smoke-orphan.log 2>&1 &
    echo $! > "$root/orphan-owned-process.pid"
  )
  orphan_pid="$(cat "$root/orphan-owned-process.pid")"
  for _ in $(seq 1 20); do
    if kill -0 "$orphan_pid" 2>/dev/null; then break; fi
    sleep 0.1
  done
  kill -0 "$orphan_pid"
  mv /tmp/run_server.py.image-smoke-backup "$server_path"
  assignment_before="$(sha256sum "$root/assignment.json" | awk '\''{print $1}'\'')"
  orphan_bootstrap_rc=0
  if /opt/agora-venv/bin/python /opt/agora-image-runtime/agora_image_bootstrap.py \
    > /tmp/image-smoke-orphan-bootstrap.log 2>&1; then
    :
  else
    orphan_bootstrap_rc=$?
  fi
  test "$orphan_bootstrap_rc" = 70
  grep -Fq "owned Agora process is still running" /tmp/image-smoke-orphan-bootstrap.log
  test "$(sha256sum "$root/assignment.json" | awk '\''{print $1}'\'')" = "$assignment_before"
  kill "$orphan_pid"
  wait "$orphan_pid" 2>/dev/null || true
  rm -f "$root/orphan-owned-process.pid"
  /opt/agora-venv/bin/python /opt/agora-image-runtime/agora_image_bootstrap.py \
    > /tmp/image-smoke-orphan-retry.log 2>&1
  jq -e '\''.status == "ready" and .assignmentTransition.state == "staged"'\'' \
    "$root/bootstrap-receipt.json" >/dev/null
'

# Exercise stale-stage rejection, rollback, and lost-ACK replay using only the
# exported Fleet production builders for configs and manifests.
cp -p "$state/controller-input/machine-config.json" "$work/configured-stage-config.json"
jq \
  '.assignmentGeneration = 5 |
   .assignmentOperationId = "assignment-operation-newer" |
   .tokenLabel = "smoke-user-newer" |
   .tokenInstance = 4' \
  "$work/configured-machine.json" > "$work/configured-newer-machine.json"
python3 "$REPO_ROOT/tests/generate_machine_image_config.py" manifest \
  --machine "$work/configured-newer-machine.json" \
  --token-sha256 "$token_hash" \
  --state fenced \
  --output "$work/configured-newer-fence.json"
cp "$work/configured-newer-fence.json" "$state/.assignment.newer.json"
chmod 600 "$state/.assignment.newer.json"
mv "$state/.assignment.newer.json" "$state/assignment.json"
newer_fence_hash="$(docker exec "$configured" sha256sum /workspace/agora-run/assignment.json | awk '{print $1}')"
set +e
docker exec "$configured" /opt/agora-venv/bin/python \
  /opt/agora-image-runtime/agora_image_bootstrap.py \
  > "$work/image-smoke-stale-stage.log" 2>&1
stale_stage_rc=$?
set -e
test "$stale_stage_rc" = 70
grep -Fq "assignment manifest changed after controller precondition" \
  "$work/image-smoke-stale-stage.log"
test "$(docker exec "$configured" sha256sum /workspace/agora-run/assignment.json | awk '{print $1}')" = "$newer_fence_hash"

python3 "$REPO_ROOT/tests/generate_machine_image_config.py" config \
  --machine "$work/configured-machine.json" \
  --token-sha256 "$token_hash" \
  --output "$state/controller-input/machine-config.json" \
  --sentinel "$work/configured-sentinel.json" \
  --transition-kind rollback_prior \
  --expected-machine "$work/configured-newer-machine.json" \
  --expected-token-sha256 "$token_hash" \
  --expected-state fenced
docker exec "$configured" /opt/agora-venv/bin/python \
  /opt/agora-image-runtime/agora_image_bootstrap.py \
  > "$work/image-smoke-rollback.log" 2>&1
rollback_hash="$(docker exec "$configured" sha256sum /workspace/agora-run/assignment.json | awk '{print $1}')"
docker exec "$configured" jq -e '.status == "ready" and
  .assignmentTransition.kind == "rollback_prior" and
  .assignmentTransition.state == "fenced" and
  .assignmentTransition.rollbackAuthorized == true' \
  /workspace/agora-run/bootstrap-receipt.json >/dev/null
docker exec "$configured" rm /workspace/agora-run/bootstrap-receipt.json
docker exec "$configured" /opt/agora-venv/bin/python \
  /opt/agora-image-runtime/agora_image_bootstrap.py \
  > "$work/image-smoke-rollback-replay.log" 2>&1
test "$(docker exec "$configured" sha256sum /workspace/agora-run/assignment.json | awk '{print $1}')" = "$rollback_hash"
docker exec "$configured" jq -e '.status == "ready" and
  .assignmentTransition.kind == "rollback_prior" and
  .assignmentTransition.idempotent == true' \
  /workspace/agora-run/bootstrap-receipt.json >/dev/null
mv "$work/configured-stage-config.json" "$state/controller-input/machine-config.json"
docker exec "$configured" /opt/agora-venv/bin/python \
  /opt/agora-image-runtime/agora_image_bootstrap.py \
  > "$work/image-smoke-restage.log" 2>&1
docker exec "$configured" jq -e \
  '.status == "ready" and .assignmentTransition.state == "staged"' \
  /workspace/agora-run/bootstrap-receipt.json >/dev/null

ssh "${ssh_options[@]}" -p "$configured_port" root@127.0.0.1 '
  set -Eeuo pipefail
  # Exercise the installed supervisor exact outdated-client repair trigger
  # without joining Agora or touching a GPU.
  root=/workspace/agora-run
  cp "$root/launch-agora-gpu0.sh" "$root/launch-agora-gpu0.sh.smoke-backup"
  jq '\''.state = "ready"'\'' "$root/assignment.json" > "$root/.assignment.smoke"
  chmod 600 "$root/.assignment.smoke"
  mv "$root/.assignment.smoke" "$root/assignment.json"
  printf "%s\n" fixture-private-key > "$root/private_gpu0.key"
  chmod 600 "$root/private_gpu0.key"
  rm -rf /tmp/agora-repair-fixture
  mkdir -m 700 /tmp/agora-repair-fixture
  GIT_NO_LAZY_FETCH=1 git -C /opt/agora-source archive HEAD \
    | tar -x -C /tmp/agora-repair-fixture
  test -f /tmp/agora-repair-fixture/pithos/pyproject.toml
  test -f /tmp/agora-repair-fixture/agora_server/pyproject.toml
  test -f /tmp/agora-repair-fixture/agora/pyproject.toml
  git -C /tmp/agora-repair-fixture init -q
  git -C /tmp/agora-repair-fixture config user.name "Agora image smoke"
  git -C /tmp/agora-repair-fixture config user.email "image-smoke@example.invalid"
  git -C /tmp/agora-repair-fixture add -A
  git -C /tmp/agora-repair-fixture commit -q -m "test: offline repair base"
  printf "%s\n" "offline repair fixture" > /tmp/agora-repair-fixture/.agora-image-smoke-repair
  git -C /tmp/agora-repair-fixture add .agora-image-smoke-repair
  git -C /tmp/agora-repair-fixture commit -q -m "test: offline repair fixture"
  repair_target="$(git -C /tmp/agora-repair-fixture rev-parse HEAD)"
  cat > "$root/launch-agora-gpu0.sh" <<'\''SH'\''
#!/usr/bin/env bash
printf "%s\n" "Authorization failed: Please use the latest agora library. Make sure to pull the latest version from Github. Exiting run." >&2
sleep 3
exit 1
SH
  chmod 700 "$root/launch-agora-gpu0.sh"
  tmux new-session -d -s agora_gpu \
    -e "AGORA_REPAIR_REPO_URL=file:///tmp/agora-repair-fixture" \
    -e "GIT_NO_LAZY_FETCH=1" \
    "$root/supervise-agora-gpu0.sh"
  for _ in $(seq 1 20); do
    if test -f "$root/logs/launcher-active.log"; then break; fi
    sleep 0.25
  done
  test -f "$root/logs/launcher-active.log"
  test "$(stat -c %h "$root/logs/launcher-active.log")" -ge 2
  for _ in $(seq 1 180); do
    if grep -Fq "event=client_repair_finished action=update_client exit=0" "$root/progress.log"; then break; fi
    sleep 1
  done
  grep -Fq "event=client_repair_detected pattern=outdated_library action=update_client" "$root/progress.log"
  grep -Fq "event=client_repair_finished action=update_client exit=0" "$root/progress.log"
  grep -Fq "status=updated" "$root/logs/client-repair.log"
  grep -Fq "after_commit=$repair_target" "$root/logs/client-repair.log"
  test "$(git -C /opt/agora-source rev-parse HEAD)" = "$repair_target"
  for _ in $(seq 1 30); do
    if grep -Fq "event=client_repair_skipped action=update_client reason=supervisor_cooldown" "$root/progress.log"; then break; fi
    sleep 1
  done
  grep -Fq "event=client_repair_skipped action=update_client reason=supervisor_cooldown" "$root/progress.log"
  identity_before="$(awk -F= '\''$1 == "identity_sha256_before" {print $2}'\'' "$root/logs/client-repair.log" | tail -1)"
  identity_after="$(awk -F= '\''$1 == "identity_sha256_after" {print $2}'\'' "$root/logs/client-repair.log" | tail -1)"
  config_before="$(awk -F= '\''$1 == "config_sha256_before" {print $2}'\'' "$root/logs/client-repair.log" | tail -1)"
  config_after="$(awk -F= '\''$1 == "config_sha256_after" {print $2}'\'' "$root/logs/client-repair.log" | tail -1)"
  test -n "$identity_before"
  test "$identity_before" = "$identity_after"
  test -n "$config_before"
  test "$config_before" = "$config_after"
  tmux kill-session -t agora_gpu
  mv "$root/launch-agora-gpu0.sh.smoke-backup" "$root/launch-agora-gpu0.sh"
  jq '\''.state = "staged"'\'' "$root/assignment.json" > "$root/.assignment.smoke"
  chmod 600 "$root/.assignment.smoke"
  mv "$root/.assignment.smoke" "$root/assignment.json"
  rm -f "$root/private_gpu0.key"
  test "$(tmux list-sessions -F "#{session_name}" | grep -xc agora_sentinel)" = 1
  test "$(tmux list-sessions -F "#{session_name}" | grep -xc agora_px0)" = 1
'
repaired_source_commit="$(docker exec "$configured" git -C /opt/agora-source rev-parse HEAD)"
test "$repaired_source_commit" != "71a44b894100baa8f2996b97e73ae0bd67fa6b9d"

capability_hash="$(docker exec "$configured" sha256sum /opt/agora-image-runtime/capability.json | awk '{print $1}')"
runtime_fingerprint="$(docker exec "$configured" jq -r .runtimeExport.artifactFingerprint /opt/agora-image-runtime/capability.json)"
docker exec "$configured" jq -e \
  --arg config "$config_hash" \
  --arg capability "$capability_hash" \
  --arg runtime "$runtime_fingerprint" \
  '.configSha256 == $config and .capabilitySha256 == $capability and
   .runtimeExport.artifactFingerprint == $runtime and
   .trainingSource.commit == "71a44b894100baa8f2996b97e73ae0bd67fa6b9d"' \
  /workspace/agora-run/bootstrap-receipt.json >/dev/null

# A separate fresh container exercises the actual canonical start/watchdog/
# supervisor path with a bounded offline Python process. It makes no Agora,
# provider, GPU, or external network request.
training_state="$work/training-state"
bad_inspection="$work/bad-inspection"
mkdir -p "$training_state/controller-input" "$bad_inspection/unexpected-directory"
chmod 700 "$training_state" "$training_state/controller-input" "$bad_inspection"
printf '%s' 'hf_fixture_training_token_456' > "$training_state/controller-input/hf-token"
printf '%s\n' 'AGORA_HEARTBEAT_SECRET=heartbeat_fixture_secret_456' \
  > "$training_state/controller-input/heartbeat-machine-secret"
chmod 600 "$training_state/controller-input/hf-token" \
  "$training_state/controller-input/heartbeat-machine-secret"
training_token_hash="$(sha256sum "$training_state/controller-input/hf-token" | awk '{print $1}')"
jq -n \
  --arg runtime_fingerprint "$declared_runtime_fingerprint" \
  '{
    id:"machine-training-smoke", provider:"runpod", accountScope:"account-training-smoke",
    providerResourceId:"pod-training-smoke", tokenLabel:"training-smoke-user", tokenInstance:1,
    assignmentGeneration:1, assignmentOperationId:"assignment-operation-training-smoke",
    launchId:"launch-training-smoke", reservationId:"reservation-training-smoke",
    slotId:"slot-training-smoke", slotGeneration:1,
    machineGenerationId:"machine-generation-training-smoke",
    runId:"run-training-smoke", gpuModel:"NVIDIA RTX PRO 6000 Blackwell Server Edition",
    agoraJoinRole:"tail", provisioningOrigin:"existing_rental",
    hostPort:49200, announcePort:55001, remoteRoot:"/workspace/agora-run",
    px0Enabled:true, fleetId:"fleet-training-smoke", authorityEpoch:5,
    imageCapability:{contractVersion:"agora.machine-image-capability.v1",runtimeArtifactFingerprint:$runtime_fingerprint}
  }' > "$work/training-machine.json"
jq -n '{
  url:"https://127.0.0.1:9/api/machine-heartbeat",
  machineSecret:"heartbeat_fixture_secret_456",
  role:"tail", tokenLabel:"training-smoke-user",
  runpodPodId:"pod-training-smoke", runpodDcId:"offline-fixture-dc",
  intervalSeconds:1, jitterSeconds:0, timeoutSeconds:0.2
}' > "$work/training-heartbeat.json"
python3 "$REPO_ROOT/tests/generate_machine_image_config.py" config \
  --machine "$work/training-machine.json" \
  --token-sha256 "$training_token_hash" \
  --output "$training_state/controller-input/machine-config.json" \
  --start-training \
  --heartbeat "$work/training-heartbeat.json" \
  --transition-kind ready \
  --allow-absent
training_launch_b64="$(python3 - "$training_state/controller-input/machine-config.json" "$training_token_hash" <<'PY'
import base64
import json
import sys

config = json.load(open(sys.argv[1], encoding="utf-8"))
config.pop("providerResourceId", None)
config.pop("announcePort", None)
launch = {
    "schemaVersion": "agora.machine-boot-launch.v1",
    "tokenSha256": sys.argv[2],
    "config": config,
}
print(base64.b64encode(json.dumps(launch, sort_keys=True, separators=(",", ":")).encode()).decode())
PY
)"
rm "$training_state/controller-input/machine-config.json" \
  "$training_state/controller-input/hf-token"
cat > "$work/fake-agora-cli.py" <<'PY'
import signal
import time
from pathlib import Path

counter = Path("/workspace/agora-run/fake-launch-count")
identity = Path("/workspace/agora-run/private_gpu0.key")
if not identity.exists():
    identity.write_text("offline-fixture-private-identity\n", encoding="utf-8")
    identity.chmod(0o600)
attempt = int(counter.read_text() or "0") + 1 if counter.exists() else 1
counter.write_text(str(attempt), encoding="utf-8")
if attempt == 1:
    time.sleep(1)
    raise SystemExit(17)
stop = False
def halt(*_args):
    global stop
    stop = True
signal.signal(signal.SIGTERM, halt)
signal.signal(signal.SIGINT, halt)
while not stop:
    time.sleep(0.2)
PY
chmod 644 "$work/fake-agora-cli.py"

docker run -d --name "$training" \
  -e PUBLIC_KEY="$(cat "$work/id_ed25519.pub")" \
  -e AGORA_BOOT_AUTOSTART=1 \
  -e AGORA_BOOT_LAUNCH_B64="$training_launch_b64" \
  -e AGORA_BOOT_HF_TOKEN=hf_fixture_training_token_456 \
  -e AGORA_BOOT_METADATA_WAIT_SECONDS=30 \
  -v "$training_state:/workspace/agora-run" \
  -v "$bad_inspection:/run/agora-inspection" \
  -v "$work/fake-agora-cli.py:/opt/agora-source/agora_cli.py:ro" \
  -p 127.0.0.1::22 \
  "$IMAGE" >/dev/null
docker cp "$SCRIPT_DIR/smoke-assertions.sh" \
  "$training:/tmp/smoke-assertions.sh"
training_port="$(host_port "$training")"
wait_for_ssh "$training_port"
docker exec "$training" sh -c \
  'printf "%s\n" "RUNPOD_POD_ID=pod-training-smoke" "RUNPOD_TCP_PORT_49200=55001" > /etc/rp_environment'
wait_for_bootstrap "$training"
ssh "${ssh_options[@]}" -p "$training_port" root@127.0.0.1 '
  set -Eeuo pipefail
  source /tmp/smoke-assertions.sh
  root=/workspace/agora-run
  test "$(cat /run/agora-image-bootstrap.status)" = 0
  jq -e '\'' .state == "ready" and .selection == "launch" '\'' \
    /run/agora-image-bootstrap.status.json >/dev/null
  jq -e '\''
    .status == "ready" and .training.requested == true and .training.status == "started" and
    .heartbeat.requested == true and .heartbeat.status == "started" and
    .optional.sentinel.status == "started" and
    .optional.inspection.status == "unavailable" and
    .optional.px0.status == "blocked_by_inspection"
  '\'' "$root/bootstrap-receipt.json" >/dev/null
  test "$(tmux list-sessions -F "#{session_name}" | grep -xc agora_gpu)" = 1
  test "$(tmux list-sessions -F "#{session_name}" | grep -xc agora_heartbeat)" = 1
  for _ in $(seq 1 45); do
    if test -f "$root/fake-launch-count" && test "$(cat "$root/fake-launch-count")" -ge 2; then break; fi
    sleep 1
  done
  test "$(cat "$root/fake-launch-count")" -ge 2
  second_pid="$(ps -eo pid=,args= | awk '\''$2 == "/opt/agora-venv/bin/python" && $3 == "agora_cli.py" {print $1; exit}'\'')"
  test -n "$second_pid"
  kill -TERM "$second_pid"
  for _ in $(seq 1 45); do
    if test "$(cat "$root/fake-launch-count")" -ge 3; then break; fi
    sleep 1
  done
  test "$(cat "$root/fake-launch-count")" -ge 3
  third_pid="$(ps -eo pid=,args= | awk '\''$2 == "/opt/agora-venv/bin/python" && $3 == "agora_cli.py" {print $1; exit}'\'')"
  test -n "$third_pid"
  test "$third_pid" != "$second_pid"
  test "$(tmux list-sessions -F "#{session_name}" | grep -xc agora_gpu)" = 1
  test "$(tmux list-sessions -F "#{session_name}" | grep -xc agora_sentinel)" = 1
  assert_no_tmux_session "failed inspection prevents px0" agora_px0
  grep -Fq "attempt=1 exit=17" "$root/progress.log"
  assert_no_fixed_text "runtime evidence excludes HF token" \
    "hf_fixture_training_token_456" \
    /run/agora-image-bootstrap.status.json "$root/bootstrap-receipt.json" \
    /var/log/agora-image-bootstrap.log "$root/progress.log"
  for process_name in sshd cron; do
    process="$(pgrep -xo "$process_name")"
    test -n "$process"
    test -r "/proc/$process/environ"
    assert_no_fixed_text "$process_name environment excludes HF token" \
      "hf_fixture_training_token_456" "/proc/$process/environ"
  done
  runtime_processes="$(pgrep -f "agora_cli.py|agora_heartbeat_agent.py|agora_machine_sentinel_agent.py")"
  test -n "$runtime_processes"
  for process in $runtime_processes; do
    test -r "/proc/$process/environ"
    assert_no_fixed_text "runtime process environment excludes HF token" \
      "hf_fixture_training_token_456" "/proc/$process/environ"
    assert_no_fixed_text "runtime process environment excludes launch envelope" \
      "AGORA_BOOT_LAUNCH_B64=" "/proc/$process/environ"
  done
  printf "%s\n" "$third_pid" > "$root/fake-running-pid"
'
docker exec "$training" cat /workspace/agora-run/controller-input/machine-config.json \
  > "$work/training-ready-config.json"
python3 "$REPO_ROOT/tests/generate_machine_image_config.py" config \
  --machine "$work/training-machine.json" \
  --token-sha256 "$training_token_hash" \
  --output "$work/training-stage-config.json" \
  --heartbeat "$work/training-heartbeat.json" \
  --transition-kind stage \
  --expected-machine "$work/training-machine.json" \
  --expected-token-sha256 "$training_token_hash" \
  --expected-state ready
docker cp "$work/training-stage-config.json" \
  "$training:/workspace/agora-run/controller-input/.machine-config.stage"
docker exec "$training" sh -c \
  'chmod 600 /workspace/agora-run/controller-input/.machine-config.stage && mv /workspace/agora-run/controller-input/.machine-config.stage /workspace/agora-run/controller-input/machine-config.json'
docker exec "$training" /opt/agora-venv/bin/python \
  /opt/agora-image-runtime/agora_image_bootstrap.py \
  > "$work/image-smoke-ready-stage-replay.log" 2>&1
docker exec "$training" jq -e '.status == "ready" and
  .assignmentTransition.kind == "stage" and
  .assignmentTransition.state == "ready" and
  .assignmentTransition.idempotent == true and
  .training.status == "already_started"' \
  /workspace/agora-run/bootstrap-receipt.json >/dev/null
test "$(docker exec "$training" sh -c 'ps -eo pid=,args= | awk '\''$2 == "/opt/agora-venv/bin/python" && $3 == "agora_cli.py" {print $1; exit}'\''')" \
  = "$(docker exec "$training" cat /workspace/agora-run/fake-running-pid)"
docker cp "$work/training-ready-config.json" \
  "$training:/workspace/agora-run/controller-input/.machine-config.ready"
docker exec "$training" sh -c \
  'chmod 600 /workspace/agora-run/controller-input/.machine-config.ready && mv /workspace/agora-run/controller-input/.machine-config.ready /workspace/agora-run/controller-input/machine-config.json'
training_identity_hash="$(docker exec "$training" sha256sum /workspace/agora-run/private_gpu0.key | awk '{print $1}')"
for _ in $(seq 1 30); do
  if docker exec "$training" jq -e '.nextSeq >= 1' \
    /workspace/agora-run/heartbeat-agent/state.json >/dev/null 2>&1; then break; fi
  sleep 1
done
heartbeat_seq_before="$(docker exec "$training" jq -r .nextSeq /workspace/agora-run/heartbeat-agent/state.json)"
docker exec "$training" rm -f /run/agora-image-bootstrap.status
docker restart "$training" >/dev/null
training_port="$(host_port "$training")"
wait_for_ssh "$training_port"
wait_for_bootstrap "$training"
ssh "${ssh_options[@]}" -p "$training_port" root@127.0.0.1 '
  set -Eeuo pipefail
  root=/workspace/agora-run
  test "$(cat /run/agora-image-bootstrap.status)" = 0
  test "$(stat -c %a "$root/private_gpu0.key")" = 600
  test "$(stat -c %a "$root/private-identity.json")" = 600
  jq -e '\''
    .status == "ready" and .training.status == "started" and
    .heartbeat.status == "started" and
    .privateIdentity.status == "adopted_same_assignment"
  '\'' "$root/bootstrap-receipt.json" >/dev/null
  test "$(tmux list-sessions -F "#{session_name}" | grep -xc agora_gpu)" = 1
  test "$(tmux list-sessions -F "#{session_name}" | grep -xc agora_heartbeat)" = 1
'
test "$(docker exec "$training" sha256sum /workspace/agora-run/private_gpu0.key | awk '{print $1}')" = "$training_identity_hash"
for _ in $(seq 1 30); do
  heartbeat_seq_after="$(docker exec "$training" jq -r .nextSeq /workspace/agora-run/heartbeat-agent/state.json)"
  if test "$heartbeat_seq_after" -gt "$heartbeat_seq_before"; then break; fi
  sleep 1
done
test "$heartbeat_seq_after" -gt "$heartbeat_seq_before"

# A durable pause marker must outrank the saved ready config on reboot. SSH
# remains available, no owned trainer restarts, and configured observation does.
docker exec "$training" sh -c \
  'jq '\'' .desiredState = "paused" '\'' /workspace/agora-run/training-intent.json > /workspace/agora-run/.training-intent.paused && chmod 600 /workspace/agora-run/.training-intent.paused && mv /workspace/agora-run/.training-intent.paused /workspace/agora-run/training-intent.json'
heartbeat_seq_pause_before="$(docker exec "$training" jq -r .nextSeq /workspace/agora-run/heartbeat-agent/state.json)"
docker exec "$training" rm -f /run/agora-image-bootstrap.status
docker restart "$training" >/dev/null
training_port="$(host_port "$training")"
wait_for_ssh "$training_port"
wait_for_bootstrap "$training"
docker exec "$training" jq -e '.state == "stopped" and (.reason | contains("pause"))' \
  /run/agora-image-bootstrap.status.json >/dev/null
assert_no_docker_tmux_session "paused reboot has no trainer session" \
  "$training" agora_gpu
docker exec "$training" tmux has-session -t agora_heartbeat 2>/dev/null
for _ in $(seq 1 20); do
  heartbeat_seq_paused="$(docker exec "$training" jq -r .nextSeq /workspace/agora-run/heartbeat-agent/state.json)"
  if [ "$heartbeat_seq_paused" -gt "$heartbeat_seq_pause_before" ]; then break; fi
  sleep 1
done
test "$heartbeat_seq_paused" -gt "$heartbeat_seq_pause_before"

docker image inspect "$IMAGE" --format '{{json .Config.ExposedPorts}}' \
  | jq -e 'keys == ["22/tcp", "49200/tcp"]' >/dev/null
assert_output_lacks_ere "configured container environment excludes runtime secrets" \
  '^(HF_TOKEN|AGORA_SENTINEL_.*TOKEN|AGORA_HEARTBEAT_SECRET)=' \
  docker inspect "$configured" --format '{{range .Config.Env}}{{println .}}{{end}}'
assert_output_lacks_fixed_text "image history excludes configured HF token" \
  'hf_fixture_training_token_123' docker history --no-trunc "$IMAGE"
assert_output_lacks_fixed_text "image history excludes heartbeat secret" \
  'heartbeat_fixture_secret_456' docker history --no-trunc "$IMAGE"

tunnel_port="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
ssh "${ssh_options[@]}" -p "$configured_port" \
  -N -L "127.0.0.1:$tunnel_port:127.0.0.1:7777" root@127.0.0.1 &
tunnel_pid=$!
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$tunnel_port/api/meta" > "$work/px0-meta.json"; then break; fi
  sleep 1
done
jq -e '.name == "agora-inspection"' "$work/px0-meta.json" >/dev/null
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$tunnel_port/api/raw?path=progress.log" \
    > "$work/px0-progress.log" \
    && grep -Fq "permitted-inspection-marker" "$work/px0-progress.log"; then
    break
  fi
  sleep 1
done
grep -Fq "permitted-inspection-marker" "$work/px0-progress.log"
expected_progress_mtime="$(docker exec "$configured" /opt/agora-venv/bin/python -c \
  'import datetime as dt,pathlib; value=pathlib.Path("/workspace/agora-run/progress.log").stat().st_mtime_ns; print(dt.datetime.fromtimestamp(value / 1_000_000_000, tz=dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))')"
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$tunnel_port/api/raw?path=inspection-metadata.json" \
    > "$work/inspection-metadata.json" \
    && jq -e --arg expected "$expected_progress_mtime" \
      '.logs[] | select(.name == "progress.log" and .modifiedAt == $expected)' \
      "$work/inspection-metadata.json" >/dev/null; then
    break
  fi
  sleep 1
done
jq -e --arg expected "$expected_progress_mtime" \
  '.logs[] | select(.name == "progress.log" and .modifiedAt == $expected)' \
  "$work/inspection-metadata.json" >/dev/null
test "$(curl -sS -o "$work/traversal" -w '%{http_code}' "http://127.0.0.1:$tunnel_port/api/raw?path=../agora.env")" = 400
test "$(curl -sS -o "$work/absolute" -w '%{http_code}' "http://127.0.0.1:$tunnel_port/api/file?path=/workspace/agora-run/agora.env")" = 400
assert_no_fixed_text "px0 responses exclude configured HF token" \
  'hf_fixture_training_token_123' \
  "$work/px0-meta.json" "$work/traversal" "$work/absolute"
assert_no_fixed_text "px0 responses exclude Sentinel machine token" \
  'sentinel_fixture_machine_token_123' \
  "$work/px0-meta.json" "$work/traversal" "$work/absolute"
kill "$tunnel_pid"
tunnel_pid=""

for _ in $(seq 1 20); do
  if docker exec "$configured" test -s \
    /workspace/agora-run/machine-sentinel/events.jsonl; then break; fi
  sleep 1
done
docker exec "$configured" test -s /workspace/agora-run/machine-sentinel/events.jsonl
events_hash="$(docker exec "$configured" sha256sum /workspace/agora-run/machine-sentinel/events.jsonl | awk '{print $1}')"
docker exec "$configured" cat /workspace/agora-run/machine-sentinel/events.jsonl \
  > "$work/events-before-restart.jsonl"
receipt_hash="$(docker exec "$configured" sha256sum /workspace/agora-run/bootstrap-receipt.json | awk '{print $1}')"

docker exec "$configured" rm -f /run/agora-image-bootstrap.status
docker restart "$configured" >/dev/null
configured_port="$(host_port "$configured")"
wait_for_ssh "$configured_port"
wait_for_bootstrap "$configured"
ssh "${ssh_options[@]}" -p "$configured_port" root@127.0.0.1 '
  set -Eeuo pipefail
  source /tmp/smoke-assertions.sh
  test "$(cat /run/agora-image-bootstrap.status)" = 0
  jq -e '\''.state == "stopped" and (.reason | contains("assignment"))'\'' \
    /run/agora-image-bootstrap.status.json >/dev/null
  tmux has-session -t agora_sentinel 2>/dev/null
  tmux has-session -t agora_inspection 2>/dev/null
  tmux has-session -t agora_px0 2>/dev/null
  assert_no_tmux_session "stopped assignment has no trainer" agora_gpu
'
test "$(docker exec "$configured" git -C /opt/agora-source rev-parse HEAD)" = "$repaired_source_commit"
test "$(docker exec "$configured" sha256sum /workspace/agora-run/bootstrap-receipt.json | awk '{print $1}')" = "$receipt_hash"
docker exec "$configured" test -s /workspace/agora-run/machine-sentinel/events.jsonl
docker exec "$configured" jq -e \
  '.process.observedAt | type == "string" and length > 0' \
  /workspace/agora-run/machine-sentinel/state.json >/dev/null
process_observed_after_restart="$(docker exec "$configured" jq -r \
  '.process.observedAt' /workspace/agora-run/machine-sentinel/state.json)"
for _ in $(seq 1 20); do
  if docker exec "$configured" jq -e --arg before "$process_observed_after_restart" '
    .process.observedAt as $after |
    (($after | type) == "string") and $after > $before and
    .process.tmuxAgora == false
  ' /workspace/agora-run/machine-sentinel/state.json >/dev/null; then break; fi
  sleep 1
done
docker exec "$configured" jq -e --arg before "$process_observed_after_restart" '
  .process.observedAt as $after |
  (($after | type) == "string") and $after > $before and
  .process.tmuxAgora == false
' /workspace/agora-run/machine-sentinel/state.json >/dev/null
events_prefix_size="$(wc -c < "$work/events-before-restart.jsonl" | tr -d '[:space:]')"
docker exec "$configured" head -c "$events_prefix_size" \
  /workspace/agora-run/machine-sentinel/events.jsonl \
  > "$work/events-after-restart-prefix.jsonl"
cmp "$work/events-before-restart.jsonl" "$work/events-after-restart-prefix.jsonl"
test -n "$events_hash"
test -n "$receipt_hash"

image_id="$(docker image inspect "$IMAGE" --format '{{.Id}}')"
image_size="$(docker image inspect "$IMAGE" --format '{{.Size}}')"
jq -n \
  --arg image "$IMAGE" \
  --arg imageId "$image_id" \
  --argjson imageSizeBytes "$image_size" \
  --argjson imageWasLocal "$image_was_local" \
  --argjson pullSeconds "$((pull_finished - pull_start))" \
  --argjson neutralSshReadySeconds "$((neutral_ready - boot_start))" \
  --argjson configuredReadySeconds "$((configured_ready - configured_start))" \
  --arg runtimeEventsBeforeRestartSha256 "$events_hash" \
  '{schemaVersion:"agora.image-smoke-evidence.v1", image:$image, imageId:$imageId,
    imageSizeBytes:$imageSizeBytes, imageWasLocal:$imageWasLocal,
    timings:{pullSeconds:$pullSeconds, neutralSshReadySeconds:$neutralSshReadySeconds,
    configuredReadySeconds:$configuredReadySeconds}, checks:{ssh:true, neutralBoot:true,
    configuredBootstrap:true, offlineTrainingStartAndRestart:true,
    invalidOptInKeepsSsh:true,
    pauseOutranksSavedReady:true,
    generatedIdentityRestart:true, heartbeatStartedAndPersistent:true, knownRepair:true,
    optionalFailureIndependence:true, restart:true, sentinelOutageSpool:true,
    px0LoopbackTunnel:true, px0PermittedRead:true, px0SourceMtime:true,
    px0TraversalDenied:true, secretExcluded:true},
    runtimeEventsBeforeRestartSha256:$runtimeEventsBeforeRestartSha256}' \
  > "$EVIDENCE"

trap - EXIT
cleanup
