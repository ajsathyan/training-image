from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import resolve_registry_cache  # noqa: E402


class RegistryCacheTests(unittest.TestCase):
    def test_valid_digest_is_returned_as_an_immutable_reference(self) -> None:
        digest = "sha256:" + "a" * 64
        result = resolve_registry_cache.resolve(
            "example/image",
            0.1,
            runner=lambda *args, **kwargs: subprocess.CompletedProcess(
                args[0], 0, digest + "\n", ""
            ),
        )
        self.assertEqual(result["status"], "available")
        self.assertEqual(
            result["reference"], f"type=registry,ref=example/image@{digest}"
        )

    def test_timeout_and_failure_fall_back_to_no_cache(self) -> None:
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

        timed_out = resolve_registry_cache.resolve("example/image", 0.1, runner=timeout)
        self.assertEqual(timed_out["status"], "timeout")
        self.assertEqual(timed_out["reference"], "")

        failed = resolve_registry_cache.resolve(
            "example/image",
            0.1,
            runner=lambda *args, **kwargs: subprocess.CompletedProcess(
                args[0], 1, "", "denied"
            ),
        )
        self.assertEqual(failed["status"], "unavailable")
        self.assertEqual(failed["reference"], "")


class BuildContractTests(unittest.TestCase):
    def test_dockerfile_args_are_scoped_to_owned_layers_and_labels_are_last(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        markers = [
            "COPY image-repair-build-requirements.txt",
            "ARG TRAINING_REPO_URL",
            "RUN git clone",
            "ARG PX0_VERSION",
            "RUN curl -fsSLo /tmp/px0",
            "ARG FLEET_SOURCE_COMMIT",
            "COPY machine-runtime",
            "RUN chmod 755 /start.sh",
            "LABEL io.agora.image.platform",
            "WORKDIR /workspace",
        ]
        positions = [dockerfile.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))

    def test_workflow_uses_optional_digest_cache_and_inline_export(self) -> None:
        workflow = (ROOT / ".github/workflows/publish-image.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("tools/resolve_registry_cache.py", workflow)
        self.assertIn("cache-from: ${{ steps.cache_probe.outputs.cache_from }}", workflow)
        self.assertIn("cache-to: type=inline", workflow)
        self.assertNotIn("minimumFreeBytes", workflow)
        self.assertNotIn("prepare_hosted_runner.py", workflow)


if __name__ == "__main__":
    unittest.main()
