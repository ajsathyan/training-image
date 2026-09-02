#!/usr/bin/env bash
set -Eeuo pipefail

mkdir -p /run/sshd /root/.ssh
chmod 700 /root/.ssh

if [[ -n "${PUBLIC_KEY:-}" ]]; then
    printf '%s\n' "$PUBLIC_KEY" > /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
fi

ssh-keygen -A
/usr/sbin/sshd

service cron start >/dev/null 2>&1 || cron

{
    while IFS='=' read -r name value; do
        printf 'export %s=%q\n' "$name" "$value"
    done < <(printenv | grep -E '^(RUNPOD_|NVIDIA_|CUDA_|LD_LIBRARY_PATH=|PATH=)')
} > /etc/rp_environment

if ! grep -qF 'source /etc/rp_environment' /root/.bashrc; then
    printf '%s\n' 'source /etc/rp_environment' >> /root/.bashrc
fi

if [[ -n "${AGORA_SENTINEL_BOOTSTRAP_TOKEN:-}" ]]; then
    umask 077
    sentinel_state_dir=/workspace/.agora/machine-sentinel
    mkdir -p "$sentinel_state_dir"
    chmod 700 /workspace/.agora "$sentinel_state_dir"
    /opt/agora-machine-sentinel/start-machine-sentinel.sh \
        >> "$sentinel_state_dir/launcher.log" 2>&1 &
    unset AGORA_SENTINEL_BOOTSTRAP_TOKEN
fi

exec sleep infinity
