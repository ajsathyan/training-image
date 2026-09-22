#!/usr/bin/env bash
set -Eeuo pipefail

boot_autostart="${AGORA_BOOT_AUTOSTART:-}"
boot_launch_b64="${AGORA_BOOT_LAUNCH_B64:-}"
boot_hf_token="${AGORA_BOOT_HF_TOKEN:-}"
boot_metadata_wait_seconds="${AGORA_BOOT_METADATA_WAIT_SECONDS:-}"
unset HF_TOKEN AGORA_BOOT_HF_TOKEN AGORA_BOOT_LAUNCH_B64 \
    AGORA_SENTINEL_BOOTSTRAP_TOKEN AGORA_SENTINEL_MACHINE_TOKEN

vast_start_log=/var/log/agora-vast-onstart.log
log_start_stage() {
    printf 'stage=%s rc=%s\n' "$1" "${2:-0}" >> "$vast_start_log"
}
start_failure() {
    rc=$?
    log_start_stage start_sh_failed "$rc"
    exit "$rc"
}
trap start_failure ERR

install -d -o root -g root -m 755 /run/sshd
install -d -o root -g root -m 700 /root/.ssh
if [[ -L /root/.ssh/authorized_keys ]] || \
   [[ -e /root/.ssh/authorized_keys && ! -f /root/.ssh/authorized_keys ]]; then
    log_start_stage authorized_keys_unsafe 76
    exit 76
fi
touch /root/.ssh/authorized_keys
chown root:root /root/.ssh /root/.ssh/authorized_keys
chmod 700 /root/.ssh
chmod 600 /root/.ssh/authorized_keys

configured_public_key="${SSH_PUBLIC_KEY:-${PUBLIC_KEY:-}}"
if [[ -n "${SSH_PUBLIC_KEY:-}" && -n "${PUBLIC_KEY:-}" && \
      "$SSH_PUBLIC_KEY" != "$PUBLIC_KEY" ]]; then
    log_start_stage conflicting_public_keys 76
    exit 76
fi
if [[ -n "$configured_public_key" ]]; then
    if [[ "$configured_public_key" == *$'\n'* || "$configured_public_key" == *$'\r'* ]]; then
        log_start_stage invalid_public_key 76
        exit 76
    fi
    grep -qxF "$configured_public_key" /root/.ssh/authorized_keys || \
        printf '%s\n' "$configured_public_key" >> /root/.ssh/authorized_keys
fi
log_start_stage key_setup_ready

ssh-keygen -A
/usr/sbin/sshd -t
pgrep -x sshd >/dev/null 2>&1 || /usr/sbin/sshd
log_start_stage sshd_ready

pgrep -x cron >/dev/null 2>&1 || service cron start >/dev/null 2>&1 || cron

if ! grep -qF 'source /etc/rp_environment' /root/.bashrc; then
    [[ -f /etc/rp_environment ]] && printf '%s\n' 'source /etc/rp_environment' >> /root/.bashrc
fi

# Keep the historical SSH-login environment compatibility without copying
# provider identity, credentials, or arbitrary launch variables.  This is a
# separate image-owned snapshot; provider-owned metadata files are untouched.
agora_environment_next="$(mktemp /etc/.agora_environment.XXXXXX)"
{
    for name in CUDA_HOME CUDA_PATH LD_LIBRARY_PATH NVIDIA_DRIVER_CAPABILITIES NVIDIA_REQUIRE_CUDA PATH; do
        value="${!name-}"
        if [[ -n "$value" && "$value" != *$'\n'* && "$value" != *$'\r'* ]]; then
            printf 'export %s=%q\n' "$name" "$value"
        fi
    done
} > "$agora_environment_next"
chmod 644 "$agora_environment_next"
mv -f "$agora_environment_next" /etc/agora_environment
if ! grep -qF 'source /etc/agora_environment' /root/.bashrc; then
    printf '%s\n' 'source /etc/agora_environment' >> /root/.bashrc
fi

exec 9>/run/agora-image-start.lock
if ! flock -n 9; then
    exit 0
fi

rm -f /run/agora-image-bootstrap.status /run/agora-image-bootstrap.status.json
set +e
env \
    AGORA_BOOT_AUTOSTART="$boot_autostart" \
    AGORA_BOOT_LAUNCH_B64="$boot_launch_b64" \
    AGORA_BOOT_HF_TOKEN="$boot_hf_token" \
    AGORA_BOOT_METADATA_WAIT_SECONDS="$boot_metadata_wait_seconds" \
    /opt/agora-venv/bin/python /opt/agora-image-runtime/agora_boot_start.py \
    >/var/log/agora-image-bootstrap.log 2>&1
bootstrap_rc=$?
set -e
printf '%s\n' "$bootstrap_rc" > /run/agora-image-bootstrap.status
log_start_stage bootstrap_complete "$bootstrap_rc"

unset boot_autostart boot_launch_b64 boot_hf_token boot_metadata_wait_seconds configured_public_key
trap - ERR

exec sleep infinity
