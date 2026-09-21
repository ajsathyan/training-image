from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_evidence  # noqa: E402
import cache_rehearsal  # noqa: E402
import parse_buildkit_progress  # noqa: E402
import prepare_hosted_runner as prepare  # noqa: E402
import resolve_registry_cache  # noqa: E402


class HostedRunnerPreparationTests(unittest.TestCase):
    def test_cleanup_allowlist_excludes_toolcache_and_is_ordered(self) -> None:
        self.assertNotIn(Path("/opt/hostedtoolcache"), prepare.CLEANUP_ALLOWLIST)
        present = {Path("/opt/ghc"), Path("/usr/local/lib/android")}
        self.assertEqual(
            prepare.cleanup_candidates(present),
            [Path("/usr/local/lib/android"), Path("/opt/ghc")],
        )

    def test_identity_guard_rejects_non_hosted_and_self_hosted(self) -> None:
        baseline = {
            "GITHUB_ACTIONS": "true",
            "RUNNER_ENVIRONMENT": "github-hosted",
            "RUNNER_OS": "Linux",
        }
        with mock.patch.object(prepare.sys, "platform", "linux"), mock.patch.object(
            prepare.platform, "system", return_value="Linux"
        ):
            self.assertTrue(prepare.is_github_hosted_linux(baseline))
            for key, value in (
                ("GITHUB_ACTIONS", "false"),
                ("RUNNER_ENVIRONMENT", "self-hosted"),
                ("RUNNER_OS", "macOS"),
            ):
                changed = dict(baseline)
                changed[key] = value
                self.assertFalse(prepare.is_github_hosted_linux(changed))

    def test_unique_device_budget_uses_lowest_observation(self) -> None:
        enough = prepare.MIN_FREE_BYTES + 1
        low = prepare.MIN_FREE_BYTES - 1
        measurements = [
            {"deviceId": 1, "freeBytes": enough, "mount": "/"},
            {"deviceId": 1, "freeBytes": low, "mount": "/mnt"},
            {"deviceId": 2, "freeBytes": enough, "mount": "/workspace"},
        ]
        self.assertEqual(prepare.deficient_devices(measurements), [measurements[1]])

    def test_foreign_or_nested_mount_refusal_precedes_every_destructive_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary) / "evidence.json"
            hosted = {
                "GITHUB_ACTIONS": "true",
                "RUNNER_ENVIRONMENT": "github-hosted",
                "RUNNER_OS": "Linux",
            }
            with mock.patch.dict(os.environ, hosted), mock.patch.object(
                prepare, "_assert_safe_host"
            ), mock.patch.object(
                prepare, "_preflight_layout", side_effect=prepare.PreparationError("foreign mount")
            ), mock.patch.object(prepare, "_run") as destructive:
                with self.assertRaisesRegex(prepare.PreparationError, "foreign mount"):
                    prepare.prepare(evidence, Path(temporary) / "github-env")
            destructive.assert_not_called()
            retained = json.loads(evidence.read_text(encoding="utf-8"))
            self.assertEqual(retained["failedStage"], "identity_guard")
            self.assertEqual(retained["status"], "failed")

    def test_mutation_target_rejects_nested_mount(self) -> None:
        target = Path("/")
        with self.assertRaisesRegex(prepare.PreparationError, "contains a mountpoint"):
            prepare._validate_mutation_target(
                target,
                root_device=target.stat().st_dev,
                mountpoints=[Path("/nested")],
            )


class BuildEvidenceTests(unittest.TestCase):
    def test_checkpoint_labels_phase_boundary_not_transient_peak(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = build_evidence._checkpoint("post-build", [Path(temporary)])
        self.assertIn("not a transient peak", checkpoint["note"])
        self.assertEqual(checkpoint["filesystems"][0]["status"] if "status" in checkpoint["filesystems"][0] else "present", "present")

    def test_buildkit_phases_are_separated(self) -> None:
        parsed = parse_buildkit_progress.parse_progress(
            "\n".join(
                [
                    "#1 [internal] load metadata for example/base@sha256:abc",
                    "#1 DONE 2.0s",
                    "#2 importing cache manifest from example/cache@sha256:def",
                    "#2 DONE 0.4s",
                    "#3 [2/3] RUN echo work",
                    "#3 DONE 1.5s",
                    "#4 exporting to docker image format",
                    "#4 DONE 3.0s",
                    "#5 exporting cache to client directory",
                    "#5 DONE 0.2s",
                ]
            )
        )
        self.assertEqual(
            parsed["categories"],
            {
                "baseDownloadSeconds": 2.0,
                "cacheImportSeconds": 0.4,
                "dockerfileExecutionSeconds": 1.5,
                "outputExportSeconds": 3.0,
                "cacheExportSeconds": 0.2,
                "unclassifiedSeconds": 0.0,
            },
        )

    def test_cache_probe_timeout_and_failure_fall_back_cold(self) -> None:
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

        timed_out = resolve_registry_cache.resolve("example/image", 0.1, runner=timeout)
        self.assertEqual(timed_out["status"], "timeout")
        self.assertEqual(timed_out["exitStatus"], 124)
        failed = resolve_registry_cache.resolve(
            "example/image",
            0.1,
            runner=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "denied"),
        )
        self.assertEqual(failed["status"], "unavailable")
        self.assertEqual(failed["reference"], "")


class DockerfileCacheContractTests(unittest.TestCase):
    def test_args_are_declared_at_owned_boundaries_and_labels_are_last(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        repair = dockerfile.index("COPY image-repair-build-requirements.txt")
        training_arg = dockerfile.index("ARG TRAINING_REPO_URL")
        training_run = dockerfile.index("RUN git clone")
        px0_arg = dockerfile.index("ARG PX0_VERSION")
        px0_run = dockerfile.index("RUN curl -fsSLo /tmp/px0")
        fleet_arg = dockerfile.index("ARG FLEET_SOURCE_COMMIT")
        runtime_copy = dockerfile.index("COPY machine-runtime")
        final_run = dockerfile.index("RUN chmod 755 /start.sh")
        labels = dockerfile.index("LABEL io.agora.image.platform")
        workdir = dockerfile.index("WORKDIR /workspace")
        self.assertLess(repair, training_arg)
        self.assertLess(training_arg, training_run)
        self.assertLess(training_run, px0_arg)
        self.assertLess(px0_arg, px0_run)
        self.assertLess(px0_run, fleet_arg)
        self.assertLess(fleet_arg, runtime_copy)
        self.assertLess(runtime_copy, final_run)
        self.assertLess(final_run, labels)
        self.assertLess(labels, workdir)

    def test_workflow_uses_resolved_digest_and_inline_export(self) -> None:
        workflow = (ROOT / ".github/workflows/publish-image.yml").read_text(encoding="utf-8")
        self.assertIn("RUNNER_ENVIRONMENT", workflow)
        self.assertIn("tools/resolve_registry_cache.py", workflow)
        resolver = (ROOT / "tools/resolve_registry_cache.py").read_text(encoding="utf-8")
        self.assertIn('type=registry,ref={image}@{digest}', resolver)
        self.assertIn("--cache-to type=inline", workflow)
        self.assertIn("runner-preparation-evidence.json", workflow)
        self.assertIn("image-build-timing-evidence.json", workflow)

    def test_rehearsal_mapping_is_bound_to_real_dockerfile(self) -> None:
        mapping = cache_rehearsal.inspect_real_dockerfile(ROOT / "Dockerfile")
        self.assertEqual(
            [(item["instruction"], item["input"]) for item in mapping],
            list(cache_rehearsal.EXPECTED_STRUCTURE),
        )
        generated = cache_rehearsal.generated_dockerfile()
        old = cache_rehearsal.generated_dockerfile(old_layout=True)
        self.assertLess(generated.index("ARG TRAINING_REPO_URL"), generated.index("CACHE_BOUNDARY=training"))
        self.assertGreater(generated.index("ARG FLEET_SOURCE_COMMIT"), generated.index("CACHE_BOUNDARY=px0"))
        self.assertLess(old.index("ARG FLEET_SOURCE_COMMIT"), old.index("CACHE_BOUNDARY=os"))

    def test_rehearsal_rejects_early_px0_arg_and_missing_runtime_copy(self) -> None:
        original = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        mutations = [
            original.replace("ARG PX0_SHA256=d4f2378a1d6fbda9960cc7da45a5e3b5a5f9f6b331be80bcbb8a27f9dc5e9e0c\n", "").replace(
                "COPY image-repair-build-requirements.txt",
                "ARG PX0_SHA256=d4f2378a1d6fbda9960cc7da45a5e3b5a5f9f6b331be80bcbb8a27f9dc5e9e0c\n\nCOPY image-repair-build-requirements.txt",
            ),
            original.replace("COPY image-runtime /opt/agora-image-runtime\n", ""),
        ]
        for mutation in mutations:
            with self.subTest():
                with tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / "Dockerfile"
                    path.write_text(mutation, encoding="utf-8")
                    with self.assertRaisesRegex(cache_rehearsal.RehearsalError, "structure drifted"):
                        cache_rehearsal.inspect_real_dockerfile(path)

    def test_cache_status_requires_a_behavioral_completion_oracle(self) -> None:
        headers = "\n".join(
            f"#{index} [{index}/5] RUN echo CACHE_BOUNDARY={boundary}"
            for index, boundary in enumerate(cache_rehearsal.BOUNDARIES, start=1)
        )
        with self.assertRaisesRegex(cache_rehearsal.RehearsalError, "no execution or cache"):
            cache_rehearsal.cache_statuses(headers)


if __name__ == "__main__":
    unittest.main()
