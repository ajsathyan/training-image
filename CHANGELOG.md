# Changelog

## Unreleased

- Pin the upstream image, Agora source, fleet runtime export, and px0 binary to exact verified identities.
- Add the versioned machine-image capability/config/receipt contract and controller-authorized reassignment fencing.
- Keep Sentinel, private inspection, and px0 failures independent from SSH and requested training.
- Add loopback-only, SSH-tunneled px0 under an unprivileged account with a regular-file allowlist.
- Bake assignment-bound heartbeat supervision with a separate machine credential and restart-persistent state.
- Bind approved in-place Agora client repairs to durable provenance so repaired containers restart without accepting unknown source drift.
- Add neutral/configured/restart/security fresh-container smoke checks and build evidence artifacts.
- Bundle an opt-in Agora Machine Sentinel boot agent with persistent private state and six fixed typed action handlers.
- Wrap the upstream Agora test image with only the RunPod SSH/control tools and
  expose its Python 3.13/PyTorch 2.11 runtime at `/opt/agora-venv`.
