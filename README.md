# Agora training image

This repository builds the public `linux/amd64` dependency image used by the
private Agora fleet controller. It contains training software and reviewed
machine-side runtime code. It does not contain HF tokens, provider credentials,
SSH private keys, fleet state, or autoscaling policy.

The publication target is `ghcr.io/ajsathyan/training-image`. Pull requests
build and run fresh-container tests without pushing. Merges to `main` publish
`latest` and `sha-<commit>` through `.github/workflows/publish-image.yml`.

## Pinned inputs

- upstream image: `ghcr.io/pluralisresearch/agora-test@sha256:da54b2e3e37b90f9f62d9a04546a95e3bd4711fb4641bd61c13e3db8561a1326`
- Agora training source: `PluralisResearch/agora-test@71a44b894100baa8f2996b97e73ae0bd67fa6b9d`
- fleet runtime: `ajsathyan/agora-runpod@906ebef7d04c43f73f173dfc8cab34b6b30940d2`
- fleet source tree: `a0a337ae8ed84bfe4fd115a54ca07593d6aa6a12`
- runtime export: `bde1307a1515581cc5f44bfd1308b08623f73e5453e085aa1ed37437b1fe4ce3`
- px0: `v0.1.6`, verified by the SHA-256 in `Dockerfile`
- in-place repair build tooling: exact wheel hashes in
  `image-repair-build-requirements.txt`

The build verifies those identities, the runtime manifest, Python 3.13/PyTorch
2.11, the canonical repair backend imports, and the installed Agora import
paths. Normal boot does not clone, fetch, pull, or run pip.

## Machine-image contract

The baked marker is `/opt/agora-image-runtime/capability.json`, schema
`agora.machine-image-capability.v1`. A controller that explicitly declares this
contract as machine provenance
`imageCapability={contractVersion:"agora.machine-image-capability.v1",runtimeArtifactFingerprint:"<sha256>"}`
writes these files through its existing machine-scoped SSH path:

```text
/workspace/agora-run/controller-input/machine-config.json  (0600)
/workspace/agora-run/controller-input/hf-token             (0600)
```

The config schema is `agora.machine-image-config.v1`. The bootstrap is:

```text
/opt/agora-venv/bin/python /opt/agora-image-runtime/agora_image_bootstrap.py \
  --config /workspace/agora-run/controller-input/machine-config.json \
  --token-file /workspace/agora-run/controller-input/hf-token \
  --receipt /workspace/agora-run/bootstrap-receipt.json
```

The receipt schema is `agora.machine-image-bootstrap-receipt.v1`. The controller
must match its config and capability hashes before accepting setup. A declared
v1 image fails closed when its marker, runtime, bootstrap, or receipt differs;
it must not silently fall back to copied code. Undeclared images keep the legacy
setup path.

`assignmentOperationId` and `trainingSessionId` are explicit local assignment
identity. Historical launch, reservation, slot, fleet, migration, and remote
authority metadata are not invented or required for local SSH and training.
A provider machine may accept a newer controller-authorized assignment
generation; stale or conflicting generations fail closed.

SSH starts before bootstrap. Missing machine config leaves SSH ready with
training disabled. Sentinel reporting, inspection, and px0 are optional and
cannot block requested training. Training failures still fail the core receipt.

### Explicit provider boot-autostart

Normal/manual users need no controller reporting credentials. With no
`AGORA_BOOT_AUTOSTART=1`, `/start.sh` keeps the existing behavior: it runs saved
canonical controller input when present, otherwise leaves SSH ready and training
off. Merely setting an HF token never opts in.

The opt-in controller supplies one base64 `agora.machine-boot-launch.v1`
envelope in `AGORA_BOOT_LAUNCH_B64` and only that machine's credential in
`AGORA_BOOT_HF_TOKEN`. The envelope carries no provider API key and defers the
provider resource id and public training port. RunPod must provide exact
`RUNPOD_POD_ID` and `RUNPOD_TCP_PORT_49200`; Vast must provide numeric
`CONTAINER_ID` (or exact `VAST_CONTAINERLABEL=C.<id>`) and
`VAST_TCP_PORT_49200`. The adapter reopens provider-owned metadata files during
its bounded wait. It never scans ports, uses internal 49200 as public, or infers
SSH+1.

Missing metadata records `waiting_for_network_config`; malformed or conflicting
input records `invalid_input`. Both leave SSH/container life intact for the
controller's SSH fallback. `/start.sh`, Vast `onstart`, repair, and reboot share
one starter/assignment lock. A saved newer ready assignment restarts from its
canonical 0600 input; staged, fenced, paused, corrupt, or stale launch state
stays stopped. `training-intent.json` records running versus paused intent.

Configured heartbeat uses a separately written 0600 machine-secret dotenv file;
the image never receives the controller's master heartbeat secret. Its baked
agent and watchdog start only with requested training, preserve local state over
container restart, and report `started`, `staged`, `disabled`, or nonblocking
`unavailable` in the receipt.

The exact baked training commit remains immutable build provenance. If the
canonical outdated-client repair advances the checkout, it atomically records
an assignment-bound 0600 repair-provenance marker. A later bootstrap accepts the
new runtime commit only when that marker, durable machine state, provider
binding, and assignment all agree; unrelated source drift still fails closed.

## Private log inspection

px0 runs as the unprivileged `agora-inspection` user, binds only
`127.0.0.1:7777` in the container, and uses `-no-agent -no-lsp -no-git
-no-telemetry`. Its root is a copied regular-file allowlist at
`/run/agora-inspection`, outside the root-owned private runtime. Credentials,
config, private keys, and source remain outside it. Port 7777 is not exposed by
the image.

A root-owned refresher updates the allowlist every 15 seconds. Each copy is
bounded to the newest 2 MiB per log, retains the source log's UTC mtime in the
metadata, and removes an allowlisted copy when its source disappears. Original
runtime logs are never modified.

Use one labeled local port per exact provider machine:

```bash
# machine-id=machine-123 providerResourceId=pod-abc local-port=17777
ssh -N -L 127.0.0.1:17777:127.0.0.1:7777 \
  -p <exact-provider-ssh-port> root@<exact-provider-host>
```

Open `http://127.0.0.1:17777`, then confirm the machine and provider-resource
identity in `inspection-metadata.json`. The SSH tunnel is the access control.
Refresh the copied allowlist on demand with:

```bash
/opt/agora-venv/bin/python /opt/agora-image-runtime/refresh_inspection.py \
  --root /workspace/agora-run --inspection-root /run/agora-inspection
```

## Rebuild the fleet runtime export

Use a fleet clone containing the reviewed exact commit:

```bash
python3 tools/export_fleet_runtime.py \
  --source-repo /absolute/path/to/agora-runpod \
  --source-commit 906ebef7d04c43f73f173dfc8cab34b6b30940d2 \
  --output-dir machine-runtime
python3 -m unittest tests.test_runtime_export -v
```

The exporter rejects source-hash drift, unowned files, symlinks, and modified
generated artifacts. `machine-runtime/manifest.json` records source commit/tree,
source and export fingerprints, and every file hash.

## Proof boundaries

The pull-request workflow proves source/unit checks, an exact `linux/amd64`
build, neutral and configured fresh containers, SSH readiness, restart
idempotency, outage spooling, private px0 access, and secret exclusion. It
uploads image identity, size, and timing evidence.

It does not prove a GPU workload, provider networking, live Agora enrollment,
or production health. Those require a separately authorized exact-digest GPU
canary after publication.
