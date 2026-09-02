# Training Image

Public RunPod dependency image for Agora training setup.

This repository intentionally contains only build instructions for the container
image. HF tokens, SMTP credentials, RunPod API keys, SSH keys, fleet state, and
autoscaler logic stay in the private `agora-runpod` control-plane repository.

The published image is:

```text
ghcr.io/ajsathyan/training-image:latest
```

The image wraps `ghcr.io/pluralisresearch/agora-test:latest` with the SSH,
cron, tmux, jq, and network/process tooling needed by the RunPod control plane.
Its Python 3.13 and PyTorch 2.11 runtime, including the staged Pithos, Agora
Server, and Agora packages, is available at `/opt/agora-venv`. Per-machine setup
still writes secrets only on the RunPod pod and installs/starts the tmux
watchdog at setup time.

The image also bundles the deterministic Agora Machine Sentinel. It remains
disabled unless `AGORA_SENTINEL_BOOTSTRAP_TOKEN` is present, so existing image
users keep the legacy SSH/cron behavior. When enabled, the launcher validates
the complete cloud-issued machine identity before starting and stores its
identity, rotating machine credential, command journal, and runtime state only
under `/workspace/.agora/machine-sentinel` with private permissions. Remote
commands are limited to the six typed Sentinel actions; command payloads are
never executed as shell text.

Cloud-owned launches enable the agent with `AGORA_SENTINEL_URL`,
`AGORA_SENTINEL_BOOTSTRAP_TOKEN`, and the `AGORA_FLEET_ID`, `AGORA_LAUNCH_ID`,
`AGORA_RESERVATION_ID`, `AGORA_SLOT_ID`, `AGORA_SLOT_GENERATION`,
`AGORA_MACHINE_GENERATION_ID`, `AGORA_MACHINE_ID`, `AGORA_TOKEN_LABEL`,
`AGORA_NODE_TYPE`, `AGORA_GPU_MODEL`, `AGORA_SETUP_REVISION`, and
`AGORA_AUTHORITY_EPOCH` identity fields. The RunPod runtime supplies
`RUNPOD_POD_ID` and optionally `RUNPOD_POD_NAME`. The observation URL must be
the exact HTTPS `/api/machine-sentinel/observe` endpoint.
