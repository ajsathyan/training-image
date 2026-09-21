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
import prepare_hosted_runner as prepare  # noqa: E402


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


class BuildEvidenceTests(unittest.TestCase):
    def test_checkpoint_labels_phase_boundary_not_transient_peak(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkpoint = build_evidence._checkpoint("post-build", [Path(temporary)])
        self.assertIn("not a transient peak", checkpoint["note"])
        self.assertEqual(checkpoint["filesystems"][0]["status"] if "status" in checkpoint["filesystems"][0] else "present", "present")


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
        self.assertIn("type=registry,ref=${IMAGE_NAME}@${digest}", workflow)
        self.assertIn("cache-to: type=inline", workflow)
        self.assertIn("runner-preparation-evidence.json", workflow)
        self.assertIn("image-build-timing-evidence.json", workflow)

    def test_rehearsal_mapping_is_bound_to_real_dockerfile(self) -> None:
        mapping = cache_rehearsal.inspect_real_dockerfile(ROOT / "Dockerfile")
        self.assertEqual([item["boundary"] for item in mapping], list(cache_rehearsal.LANDMARKS))
        generated = cache_rehearsal.generated_dockerfile()
        old = cache_rehearsal.generated_dockerfile(old_layout=True)
        self.assertLess(generated.index("ARG TRAINING_REPO_URL"), generated.index("CACHE_BOUNDARY=training"))
        self.assertGreater(generated.index("ARG FLEET_SOURCE_COMMIT"), generated.index("CACHE_BOUNDARY=px0"))
        self.assertLess(old.index("ARG FLEET_SOURCE_COMMIT"), old.index("CACHE_BOUNDARY=os"))


if __name__ == "__main__":
    unittest.main()
