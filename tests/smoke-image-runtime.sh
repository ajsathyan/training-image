#!/usr/bin/env bash
set -Eeuo pipefail

IMAGE="${IMAGE:?IMAGE is required}"
EVIDENCE="${SMOKE_EVIDENCE:-image-smoke-evidence.json}"
work="$(mktemp -d)"
neutral="agora-image-neutral-$RANDOM"
configured="agora-image-configured-$RANDOM"
training="agora-image-training-fixture-$RANDOM"
tunnel_pid=""

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    docker logs "$neutral" 2>/dev/null || true
    docker logs "$configured" 2>/dev/null || true
  fi
  if [ -n "$tunnel_pid" ]; then kill "$tunnel_pid" >/dev/null 2>&1 || true; fi
  docker rm -f "$neutral" "$configured" "$training" >/dev/null 2>&1 || true
  rm -rf "$work"
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

ssh "${ssh_options[@]}" -p "$neutral_port" root@127.0.0.1 '
  set -Eeuo pipefail
  test "$(cat /run/agora-image-bootstrap.status)" = 0
  grep -q "no machine configuration" /var/log/agora-image-bootstrap.log
  ! tmux has-session -t agora_gpu 2>/dev/null
  ! tmux has-session -t agora_sentinel 2>/dev/null
  ! tmux has-session -t agora_px0 2>/dev/null
  test ! -e /workspace/agora-run/bootstrap-receipt.json
  pgrep -x sshd >/dev/null
  pgrep -x cron >/dev/null
'

state="$work/state"
mkdir -p "$state/controller-input"
chmod 700 "$state" "$state/controller-input"
printf '%s' 'hf_fixture_training_token_123' > "$state/controller-input/hf-token"
printf '%s' 'sentinel_fixture_machine_token_123' > "$state/controller-input/sentinel-machine-token"
chmod 600 "$state/controller-input/hf-token" "$state/controller-input/sentinel-machine-token"
token_hash="$(sha256sum "$state/controller-input/hf-token" | awk '{print $1}')"
declared_runtime_fingerprint="$(docker exec "$neutral" jq -r .runtimeExport.artifactFingerprint /opt/agora-image-runtime/capability.json)"
jq -n \
  --arg token_hash "$token_hash" \
  --arg runtime_fingerprint "$declared_runtime_fingerprint" \
  '{
    schemaVersion:"agora.machine-image-config.v1",
    machineId:"machine-smoke", provider:"runpod", accountScope:"account-smoke",
    providerResourceId:"pod-smoke", tokenLabel:"smoke-user", tokenInstance:3,
    assignmentGeneration:4, assignmentOperationId:"assignment-operation-smoke",
    trainingSessionId:"assignment-operation-smoke", runId:"run-smoke",
    gpuModel:"NVIDIA RTX PRO 6000 Blackwell Server Edition", nodeType:"tail",
    provisioningOrigin:"existing_rental", hostPort:49200, announcePort:55001,
    remoteRoot:"/workspace/agora-run", tokenSha256:$token_hash,
    imageCapability:{contractVersion:"agora.machine-image-capability.v1",runtimeArtifactFingerprint:$runtime_fingerprint},
    startTraining:false, px0Enabled:true,
    assignmentTransition:{kind:"stage",allowAbsent:true,expectedManifest:null},
    sentinel:{
      mode:"remote", url:"https://127.0.0.1:9/api/machine-sentinel/observe",
      fleetId:"fleet-smoke", authorityEpoch:5,
      machineTokenFile:"/workspace/agora-run/controller-input/sentinel-machine-token",
      timeoutSeconds:0.2
    },
    heartbeat:{mode:"disabled",url:""}
  }' > "$state/controller-input/machine-config.json"
chmod 600 "$state/controller-input/machine-config.json"
config_hash="$(sha256sum "$state/controller-input/machine-config.json" | awk '{print $1}')"
printf '%s\n' 'permitted-inspection-marker' > "$state/progress.log"

configured_start="$(date +%s)"
docker run -d --name "$configured" \
  -e PUBLIC_KEY="$(cat "$work/id_ed25519.pub")" \
  -v "$state:/workspace/agora-run" \
  -p 127.0.0.1::22 \
  "$IMAGE" >/dev/null
configured_port="$(host_port "$configured")"
wait_for_ssh "$configured_port"
wait_for_bootstrap "$configured"
configured_ready="$(date +%s)"

ssh "${ssh_options[@]}" -p "$configured_port" root@127.0.0.1 '
  set -Eeuo pipefail
  test "$(cat /run/agora-image-bootstrap.status)" = 0
  jq -e '\''
    .schemaVersion == "agora.machine-image-bootstrap-receipt.v1" and
    .status == "ready" and
    .machineId == "machine-smoke" and
    .assignmentOperationId == "assignment-operation-smoke" and
    .training.requested == false and
    .training.status == "staged" and
    .optional.inspection.status == "ready" and
    .optional.px0.status == "ready"
  '\'' /workspace/agora-run/bootstrap-receipt.json >/dev/null
  test "$(stat -c %a /workspace/agora-run)" = 700
  test "$(stat -c %a /workspace/agora-run/controller-input/machine-config.json)" = 600
  test "$(stat -c %a /workspace/agora-run/controller-input/hf-token)" = 600
  test "$(stat -c %a /workspace/agora-run/agora.env)" = 600
  test "$(stat -c %a /workspace/agora-run/bootstrap-receipt.json)" = 600
  test -x /workspace/agora-run/repair-agora-client.sh
  test -x /workspace/agora-run/supervise-agora-gpu0.sh
  test -x /workspace/agora-run/install-watchdog.sh
  test ! -e /run/agora-inspection/agora.env
  test ! -e /run/agora-inspection/hf-token
  ! find /run/agora-inspection -type l -print -quit | grep -q .
  test "$(tmux list-sessions -F "#{session_name}" | grep -xc agora_sentinel)" = 1
  test "$(tmux list-sessions -F "#{session_name}" | grep -xc agora_px0)" = 1
  test "$(tmux list-sessions -F "#{session_name}" | grep -xc agora_inspection)" = 1
  ! tmux has-session -t agora_gpu 2>/dev/null
  ss -ltn | grep -Eq '\''127\.0\.0\.1:7777[[:space:]]'\''
  ! ss -ltn | grep -Eq '\''(0\.0\.0\.0|\[::\]):7777[[:space:]]'\''
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
  set +e
  /opt/agora-venv/bin/python /opt/agora-image-runtime/agora_image_bootstrap.py \
    > /tmp/image-smoke-orphan-bootstrap.log 2>&1
  orphan_bootstrap_rc=$?
  set -e
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
  # Exercise the installed supervisor's exact outdated-client repair trigger
  # without joining Agora or touching a GPU.
  root=/workspace/agora-run
  cp "$root/launch-agora-gpu0.sh" "$root/launch-agora-gpu0.sh.smoke-backup"
  jq '\''.state = "ready"'\'' "$root/assignment.json" > "$root/.assignment.smoke"
  chmod 600 "$root/.assignment.smoke"
  mv "$root/.assignment.smoke" "$root/assignment.json"
  printf "%s\n" fixture-private-key > "$root/private_gpu0.key"
  chmod 600 "$root/private_gpu0.key"
  rm -rf /tmp/agora-repair-fixture
  git clone -q /opt/agora-source /tmp/agora-repair-fixture
  git -C /tmp/agora-repair-fixture config user.name "Agora image smoke"
  git -C /tmp/agora-repair-fixture config user.email "image-smoke@example.invalid"
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
jq -e \
  --arg config "$config_hash" \
  --arg capability "$capability_hash" \
  --arg runtime "$runtime_fingerprint" \
  '.configSha256 == $config and .capabilitySha256 == $capability and
   .runtimeExport.artifactFingerprint == $runtime and
   .trainingSource.commit == "71a44b894100baa8f2996b97e73ae0bd67fa6b9d"' \
  "$state/bootstrap-receipt.json" >/dev/null

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
  --arg token_hash "$training_token_hash" \
  --arg runtime_fingerprint "$declared_runtime_fingerprint" \
  '{
    schemaVersion:"agora.machine-image-config.v1",
    machineId:"machine-training-smoke", provider:"runpod", accountScope:"account-training-smoke",
    providerResourceId:"pod-training-smoke", tokenLabel:"training-smoke-user", tokenInstance:1,
    assignmentGeneration:1, assignmentOperationId:"assignment-operation-training-smoke",
    runId:"run-training-smoke", gpuModel:"NVIDIA RTX PRO 6000 Blackwell Server Edition",
    nodeType:"tail", provisioningOrigin:"existing_rental",
    hostPort:49200, announcePort:55001, remoteRoot:"/workspace/agora-run",
    tokenSha256:$token_hash, startTraining:true, px0Enabled:true,
    assignmentTransition:{kind:"ready",allowAbsent:true,expectedManifest:null},
    imageCapability:{contractVersion:"agora.machine-image-capability.v1",runtimeArtifactFingerprint:$runtime_fingerprint},
    sentinel:{
      mode:"local", url:"", timeoutSeconds:"invalid-fixture"
    },
    heartbeat:{
      mode:"configured", url:"https://127.0.0.1:9/api/machine-heartbeat",
      secretFile:"/workspace/agora-run/controller-input/heartbeat-machine-secret",
      role:"tail", tokenLabel:"training-smoke-user",
      runpodPodId:"pod-training-smoke", runpodDcId:"offline-fixture-dc",
      intervalSeconds:1, jitterSeconds:0, timeoutSeconds:0.2
    }
  }' > "$training_state/controller-input/machine-config.json"
chmod 600 "$training_state/controller-input/machine-config.json"
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
  -v "$training_state:/workspace/agora-run" \
  -v "$bad_inspection:/run/agora-inspection" \
  -v "$work/fake-agora-cli.py:/opt/agora-source/agora_cli.py:ro" \
  -p 127.0.0.1::22 \
  "$IMAGE" >/dev/null
training_port="$(host_port "$training")"
wait_for_ssh "$training_port"
wait_for_bootstrap "$training"
ssh "${ssh_options[@]}" -p "$training_port" root@127.0.0.1 '
  set -Eeuo pipefail
  root=/workspace/agora-run
  test "$(cat /run/agora-image-bootstrap.status)" = 0
  jq -e '\''
    .status == "ready" and .training.requested == true and .training.status == "started" and
    .heartbeat.requested == true and .heartbeat.status == "started" and
    .optional.sentinel.status == "unavailable" and
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
  ! tmux has-session -t agora_sentinel 2>/dev/null
  ! tmux has-session -t agora_px0 2>/dev/null
  grep -Fq "attempt=1 exit=17" "$root/progress.log"
  cp -p "$root/controller-input/machine-config.json" /tmp/machine-config.ready-backup.json
  jq --slurpfile current "$root/assignment.json" \
    '\''.startTraining = false |
      .assignmentTransition = {kind:"stage",allowAbsent:false,expectedManifest:$current[0]}'\'' \
    /tmp/machine-config.ready-backup.json > "$root/controller-input/.machine-config.stage-replay.json"
  chmod 600 "$root/controller-input/.machine-config.stage-replay.json"
  mv "$root/controller-input/.machine-config.stage-replay.json" "$root/controller-input/machine-config.json"
  /opt/agora-venv/bin/python /opt/agora-image-runtime/agora_image_bootstrap.py \
    > /tmp/image-smoke-ready-stage-replay.log 2>&1
  jq -e '\''.status == "ready" and
    .assignmentTransition.kind == "stage" and
    .assignmentTransition.state == "ready" and
    .assignmentTransition.idempotent == true and
    .training.status == "already_started"'\'' "$root/bootstrap-receipt.json" >/dev/null
  test "$(ps -eo pid=,args= | awk '\''$2 == "/opt/agora-venv/bin/python" && $3 == "agora_cli.py" {print $1; exit}'\'')" = "$third_pid"
  mv /tmp/machine-config.ready-backup.json "$root/controller-input/machine-config.json"
'
training_identity_hash="$(sha256sum "$training_state/private_gpu0.key" | awk '{print $1}')"
for _ in $(seq 1 30); do
  if jq -e '.nextSeq >= 1' "$training_state/heartbeat-agent/state.json" >/dev/null 2>&1; then break; fi
  sleep 1
done
heartbeat_seq_before="$(jq -r .nextSeq "$training_state/heartbeat-agent/state.json")"
docker exec "$training" rm -f /run/agora-image-bootstrap.status
docker restart "$training" >/dev/null
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
test "$(sha256sum "$training_state/private_gpu0.key" | awk '{print $1}')" = "$training_identity_hash"
for _ in $(seq 1 30); do
  heartbeat_seq_after="$(jq -r .nextSeq "$training_state/heartbeat-agent/state.json")"
  if test "$heartbeat_seq_after" -gt "$heartbeat_seq_before"; then break; fi
  sleep 1
done
test "$heartbeat_seq_after" -gt "$heartbeat_seq_before"

docker image inspect "$IMAGE" --format '{{json .Config.ExposedPorts}}' \
  | jq -e 'keys == ["22/tcp", "49200/tcp"]' >/dev/null
test -z "$(docker inspect "$configured" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E '^(HF_TOKEN|AGORA_SENTINEL_.*TOKEN|AGORA_HEARTBEAT_SECRET)=' || true)"
! docker history --no-trunc "$IMAGE" | grep -F 'hf_fixture_training_token_123' >/dev/null
! docker history --no-trunc "$IMAGE" | grep -F 'heartbeat_fixture_secret_456' >/dev/null

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
jq -e '.name == "inspection"' "$work/px0-meta.json" >/dev/null
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
! grep -F 'hf_fixture_training_token_123' "$work/px0-meta.json" "$work/traversal" "$work/absolute"
! grep -F 'sentinel_fixture_machine_token_123' "$work/px0-meta.json" "$work/traversal" "$work/absolute"
kill "$tunnel_pid"
tunnel_pid=""

for _ in $(seq 1 20); do
  if test -s "$state/machine-sentinel/events.jsonl"; then break; fi
  sleep 1
done
test -s "$state/machine-sentinel/events.jsonl"
events_hash="$(sha256sum "$state/machine-sentinel/events.jsonl" | awk '{print $1}')"
cp "$state/machine-sentinel/events.jsonl" "$work/events-before-restart.jsonl"
receipt_hash="$(sha256sum "$state/bootstrap-receipt.json" | awk '{print $1}')"

docker exec "$configured" rm -f /run/agora-image-bootstrap.status
docker restart "$configured" >/dev/null
wait_for_ssh "$configured_port"
wait_for_bootstrap "$configured"
ssh "${ssh_options[@]}" -p "$configured_port" root@127.0.0.1 '
  set -Eeuo pipefail
  test "$(cat /run/agora-image-bootstrap.status)" = 0
  test "$(tmux list-sessions -F "#{session_name}" | grep -xc agora_sentinel)" = 1
  test "$(tmux list-sessions -F "#{session_name}" | grep -xc agora_px0)" = 1
  jq -e '\''.status == "ready" and .assignmentGeneration == 4 and
    .runtimeTrainingSource.status == "approved_repair"'\'' /workspace/agora-run/bootstrap-receipt.json >/dev/null
'
test "$(docker exec "$configured" git -C /opt/agora-source rev-parse HEAD)" = "$repaired_source_commit"
test -s "$state/machine-sentinel/events.jsonl"
cmp -n "$(stat -c %s "$work/events-before-restart.jsonl")" \
  "$work/events-before-restart.jsonl" "$state/machine-sentinel/events.jsonl"
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
    generatedIdentityRestart:true, heartbeatStartedAndPersistent:true, knownRepair:true,
    optionalFailureIndependence:true, restart:true, sentinelOutageSpool:true,
    px0LoopbackTunnel:true, px0PermittedRead:true, px0SourceMtime:true,
    px0TraversalDenied:true, secretExcluded:true},
    runtimeEventsBeforeRestartSha256:$runtimeEventsBeforeRestartSha256}' \
  > "$EVIDENCE"

trap - EXIT
cleanup
