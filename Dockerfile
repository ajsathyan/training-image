# check=skip=InvalidBaseImagePlatform
ARG UPSTREAM_IMAGE=ghcr.io/pluralisresearch/agora-test@sha256:da54b2e3e37b90f9f62d9a04546a95e3bd4711fb4641bd61c13e3db8561a1326
FROM ${UPSTREAM_IMAGE}

ARG TARGETARCH
ARG TRAINING_REPO_URL=https://github.com/PluralisResearch/agora-test.git
ARG TRAINING_REPO_REF=71a44b894100baa8f2996b97e73ae0bd67fa6b9d
ARG FLEET_SOURCE_COMMIT=7ff63e5ed211804d92aa94a17251625f90519061
ARG FLEET_SOURCE_TREE=9481feef793d9aebac96522efe9139752ed5c549
ARG FLEET_SOURCE_ARTIFACT_FINGERPRINT=2ef224a75cc502ebb04d53180367a546dd41374655ee49a79c69f3c6365a74ff
ARG PX0_VERSION=0.1.6
ARG PX0_SHA256=d4f2378a1d6fbda9960cc7da45a5e3b5a5f9f6b331be80bcbb8a27f9dc5e9e0c

ENV DEBIAN_FRONTEND=noninteractive \
    VIRTUAL_ENV=/opt/agora-venv \
    PATH=/opt/agora-venv/bin:$PATH \
    AGORA_MACHINE_RUNTIME_DIR=/opt/agora-machine-runtime \
    AGORA_TRAINING_SOURCE_DIR=/opt/agora-source \
    AGORA_PYTHON_BIN=/opt/agora-venv/bin/python

LABEL io.agora.image.platform="linux/amd64" \
      io.agora.training.repository="${TRAINING_REPO_URL}" \
      io.agora.training.revision="${TRAINING_REPO_REF}" \
      io.agora.fleet.commit="${FLEET_SOURCE_COMMIT}" \
      io.agora.fleet.tree="${FLEET_SOURCE_TREE}" \
      io.agora.image.contract="agora.machine-image-capability.v1" \
      io.agora.fleet.source-artifact-fingerprint="${FLEET_SOURCE_ARTIFACT_FINGERPRINT}" \
      io.agora.px0.version="${PX0_VERSION}" \
      io.agora.px0.sha256="${PX0_SHA256}"

RUN test "$TARGETARCH" = "amd64" \
    && apt-get update \
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
        tmux \
        util-linux \
    && ln -sfn /opt/conda /opt/agora-venv \
    && useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin agora-inspection \
    && rm -rf /var/lib/apt/lists/* \
    && /opt/agora-venv/bin/python -c 'import sys, torch; assert sys.version_info[:2] == (3, 13), sys.version; assert torch.__version__.startswith("2.11."), torch.__version__'

RUN git clone --filter=blob:none --no-checkout "$TRAINING_REPO_URL" /opt/agora-source \
    && git -C /opt/agora-source fetch --depth 1 origin "$TRAINING_REPO_REF" \
    && git -C /opt/agora-source checkout --detach "$TRAINING_REPO_REF" \
    && test "$(git -C /opt/agora-source rev-parse HEAD)" = "$TRAINING_REPO_REF" \
    && /opt/agora-venv/bin/python -m pip install --no-build-isolation --no-deps \
        -e /opt/agora-source/pithos \
        -e /opt/agora-source/agora_server \
        -e /opt/agora-source/agora \
    && /opt/agora-venv/bin/python - <<'PY'
import pathlib
import agora
import agora_server
import pithos

root = pathlib.Path("/opt/agora-source").resolve()
for module in (pithos, agora_server, agora):
    pathlib.Path(module.__file__).resolve().relative_to(root)
PY

RUN curl -fsSLo /tmp/px0 "https://github.com/px0-ai/px0/releases/download/v${PX0_VERSION}/px0-${PX0_VERSION}-linux-amd64" \
    && printf '%s  /tmp/px0\n' "$PX0_SHA256" | sha256sum -c - \
    && install -m 0755 /tmp/px0 /usr/local/bin/px0 \
    && rm -f /tmp/px0 \
    && px0 -version

COPY machine-runtime /opt/agora-machine-runtime
COPY image-runtime /opt/agora-image-runtime
COPY start.sh /start.sh

RUN chmod 755 /start.sh \
    /opt/agora-image-runtime/assignment_transition.py \
    /opt/agora-image-runtime/agora_image_bootstrap.py \
    /opt/agora-image-runtime/refresh_inspection.py \
    && install -m 0700 \
        /opt/agora-machine-runtime/scripts/agora_heartbeat_agent.py \
        /opt/agora-image-runtime/agora_heartbeat_agent.py \
    && find /opt/agora-machine-runtime -type f -name '*.py' -exec chmod 644 {} + \
    && mkdir -p /run/sshd /root/.ssh \
    && chmod 700 /root/.ssh \
    && rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub \
    && TRAINING_REPO_URL="$TRAINING_REPO_URL" \
       TRAINING_REPO_REF="$TRAINING_REPO_REF" \
       FLEET_SOURCE_COMMIT="$FLEET_SOURCE_COMMIT" \
       FLEET_SOURCE_TREE="$FLEET_SOURCE_TREE" \
       FLEET_SOURCE_ARTIFACT_FINGERPRINT="$FLEET_SOURCE_ARTIFACT_FINGERPRINT" \
       PX0_VERSION="$PX0_VERSION" \
       PX0_SHA256="$PX0_SHA256" \
       /opt/agora-venv/bin/python - <<'PY'
import hashlib
import json
import os
from pathlib import Path

runtime_dir = Path("/opt/agora-machine-runtime")
image_runtime_dir = Path("/opt/agora-image-runtime")
manifest_path = runtime_dir / "manifest.json"
manifest_bytes = manifest_path.read_bytes()
manifest = json.loads(manifest_bytes)
assert manifest["schemaVersion"] == "agora.machine-runtime-export.v1"
unsigned = dict(manifest)
fingerprint = unsigned.pop("artifactFingerprint")
canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
assert hashlib.sha256(canonical).hexdigest() == fingerprint
assert manifest["source"]["commit"] == os.environ["FLEET_SOURCE_COMMIT"]
assert manifest["source"]["tree"] == os.environ["FLEET_SOURCE_TREE"]
assert manifest["source"]["artifactFingerprint"] == os.environ["FLEET_SOURCE_ARTIFACT_FINGERPRINT"]

def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

assert isinstance(manifest.get("files"), list) and manifest["files"]
for entry in manifest["files"]:
    raw_artifact = runtime_dir / entry["path"]
    artifact = raw_artifact.resolve()
    artifact.relative_to(runtime_dir.resolve())
    assert raw_artifact.is_file() and not raw_artifact.is_symlink()
    assert digest(raw_artifact) == entry["sha256"]

capability = {
    "schemaVersion": "agora.machine-image-capability.v1",
    "contractVersion": "agora.machine-image-capability.v1",
    "bootstrap": {
        "path": "/opt/agora-image-runtime/agora_image_bootstrap.py",
        "sha256": digest(image_runtime_dir / "agora_image_bootstrap.py"),
    },
    "assignmentTransition": {
        "contractVersion": "agora.assignment-transition.v1",
        "path": "/opt/agora-image-runtime/assignment_transition.py",
        "sha256": digest(image_runtime_dir / "assignment_transition.py"),
    },
    "inspection": {
        "path": "/opt/agora-image-runtime/refresh_inspection.py",
        "sha256": digest(image_runtime_dir / "refresh_inspection.py"),
        "root": "/run/agora-inspection",
        "bindHost": "127.0.0.1",
        "port": 7777,
        "public": False,
    },
    "px0": {
        "path": "/usr/local/bin/px0",
        "version": os.environ["PX0_VERSION"],
        "sha256": os.environ["PX0_SHA256"],
    },
    "heartbeat": {
        "path": "/opt/agora-image-runtime/agora_heartbeat_agent.py",
        "sha256": digest(image_runtime_dir / "agora_heartbeat_agent.py"),
    },
    "paths": {
        "remoteRoot": "/workspace/agora-run",
        "config": "/workspace/agora-run/controller-input/machine-config.json",
        "hfToken": "/workspace/agora-run/controller-input/hf-token",
        "receipt": "/workspace/agora-run/bootstrap-receipt.json",
    },
    "fleetSource": manifest["source"],
    "runtimeExport": {
        "artifactFingerprint": manifest["artifactFingerprint"],
        "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
    },
    "trainingSource": {
        "repository": os.environ["TRAINING_REPO_URL"],
        "commit": os.environ["TRAINING_REPO_REF"],
    },
}
(image_runtime_dir / "capability.json").write_text(
    json.dumps(capability, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY

WORKDIR /workspace

EXPOSE 22 49200

CMD ["/start.sh"]
