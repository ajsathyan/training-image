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

exec sleep infinity
