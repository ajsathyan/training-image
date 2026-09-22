# syntax=docker/dockerfile:1
FROM --platform=linux/amd64 ubuntu:24.04@sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea

ENV DEBIAN_FRONTEND=noninteractive \
    VIRTUAL_ENV=/opt/agora-venv \
    PATH=/opt/agora-venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONPATH=/opt/agora-source \
    AGORA_MACHINE_RUNTIME_DIR=/opt/agora-machine-runtime \
    AGORA_TRAINING_SOURCE_DIR=/opt/agora-source \
    AGORA_PYTHON_BIN=/opt/agora-venv/bin/python

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        coreutils \
        cron \
        curl \
        git \
        iproute2 \
        jq \
        openssh-server \
        passwd \
        procps \
        python3 \
        tmux \
        util-linux \
    && install -d -m 0755 /opt/agora-venv/bin /run/sshd /root/.ssh \
    && ln -s /usr/bin/python3 /opt/agora-venv/bin/python \
    && useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin agora-inspection \
    && curl -fsSLo /usr/local/bin/px0 \
        https://github.com/px0-ai/px0/releases/download/v0.1.6/px0-0.1.6-linux-amd64 \
    && printf '%s  %s\n' \
        d4f2378a1d6fbda9960cc7da45a5e3b5a5f9f6b331be80bcbb8a27f9dc5e9e0c \
        /usr/local/bin/px0 | sha256sum -c - \
    && chmod 0755 /usr/local/bin/px0 \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb \
    && rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub

COPY start.sh /start.sh
COPY image-runtime /opt/agora-image-runtime
COPY machine-runtime /opt/agora-machine-runtime
COPY tests/process-composition-training-source /opt/agora-source

RUN chmod 0755 /start.sh /opt/agora-image-runtime/*.py \
    && printf '%s\n' /opt/agora-source > /usr/local/lib/python3.12/dist-packages/agora-process-fixture.pth \
    && install -m 0700 /opt/agora-machine-runtime/scripts/agora_heartbeat_agent.py \
        /opt/agora-image-runtime/agora_heartbeat_agent.py \
    && git -C /opt/agora-source init -q \
    && git -C /opt/agora-source config user.name 'Agora process fixture' \
    && git -C /opt/agora-source config user.email 'process-fixture@example.invalid' \
    && git -C /opt/agora-source add -A \
    && GIT_AUTHOR_DATE=2000-01-01T00:00:00Z GIT_COMMITTER_DATE=2000-01-01T00:00:00Z \
        git -C /opt/agora-source commit -q -m 'test: minimal process fixture source' \
    && /opt/agora-venv/bin/python - <<'PY'
import hashlib
import json
import subprocess
from pathlib import Path

image_runtime = Path("/opt/agora-image-runtime")
machine_runtime = Path("/opt/agora-machine-runtime")
manifest_bytes = (machine_runtime / "manifest.json").read_bytes()
manifest = json.loads(manifest_bytes)
unsigned = dict(manifest)
fingerprint = unsigned.pop("artifactFingerprint")
assert hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == fingerprint
for entry in manifest["files"]:
    path = machine_runtime / entry["path"]
    assert path.is_file() and not path.is_symlink()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
def digest(name):
    return hashlib.sha256((image_runtime / name).read_bytes()).hexdigest()
commit = subprocess.run(
    ["git", "-C", "/opt/agora-source", "rev-parse", "HEAD"],
    check=True, capture_output=True, text=True,
).stdout.strip()
capability = {
    "schemaVersion": "agora.machine-image-capability.v1",
    "contractVersion": "agora.machine-image-capability.v1",
    "bootstrap": {"path": "/opt/agora-image-runtime/agora_image_bootstrap.py", "sha256": digest("agora_image_bootstrap.py")},
    "bootStart": {"contractVersion": "agora.machine-boot-launch.v1", "path": "/opt/agora-image-runtime/agora_boot_start.py", "sha256": digest("agora_boot_start.py")},
    "assignmentTransition": {"contractVersion": "agora.assignment-transition.v1", "path": "/opt/agora-image-runtime/assignment_transition.py", "sha256": digest("assignment_transition.py")},
    "inspection": {"path": "/opt/agora-image-runtime/refresh_inspection.py", "sha256": digest("refresh_inspection.py"), "root": "/run/agora-inspection", "bindHost": "127.0.0.1", "port": 7777, "public": False},
    "px0": {"path": "/usr/local/bin/px0", "version": "0.1.6", "sha256": "d4f2378a1d6fbda9960cc7da45a5e3b5a5f9f6b331be80bcbb8a27f9dc5e9e0c"},
    "heartbeat": {"path": "/opt/agora-image-runtime/agora_heartbeat_agent.py", "sha256": digest("agora_heartbeat_agent.py")},
    "paths": {"remoteRoot": "/workspace/agora-run", "config": "/workspace/agora-run/controller-input/machine-config.json", "hfToken": "/workspace/agora-run/controller-input/hf-token", "receipt": "/workspace/agora-run/bootstrap-receipt.json"},
    "fleetSource": manifest["source"],
    "runtimeExport": {"artifactFingerprint": fingerprint, "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest()},
    "trainingSource": {"repository": "fixture://minimal-agora-source", "commit": commit},
}
(image_runtime / "capability.json").write_text(json.dumps(capability, indent=2, sort_keys=True) + "\n")
PY

WORKDIR /workspace
EXPOSE 22 49200
CMD ["/start.sh"]
