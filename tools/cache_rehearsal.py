#!/usr/bin/env python3
"""Exercise the Dockerfile cache graph with tiny, structure-bound fixtures.

This is not a production-image benchmark.  It deterministically maps the real
Dockerfile's ARG/COPY/RUN boundaries onto marker-producing lightweight steps,
then uses an inline cache, a disposable registry, and fresh BuildKit builders.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BOUNDARIES = ("os", "repair", "training", "px0", "runtime")
LANDMARKS = {
    "base": "FROM ${UPSTREAM_IMAGE}",
    "repair-copy": "COPY image-repair-build-requirements.txt",
    "os": 'RUN test "$TARGETARCH" = "amd64"',
    "repair": "RUN /opt/agora-venv/bin/uv pip install",
    "training-args": "ARG TRAINING_REPO_URL=",
    "training": "RUN git clone",
    "px0-args": "ARG PX0_VERSION=",
    "px0": "RUN curl -fsSLo /tmp/px0",
    "fleet-args": "ARG FLEET_SOURCE_COMMIT=",
    "runtime-copy": "COPY machine-runtime",
    "runtime": "RUN chmod 755 /start.sh",
    "labels": "LABEL io.agora.image.platform",
}


class RehearsalError(RuntimeError):
    pass


def inspect_real_dockerfile(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    mapping: list[dict[str, Any]] = []
    previous = -1
    for name, needle in LANDMARKS.items():
        offset = text.find(needle)
        if offset < 0:
            raise RehearsalError(f"real Dockerfile landmark missing: {name}")
        if offset <= previous:
            raise RehearsalError(f"real Dockerfile landmark out of order: {name}")
        previous = offset
        mapping.append(
            {
                "boundary": name,
                "needle": needle,
                "line": text.count("\n", 0, offset) + 1,
                "byteOffset": offset,
            }
        )
    return mapping


def generated_dockerfile(*, old_layout: bool = False) -> str:
    early = """
ARG TRAINING_REPO_URL
ARG TRAINING_REPO_REF
ARG PX0_VERSION
ARG PX0_SHA256
ARG FLEET_SOURCE_COMMIT
ARG FLEET_SOURCE_TREE
ARG FLEET_SOURCE_ARTIFACT_FINGERPRINT
""" if old_layout else ""
    training = "" if old_layout else "ARG TRAINING_REPO_URL\nARG TRAINING_REPO_REF\n"
    px0 = "" if old_layout else "ARG PX0_VERSION\nARG PX0_SHA256\n"
    fleet = "" if old_layout else (
        "ARG FLEET_SOURCE_COMMIT\nARG FLEET_SOURCE_TREE\n"
        "ARG FLEET_SOURCE_ARTIFACT_FINGERPRINT\n"
    )
    return f"""# generated from the reviewed real-Dockerfile boundary map
ARG UPSTREAM_IMAGE
FROM ${{UPSTREAM_IMAGE}}
ARG TARGETARCH
{early}COPY image-repair-build-requirements.txt /inputs/requirements.txt
RUN echo CACHE_BOUNDARY=os "$TARGETARCH" "${{FLEET_SOURCE_COMMIT:-}}" \
      && cat /inputs/requirements.txt > /marker-os
RUN echo CACHE_BOUNDARY=repair && cat /inputs/requirements.txt > /marker-repair
{training}RUN echo CACHE_BOUNDARY=training "$TRAINING_REPO_URL" "$TRAINING_REPO_REF" \
      | tee /marker-training
{px0}RUN echo CACHE_BOUNDARY=px0 "$PX0_VERSION" "$PX0_SHA256" | tee /marker-px0
{fleet}COPY machine-runtime /inputs/machine-runtime
COPY image-runtime /inputs/image-runtime
COPY start.sh /inputs/start.sh
RUN echo CACHE_BOUNDARY=runtime "$FLEET_SOURCE_COMMIT" "$FLEET_SOURCE_TREE" \
      "$FLEET_SOURCE_ARTIFACT_FINGERPRINT" \
      && cat /inputs/machine-runtime/marker /inputs/image-runtime/marker \
        /inputs/start.sh > /marker-runtime
LABEL rehearsal.training="$TRAINING_REPO_REF" \
      rehearsal.px0="$PX0_VERSION" \
      rehearsal.fleet="$FLEET_SOURCE_COMMIT"
"""


def _run(
    command: list[str], *, cwd: Path | None = None, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def _write_fixture(root: Path, dockerfile: str) -> None:
    (root / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    (root / "machine-runtime").mkdir()
    (root / "image-runtime").mkdir()
    (root / "image-repair-build-requirements.txt").write_text("repair-v1\n", encoding="utf-8")
    (root / "machine-runtime/marker").write_text("machine-v1\n", encoding="utf-8")
    (root / "image-runtime/marker").write_text("runtime-v1\n", encoding="utf-8")
    (root / "start.sh").write_text("start-v1\n", encoding="utf-8")


def _builder(name: str, network: str, config: Path) -> None:
    _run(
        [
            "docker", "buildx", "create", "--name", name, "--driver", "docker-container",
            "--driver-opt", f"network={network}", "--buildkitd-config", str(config), "--use",
        ]
    )
    _run(["docker", "buildx", "inspect", "--builder", name, "--bootstrap"], capture=True)


def _build(
    *, builder: str, context: Path, tag: str | None, cache_from: str | None,
    arguments: dict[str, str], metadata: Path | None = None, inline: bool = False,
) -> tuple[float, str, str | None]:
    command = [
        "docker", "buildx", "build", "--builder", builder, "--progress=plain",
        "--platform", "linux/amd64",
    ]
    for key, value in sorted(arguments.items()):
        command.extend(["--build-arg", f"{key}={value}"])
    if cache_from:
        command.extend(["--cache-from", cache_from])
    if tag:
        command.extend(["--tag", tag, "--push"])
    else:
        command.extend(["--output", "type=cacheonly"])
    if inline:
        command.extend(["--cache-to", "type=inline"])
    if metadata:
        command.extend(["--metadata-file", str(metadata)])
    command.append(str(context))
    started = time.monotonic()
    completed = _run(command, capture=True)
    seconds = time.monotonic() - started
    digest = None
    if metadata:
        digest = json.loads(metadata.read_text(encoding="utf-8"))["containerimage.digest"]
    return seconds, completed.stdout, digest


def cache_statuses(log: str) -> dict[str, str]:
    statuses: dict[str, str] = {}
    lines = log.splitlines()
    for boundary in BOUNDARIES:
        marker_index = next(
            (index for index, line in enumerate(lines) if f"CACHE_BOUNDARY={boundary}" in line),
            None,
        )
        if marker_index is None:
            raise RehearsalError(f"missing BuildKit marker for {boundary}")
        match = re.match(r"^(#\d+)", lines[marker_index])
        if not match:
            raise RehearsalError(f"missing BuildKit vertex for {boundary}")
        vertex = match.group(1)
        related = [line for line in lines[marker_index:] if line.startswith(vertex + " ")]
        executed = any(
            re.match(rf"^{re.escape(vertex)} \d+(?:\.\d+)? CACHE_BOUNDARY={boundary}(?: |$)", line)
            for line in related
        )
        statuses[boundary] = "ran" if executed else "cached"
    return statuses


def _expected(first_rerun: str | None) -> dict[str, str]:
    if first_rerun is None:
        return {boundary: "cached" for boundary in BOUNDARIES}
    index = BOUNDARIES.index(first_rerun)
    return {
        boundary: "cached" if position < index else "ran"
        for position, boundary in enumerate(BOUNDARIES)
    }


def _assert_statuses(case: str, actual: dict[str, str], expected: dict[str, str]) -> None:
    if actual != expected:
        raise RehearsalError(f"{case}: expected {expected}, got {actual}")


def run_rehearsal(evidence_path: Path, repetitions: int) -> dict[str, Any]:
    if repetitions < 1:
        raise RehearsalError("repetitions must be positive")
    mapping = inspect_real_dockerfile(ROOT / "Dockerfile")
    token = uuid.uuid4().hex[:10]
    network = f"cache-rehearsal-{token}"
    registry = f"cache-registry-{token}"
    prefix = f"{registry}:5000/rehearsal"
    builders: list[str] = []
    evidence: dict[str, Any] = {
        "schemaVersion": "agora.cache-rehearsal.v1",
        "productionPerformanceClaim": False,
        "modeledLimits": [
            "heavyweight commands are replaced by marker writes",
            "the production base is replaced by two tiny locally published base digests",
            "hosted disk peaks, registry latency, and final image load/pull are not measured",
        ],
        "realDockerfileMapping": mapping,
        "generatedCandidateDockerfile": generated_dockerfile(),
        "generatedOldLayoutDockerfile": generated_dockerfile(old_layout=True),
        "cases": [],
    }
    with tempfile.TemporaryDirectory(prefix="agora-cache-rehearsal-") as temporary:
        root = Path(temporary)
        candidate = root / "candidate"
        old = root / "old"
        base = root / "base"
        candidate.mkdir()
        old.mkdir()
        base.mkdir()
        _write_fixture(candidate, generated_dockerfile())
        _write_fixture(old, generated_dockerfile(old_layout=True))
        (base / "Dockerfile").write_text(
            "ARG BUSYBOX_IMAGE\nFROM ${BUSYBOX_IMAGE}\nARG BASE_MARKER\n"
            "RUN echo $BASE_MARKER > /base-marker\n", encoding="utf-8"
        )
        config = root / "buildkitd.toml"
        config.write_text(
            f'[registry."{registry}:5000"]\n  http = true\n  insecure = true\n',
            encoding="utf-8",
        )
        _run(["docker", "network", "create", network], capture=True)
        try:
            _run(
                [
                    "docker", "run", "-d", "--name", registry, "--network", network,
                    "registry:2",
                ],
                capture=True,
            )
            seed_builder = f"cache-seed-{token}"
            builders.append(seed_builder)
            _builder(seed_builder, network, config)

            base_digests: list[str] = []
            for marker in ("base-a", "base-b"):
                metadata = root / f"{marker}.json"
                _, _, digest = _build(
                    builder=seed_builder,
                    context=base,
                    tag=f"{prefix}:{marker}",
                    cache_from=None,
                    arguments={"BUSYBOX_IMAGE": "busybox:1.36.1", "BASE_MARKER": marker},
                    metadata=metadata,
                )
                assert digest is not None
                base_digests.append(digest)

            defaults = {
                "UPSTREAM_IMAGE": f"{prefix}@{base_digests[0]}",
                "TARGETARCH": "amd64",
                "TRAINING_REPO_URL": "https://example.invalid/training.git",
                "TRAINING_REPO_REF": "training-v1",
                "PX0_VERSION": "0.1.6",
                "PX0_SHA256": "px0-v1",
                "FLEET_SOURCE_COMMIT": "fleet-v1",
                "FLEET_SOURCE_TREE": "tree-v1",
                "FLEET_SOURCE_ARTIFACT_FINGERPRINT": "artifact-v1",
            }

            seeds: dict[str, str] = {}
            for layout, context in (("candidate", candidate), ("old", old)):
                metadata = root / f"seed-{layout}.json"
                _, log, digest = _build(
                    builder=seed_builder,
                    context=context,
                    tag=f"{prefix}:{layout}-seed",
                    cache_from=None,
                    arguments=defaults,
                    metadata=metadata,
                    inline=True,
                )
                assert digest is not None
                seeds[layout] = f"type=registry,ref={prefix}@{digest}"
                evidence[f"{layout}SeedDigest"] = digest
                evidence[f"{layout}SeedStatuses"] = cache_statuses(log)

            cases = [
                ("unchanged-warm", candidate, seeds["candidate"], {}, None),
                ("runtime-only", candidate, seeds["candidate"], {"runtime": "runtime-v2"}, "runtime"),
                ("fleet-provenance-only", candidate, seeds["candidate"], {"FLEET_SOURCE_COMMIT": "fleet-v2"}, "runtime"),
                ("repair-requirements", candidate, seeds["candidate"], {"requirements": "repair-v2"}, "os"),
                ("training-ref", candidate, seeds["candidate"], {"TRAINING_REPO_REF": "training-v2"}, "training"),
                ("px0-version", candidate, seeds["candidate"], {"PX0_VERSION": "0.1.7"}, "px0"),
                ("base-digest", candidate, seeds["candidate"], {"UPSTREAM_IMAGE": f"{prefix}@{base_digests[1]}"}, "os"),
                ("missing-cache", candidate, None, {}, "os"),
                ("old-layout-fleet-negative", old, seeds["old"], {"FLEET_SOURCE_COMMIT": "fleet-v2"}, "os"),
            ]
            for case_name, context, cache_from, changes, first_rerun in cases:
                samples: list[float] = []
                statuses: list[dict[str, str]] = []
                for repetition in range(repetitions):
                    case_root = root / f"{case_name}-{repetition}"
                    shutil.copytree(context, case_root)
                    arguments = dict(defaults)
                    for key, value in changes.items():
                        if key == "runtime":
                            (case_root / "image-runtime/marker").write_text(value + "\n", encoding="utf-8")
                        elif key == "requirements":
                            (case_root / "image-repair-build-requirements.txt").write_text(value + "\n", encoding="utf-8")
                        else:
                            arguments[key] = value
                    builder = f"cache-{token}-{len(builders)}"
                    builders.append(builder)
                    _builder(builder, network, config)
                    seconds, log, _ = _build(
                        builder=builder,
                        context=case_root,
                        tag=None,
                        cache_from=cache_from,
                        arguments=arguments,
                    )
                    actual = cache_statuses(log)
                    try:
                        _assert_statuses(case_name, actual, _expected(first_rerun))
                    except RehearsalError as error:
                        raise RehearsalError(f"{error}\n--- BuildKit log ---\n{log}") from error
                    samples.append(round(seconds, 3))
                    statuses.append(actual)
                    _run(["docker", "buildx", "rm", "--force", builder], capture=True)
                    builders.remove(builder)
                evidence["cases"].append(
                    {
                        "name": case_name,
                        "samplesSeconds": samples,
                        "medianSeconds": round(statistics.median(samples), 3),
                        "rangeSeconds": [min(samples), max(samples)],
                        "statuses": statuses,
                    }
                )
        finally:
            for builder in reversed(builders):
                subprocess.run(
                    ["docker", "buildx", "rm", "--force", builder],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            subprocess.run(
                ["docker", "rm", "-f", registry],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["docker", "network", "rm", network],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    evidence["finishedAtEpoch"] = int(time.time())
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    arguments = parser.parse_args()
    run_rehearsal(arguments.evidence, arguments.repetitions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
