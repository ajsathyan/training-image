#!/usr/bin/env python3
"""Exercise the Dockerfile cache graph with tiny, structure-bound fixtures.

This is not a production-image benchmark.  It deterministically maps the real
Dockerfile's ARG/COPY/RUN boundaries onto marker-producing lightweight steps,
then uses an inline cache, a disposable registry, and fresh BuildKit builders.
"""

from __future__ import annotations

import argparse
import hashlib
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

try:
    from parse_buildkit_progress import parse_progress
except ModuleNotFoundError:  # Imported as tools.cache_rehearsal in source checks.
    from tools.parse_buildkit_progress import parse_progress


ROOT = Path(__file__).resolve().parents[1]
BOUNDARIES = ("os", "repair", "training", "px0", "runtime")
EXPECTED_STRUCTURE = (
    ("ARG", "UPSTREAM_IMAGE"),
    ("FROM", "${UPSTREAM_IMAGE}"),
    ("ARG", "TARGETARCH"),
    ("ENV", "DEBIAN_FRONTEND"),
    ("COPY", "image-repair-build-requirements.txt"),
    ("RUN", "os"),
    ("RUN", "repair"),
    ("ARG", "TRAINING_REPO_URL"),
    ("ARG", "TRAINING_REPO_REF"),
    ("RUN", "training"),
    ("ARG", "PX0_VERSION"),
    ("ARG", "PX0_SHA256"),
    ("RUN", "px0"),
    ("ARG", "FLEET_SOURCE_COMMIT"),
    ("ARG", "FLEET_SOURCE_TREE"),
    ("ARG", "FLEET_SOURCE_ARTIFACT_FINGERPRINT"),
    ("COPY", "machine-runtime"),
    ("COPY", "image-runtime"),
    ("COPY", "start.sh"),
    ("RUN", "runtime"),
    ("LABEL", "io.agora.image.platform"),
    ("WORKDIR", "/workspace"),
    ("EXPOSE", "22"),
    ("CMD", '["/start.sh"]'),
)
EARLY_LAYOUT_ARGS = {
    "TRAINING_REPO_URL",
    "TRAINING_REPO_REF",
    "PX0_VERSION",
    "PX0_SHA256",
    "FLEET_SOURCE_COMMIT",
    "FLEET_SOURCE_TREE",
    "FLEET_SOURCE_ARTIFACT_FINGERPRINT",
}


class RehearsalError(RuntimeError):
    pass


def _parse_instructions(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    instructions: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        match = re.match(r"^([A-Z]+)\s+(.+)$", lines[index])
        if not match:
            raise RehearsalError(f"unparsed Dockerfile line {index + 1}: {lines[index]}")
        keyword = match.group(1)
        start = index
        raw = [lines[index]]
        while raw[-1].rstrip().endswith("\\"):
            index += 1
            if index >= len(lines):
                raise RehearsalError(f"unterminated continuation at line {start + 1}")
            raw.append(lines[index])
        heredoc = re.search(r"<<-?['\"]?([A-Za-z0-9_]+)['\"]?", "\n".join(raw))
        if heredoc:
            delimiter = heredoc.group(1)
            while index + 1 < len(lines):
                index += 1
                raw.append(lines[index])
                if lines[index].strip() == delimiter:
                    break
            else:
                raise RehearsalError(f"unterminated heredoc at line {start + 1}")
        instructions.append(
            {
                "keyword": keyword,
                "raw": "\n".join(raw),
                "startLine": start + 1,
                "endLine": index + 1,
            }
        )
        index += 1
    return instructions


def _identity(instruction: dict[str, Any]) -> tuple[str, str]:
    keyword = instruction["keyword"]
    raw = instruction["raw"]
    first = raw.splitlines()[0].split(None, 1)[1]
    if keyword == "ARG":
        return keyword, first.split("=", 1)[0]
    if keyword == "COPY":
        return keyword, first.split()[0]
    if keyword == "RUN":
        if 'test "$TARGETARCH"' in raw:
            return keyword, "os"
        if "uv pip install" in raw:
            return keyword, "repair"
        if "git clone" in raw:
            return keyword, "training"
        if "curl -fsSLo /tmp/px0" in raw:
            return keyword, "px0"
        if "chmod 755 /start.sh" in raw:
            return keyword, "runtime"
        raise RehearsalError(f"unmapped RUN at line {instruction['startLine']}")
    if keyword in {"ENV", "LABEL"}:
        return keyword, first.split()[0].split("=", 1)[0]
    return keyword, first.split()[0] if keyword == "EXPOSE" else first


def inspect_real_dockerfile(path: Path) -> list[dict[str, Any]]:
    instructions = _parse_instructions(path)
    actual = [_identity(instruction) for instruction in instructions]
    if actual != list(EXPECTED_STRUCTURE):
        raise RehearsalError(
            "real Dockerfile instruction structure drifted:\n"
            f"expected={list(EXPECTED_STRUCTURE)!r}\nactual={actual!r}"
        )
    mapping: list[dict[str, Any]] = []
    for instruction, identity in zip(instructions, actual):
        mapping.append(
            {
                "instruction": identity[0],
                "input": identity[1],
                "startLine": instruction["startLine"],
                "endLine": instruction["endLine"],
                "sourceSha256": hashlib.sha256(instruction["raw"].encode()).hexdigest(),
                "modeledBoundary": _modeled_boundary(identity),
            }
        )
    return mapping


def _modeled_boundary(identity: tuple[str, str]) -> str:
    keyword, value = identity
    if keyword in {"FROM"} or value == "UPSTREAM_IMAGE":
        return "base"
    if value in {"TARGETARCH", "DEBIAN_FRONTEND", "image-repair-build-requirements.txt"}:
        return "os"
    if value in {"os", "repair"}:
        return value
    if value.startswith("TRAINING_") or value == "training":
        return "training"
    if value.startswith("PX0_") or value == "px0":
        return "px0"
    if value.startswith("FLEET_") or value in {
        "machine-runtime", "image-runtime", "start.sh", "runtime",
        "io.agora.image.platform", "/workspace", "22", '["/start.sh"]',
    }:
        return "runtime"
    raise RehearsalError(f"unmapped instruction identity: {identity}")


def _emit(instruction: dict[str, Any]) -> str:
    keyword, value = _identity(instruction)
    if keyword == "ARG":
        return f"ARG {value}"
    if keyword == "FROM":
        return "FROM ${UPSTREAM_IMAGE}"
    if keyword == "ENV":
        return "ENV REHEARSAL_ENV=1"
    if keyword == "COPY":
        destinations = {
            "image-repair-build-requirements.txt": "/inputs/requirements.txt",
            "machine-runtime": "/inputs/machine-runtime",
            "image-runtime": "/inputs/image-runtime",
            "start.sh": "/inputs/start.sh",
        }
        return f"COPY {value} {destinations[value]}"
    if keyword == "RUN":
        commands = {
            "os": 'RUN echo CACHE_BOUNDARY=os "$TARGETARCH" | tee /marker-os && cat /inputs/requirements.txt >/dev/null',
            "repair": "RUN echo CACHE_BOUNDARY=repair | tee /marker-repair && cat /inputs/requirements.txt >/dev/null",
            "training": 'RUN echo CACHE_BOUNDARY=training "$TRAINING_REPO_URL" "$TRAINING_REPO_REF" | tee /marker-training',
            "px0": 'RUN echo CACHE_BOUNDARY=px0 "$PX0_VERSION" "$PX0_SHA256" | tee /marker-px0',
            "runtime": 'RUN echo CACHE_BOUNDARY=runtime "$FLEET_SOURCE_COMMIT" "$FLEET_SOURCE_TREE" "$FLEET_SOURCE_ARTIFACT_FINGERPRINT" | tee /marker-runtime && cat /inputs/machine-runtime/marker /inputs/image-runtime/marker /inputs/start.sh >/dev/null',
        }
        return commands[value]
    if keyword == "LABEL":
        return 'LABEL rehearsal.training="$TRAINING_REPO_REF" rehearsal.px0="$PX0_VERSION" rehearsal.fleet="$FLEET_SOURCE_COMMIT"'
    if keyword == "WORKDIR":
        return "WORKDIR /workspace"
    if keyword == "EXPOSE":
        return "EXPOSE 22 49200"
    if keyword == "CMD":
        return 'CMD ["sh"]'
    raise RehearsalError(f"cannot emit instruction: {(keyword, value)}")


def generated_dockerfile(path: Path = ROOT / "Dockerfile", *, old_layout: bool = False) -> str:
    instructions = _parse_instructions(path)
    inspect_real_dockerfile(path)
    if old_layout:
        early = [item for item in instructions if _identity(item) == ("ARG", "TARGETARCH")]
        moved = [item for item in instructions if _identity(item)[0] == "ARG" and _identity(item)[1] in EARLY_LAYOUT_ARGS]
        prefix = instructions[:2]
        remaining = [item for item in instructions[2:] if item not in early and item not in moved]
        instructions = prefix + early + moved + remaining
    emitted = [_emit(instruction) for instruction in instructions]
    return "# generated from every validated real Dockerfile instruction\n" + "\n".join(emitted) + "\n"


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


def _builder(name: str, network: str, config: Path) -> dict[str, Any]:
    command = [
        "docker", "buildx", "create", "--name", name, "--driver", "docker-container",
        "--driver-opt", f"network={network}", "--buildkitd-config", str(config), "--use",
    ]
    started = time.monotonic()
    created = _run(command, capture=True)
    inspect_command = ["docker", "buildx", "inspect", "--builder", name, "--bootstrap"]
    inspected = _run(inspect_command, capture=True)
    return {
        "seconds": round(time.monotonic() - started, 3),
        "createCommand": command,
        "inspectCommand": inspect_command,
        "createLog": created.stdout,
        "inspectLog": inspected.stdout,
    }


def _build(
    *, builder: str, context: Path, tag: str | None, cache_from: str | None,
    arguments: dict[str, str], metadata: Path | None = None, inline: bool = False,
) -> dict[str, Any]:
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
    completed = subprocess.run(
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    seconds = round(time.monotonic() - started, 3)
    digest = None
    metadata_value = None
    if metadata and metadata.exists():
        metadata_value = json.loads(metadata.read_text(encoding="utf-8"))
        digest = metadata_value.get("containerimage.digest")
    return {
        "command": command,
        "exitStatus": completed.returncode,
        "seconds": seconds,
        "log": completed.stdout,
        "metadata": metadata_value,
        "digest": digest,
        "phases": parse_progress(completed.stdout),
    }


def _require_build(name: str, result: dict[str, Any]) -> None:
    if result["exitStatus"] != 0:
        raise RehearsalError(
            f"{name} build failed with exit {result['exitStatus']}\n{result['log']}"
        )


def _write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


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
        explicit_cached = any(line == f"{vertex} CACHED" for line in related)
        lazy_cached = any(" sha256:" in line for line in related) and any(
            line.startswith(f"{vertex} DONE ") for line in related
        )
        if executed:
            statuses[boundary] = "ran"
        elif explicit_cached or lazy_cached:
            statuses[boundary] = "cached"
        else:
            raise RehearsalError(
                f"BuildKit vertex for {boundary} has no execution or cache completion oracle"
            )
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
        "status": "running",
        "stage": "initialize",
        "startedAtEpoch": int(time.time()),
        "productionPerformanceClaim": False,
        "modeledLimits": [
            "heavyweight commands are replaced by marker writes",
            "the production base is replaced by two tiny locally published base digests",
            "hosted disk peaks, registry latency, and final image load/pull are not measured",
        ],
        "realDockerfileMapping": mapping,
        "generatedCandidateDockerfile": generated_dockerfile(),
        "generatedOldLayoutDockerfile": generated_dockerfile(old_layout=True),
        "inputIdentity": {
            "sourceCommit": _run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture=True).stdout.strip(),
            "sourceTree": _run(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, capture=True).stdout.strip(),
            "dockerfileSha256": hashlib.sha256((ROOT / "Dockerfile").read_bytes()).hexdigest(),
            "dockerVersion": _run(["docker", "version", "--format", "{{.Client.Version}}/{{.Server.Version}}"], capture=True).stdout.strip(),
            "buildxVersion": _run(["docker", "buildx", "version"], capture=True).stdout.strip(),
        },
        "baseBuilds": [],
        "seedBuilds": [],
        "cases": [],
    }
    _write_evidence(evidence_path, evidence)
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
        network_command = ["docker", "network", "create", network]
        setup_started = time.monotonic()
        network_result = _run(network_command, capture=True)
        try:
            registry_command = [
                "docker", "run", "-d", "--name", registry, "--network", network,
                "registry:2",
            ]
            registry_result = _run(registry_command, capture=True)
            evidence["registrySetup"] = {
                "seconds": round(time.monotonic() - setup_started, 3),
                "networkCommand": network_command,
                "networkLog": network_result.stdout,
                "registryCommand": registry_command,
                "registryLog": registry_result.stdout,
            }
            _write_evidence(evidence_path, evidence)
            seed_builder = f"cache-seed-{token}"
            builders.append(seed_builder)
            evidence["stage"] = "seed-builder-setup"
            evidence["seedBuilderSetup"] = _builder(seed_builder, network, config)
            _write_evidence(evidence_path, evidence)

            base_digests: list[str] = []
            for marker in ("base-a", "base-b"):
                metadata = root / f"{marker}.json"
                evidence["stage"] = f"build-{marker}"
                result = _build(
                    builder=seed_builder,
                    context=base,
                    tag=f"{prefix}:{marker}",
                    cache_from=None,
                    arguments={"BUSYBOX_IMAGE": "busybox:1.36.1", "BASE_MARKER": marker},
                    metadata=metadata,
                )
                evidence["baseBuilds"].append({"name": marker, **result})
                _write_evidence(evidence_path, evidence)
                _require_build(marker, result)
                if not result["digest"]:
                    raise RehearsalError(f"{marker} did not produce a digest")
                base_digests.append(result["digest"])

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
                evidence["stage"] = f"seed-{layout}"
                result = _build(
                    builder=seed_builder,
                    context=context,
                    tag=f"{prefix}:{layout}-seed",
                    cache_from=None,
                    arguments=defaults,
                    metadata=metadata,
                    inline=True,
                )
                evidence["seedBuilds"].append({"name": layout, **result})
                _write_evidence(evidence_path, evidence)
                _require_build(f"seed-{layout}", result)
                if not result["digest"]:
                    raise RehearsalError(f"seed-{layout} did not produce a digest")
                seeds[layout] = f"type=registry,ref={prefix}@{result['digest']}"
                evidence[f"{layout}SeedDigest"] = result["digest"]
                evidence[f"{layout}SeedStatuses"] = cache_statuses(result["log"])

            cases = [
                ("unchanged-warm", candidate, seeds["candidate"], {}, None),
                ("runtime-only", candidate, seeds["candidate"], {"runtime": "runtime-v2"}, "runtime"),
                ("fleet-provenance-only", candidate, seeds["candidate"], {"FLEET_SOURCE_COMMIT": "fleet-v2"}, "runtime"),
                ("repair-requirements", candidate, seeds["candidate"], {"requirements": "repair-v2"}, "os"),
                ("training-ref", candidate, seeds["candidate"], {"TRAINING_REPO_REF": "training-v2"}, "training"),
                ("px0-version", candidate, seeds["candidate"], {"PX0_VERSION": "0.1.7"}, "px0"),
                ("px0-checksum", candidate, seeds["candidate"], {"PX0_SHA256": "px0-v2"}, "px0"),
                ("base-digest", candidate, seeds["candidate"], {"UPSTREAM_IMAGE": f"{prefix}@{base_digests[1]}"}, "os"),
                ("missing-cache", candidate, None, {}, "os"),
                ("unavailable-cache-reference", candidate, f"type=registry,ref={prefix}@sha256:{'0' * 64}", {}, "os"),
                ("old-layout-fleet-negative", old, seeds["old"], {"FLEET_SOURCE_COMMIT": "fleet-v2"}, "os"),
            ]
            for case_name, context, cache_from, changes, first_rerun in cases:
                samples: list[float] = []
                build_samples: list[float] = []
                setup_samples: list[float] = []
                statuses: list[dict[str, str]] = []
                runs: list[dict[str, Any]] = []
                case_evidence: dict[str, Any] = {
                    "name": case_name,
                    "status": "running",
                    "runs": runs,
                }
                evidence["cases"].append(case_evidence)
                _write_evidence(evidence_path, evidence)
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
                    evidence["stage"] = f"{case_name}:{repetition}:builder-setup"
                    setup = _builder(builder, network, config)
                    evidence["stage"] = f"{case_name}:{repetition}:build"
                    result = _build(
                        builder=builder,
                        context=case_root,
                        tag=None,
                        cache_from=cache_from,
                        arguments=arguments,
                    )
                    runs.append({"repetition": repetition, "builderSetup": setup, "build": result})
                    _write_evidence(evidence_path, evidence)
                    _require_build(case_name, result)
                    actual = cache_statuses(result["log"])
                    try:
                        _assert_statuses(case_name, actual, _expected(first_rerun))
                    except RehearsalError as error:
                        raise RehearsalError(f"{error}\n--- BuildKit log ---\n{result['log']}") from error
                    total_seconds = round(setup["seconds"] + result["seconds"], 3)
                    samples.append(total_seconds)
                    build_samples.append(result["seconds"])
                    setup_samples.append(setup["seconds"])
                    statuses.append(actual)
                    _run(["docker", "buildx", "rm", "--force", builder], capture=True)
                    builders.remove(builder)
                case_evidence.update(
                    {
                        "status": "passed",
                        "samplesSecondsIncludingBuilderSetup": samples,
                        "buildSamplesSeconds": build_samples,
                        "builderSetupSamplesSeconds": setup_samples,
                        "medianSecondsIncludingBuilderSetup": round(statistics.median(samples), 3),
                        "rangeSecondsIncludingBuilderSetup": [min(samples), max(samples)],
                        "statuses": statuses,
                    }
                )
                _write_evidence(evidence_path, evidence)
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
    evidence["status"] = "passed"
    evidence["stage"] = "complete"
    evidence["finishedAtEpoch"] = int(time.time())
    _write_evidence(evidence_path, evidence)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    arguments = parser.parse_args()
    try:
        run_rehearsal(arguments.evidence, arguments.repetitions)
    except BaseException as error:
        if arguments.evidence.exists():
            evidence = json.loads(arguments.evidence.read_text(encoding="utf-8"))
        else:
            evidence = {"schemaVersion": "agora.cache-rehearsal.v1"}
        evidence["status"] = "failed"
        evidence["failedStage"] = evidence.get("stage", "initialize")
        evidence["errorType"] = type(error).__name__
        evidence["error"] = str(error)
        evidence["finishedAtEpoch"] = int(time.time())
        _write_evidence(arguments.evidence, evidence)
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
