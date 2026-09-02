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
