# Training Image

Public RunPod dependency image for Agora training setup.

This repository intentionally contains only build instructions for the container
image. HF tokens, SMTP credentials, RunPod API keys, SSH keys, fleet state, and
autoscaler logic stay in the private `agora-runpod` control-plane repository.

The published image is:

```text
ghcr.io/ajsathyan/training-image:latest
```

The image preinstalls Python 3.11, tmux/jq/network tooling, PyTorch 2.7 CUDA
runtime bindings, Hivemind, and Agora Python dependencies into
`/opt/agora-venv`. Per-machine setup still writes secrets only on the RunPod pod
and still installs/starts the tmux watchdog at setup time.
