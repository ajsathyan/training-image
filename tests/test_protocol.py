from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "machine-sentinel"
sys.path.insert(0, str(RUNTIME_ROOT))

import agora_machine_sentinel_agent as agent
from machine_sentinel import CommandRejected, MachineSentinel, MemoryStateStore, initial_state
from machine_sentinel.client import SentinelIngressClient
from machine_sentinel.sentinel import ALLOWED_ACTIONS, _exact_arguments


NOW = "2026-09-02T12:00:00+00:00"


class FakeResponse:
    status = 200

    def __init__(self, value: dict):
        self.value = value

    def read(self, _limit: int = -1) -> bytes:
        return json.dumps(self.value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def identity() -> dict:
    return {
        "fleetId": "fleet-a",
        "launchId": "launch-a",
        "reservationId": "reservation-a",
        "slotId": "slot-a",
        "slotGeneration": 1,
        "machineGenerationId": "machine-generation-a",
        "machineId": "machine-a",
        "provider": "runpod",
        "providerResourceId": "pod-a",
        "nodeType": "tail",
        "gpuModel": "RTX PRO 6000",
        "bootId": "boot-a",
    }


class MachineSentinelProtocolTests(unittest.TestCase):
    def test_public_observation_url_uses_public_credential_siblings(self) -> None:
        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            return FakeResponse({"principalToken": "machine-token", "expiresAt": 1_900_000_000_000})

        client = SentinelIngressClient(
            "https://sentinel.example/api/machine-sentinel/observe",
            "bootstrap-token",
            opener=opener,
        )
        client.exchange_bootstrap(identity())
        client.refresh_machine_credential(identity())

        self.assertEqual(
            [request.full_url for request, _timeout in requests],
            [
                "https://sentinel.example/api/machine-sentinel/bootstrap",
                "https://sentinel.example/api/machine-sentinel/refresh",
            ],
        )

    def test_exact_action_set_rejects_arbitrary_shell_action_before_effect(self) -> None:
        self.assertEqual(
            ALLOWED_ACTIONS,
            {
                "prepare_setup",
                "start_training",
                "stop_training",
                "cancel_training",
                "repair_heartbeat",
                "apply_configuration",
            },
        )
        effects = {action: mock.Mock() for action in ALLOWED_ACTIONS}
        runtime = MachineSentinel(
            MemoryStateStore(
                initial_state(
                    reservation_id="reservation-a",
                    slot_generation=1,
                    machine_id="machine-a",
                    boot_id="boot-a",
                    setup_revision="setup-v1",
                    authority_epoch=7,
                )
            ),
            effects=effects,
        )
        command = {
            "commandId": "arbitrary-shell",
            "action": "shell",
            "scope": {
                "reservationId": "reservation-a",
                "slotGeneration": 1,
                "machineId": "machine-a",
                "bootId": "boot-a",
                "setupRevision": "setup-v1",
            },
            "authorityEpoch": 7,
            "issuedAt": "2026-09-02T11:59:00+00:00",
            "expiresAt": "2026-09-02T12:01:00+00:00",
            "arguments": {"command": "touch /tmp/forbidden"},
        }

        with self.assertRaises(CommandRejected) as raised:
            runtime.execute_command(command, now=NOW)

        self.assertEqual(raised.exception.code, "action_not_allowed")
        self.assertTrue(all(effect.call_count == 0 for effect in effects.values()))

    def test_effect_handler_passes_typed_fields_as_argv_without_shell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "sentinel-stop-training.sh"
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o700)
            arguments = {
                "trainingRunId": "run-a",
                "graceSeconds": 30,
                "reasonCode": "operator_requested",
                "approvalReceiptId": "approval-a",
            }
            completed = subprocess.CompletedProcess([], 0, "", "")
            with mock.patch.object(agent.subprocess, "run", return_value=completed) as runner:
                result = agent._run_effect(root, "stop_training", arguments)

        self.assertEqual(result, {"returncode": 0, "action": "stop_training"})
        command = runner.call_args.args[0]
        self.assertEqual(
            command,
            [str(script), "run-a", "30", "operator_requested", "approval-a"],
        )
        self.assertNotIn("shell", runner.call_args.kwargs)

    def test_every_action_has_an_exact_typed_argument_schema(self) -> None:
        examples = {
            "prepare_setup": {"setupProfileId": "profile-a", "setupRevision": "setup-v1"},
            "start_training": {
                "trainingRunId": "run-a",
                "trainingPlanId": "plan-a",
                "configurationRevision": "config-v1",
                "announcePort": 49200,
            },
            "stop_training": {
                "trainingRunId": "run-a",
                "graceSeconds": 30,
                "reasonCode": "operator_requested",
                "approvalReceiptId": "approval-a",
            },
            "cancel_training": {
                "trainingRunId": "run-a",
                "reasonCode": "operator_requested",
                "approvalReceiptId": "approval-a",
            },
            "repair_heartbeat": {"trainingRunId": "run-a", "watchdogRevision": "watchdog-v2"},
            "apply_configuration": {
                "configurationId": "config-a",
                "fromRevision": "config-v1",
                "toRevision": "config-v2",
                "approvalReceiptId": "approval-a",
                "settings": {"announcePort": 49200, "nodeType": "tail"},
            },
        }

        self.assertEqual(set(examples), ALLOWED_ACTIONS)
        for action, arguments in examples.items():
            with self.subTest(action=action):
                self.assertEqual(_exact_arguments(action, arguments), arguments)
                with self.assertRaises(CommandRejected):
                    _exact_arguments(action, {**arguments, "command": "touch /tmp/forbidden"})

    def test_apply_configuration_is_atomic_python_data_update_without_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agora.env").write_text("UNCHANGED=value\n", encoding="utf-8")
            with mock.patch.object(agent.subprocess, "run") as runner:
                result = agent._run_effect(
                    root,
                    "apply_configuration",
                    {
                        "configurationId": "config-a",
                        "fromRevision": "config-v1",
                        "toRevision": "config-v2",
                        "approvalReceiptId": "approval-a",
                        "settings": {"announcePort": 49200, "nodeType": "tail"},
                    },
                )

            persisted = agent.parse_env_file(root / "agora.env")
            self.assertEqual(persisted["UNCHANGED"], "value")
            self.assertEqual(persisted["ANNOUNCE_PORT"], "49200")
            self.assertEqual(persisted["AGORA_NODE_TYPE"], "tail")
            self.assertEqual(result["restartRequested"], False)
            runner.assert_not_called()

    def test_bootstrap_exchange_clears_one_time_token_and_persists_runtime_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            credential_path = Path(directory) / "credential.env"
            exchange_client = mock.Mock(
                exchange_bootstrap=mock.Mock(
                    return_value={"principalToken": "machine-token", "expiresAt": 1_900_000_000_000}
                )
            )
            with mock.patch.object(agent, "SentinelIngressClient", return_value=exchange_client):
                secrets = agent.ensure_machine_credential(
                    "https://sentinel.example/api/machine-sentinel/observe",
                    identity(),
                    credential_path,
                    agent.parse_env_file(credential_path),
                    5,
                    "bootstrap-token",
                )
            bundle_client = mock.Mock(
                machine_runtime_bundle=mock.Mock(
                    return_value={
                        "kind": "machine_runtime_bundle",
                        "tokenLabel": "alpha",
                        "huggingFaceToken": "hf_private",
                        "machineSigningSecret": "s" * 32,
                        "authorityEpoch": 7,
                        "credentialGeneration": 3,
                    }
                )
            )
            bundle_identity = {**identity(), "authorityEpoch": 7}
            secrets = agent.ensure_runtime_secret_bundle(
                bundle_client,
                bundle_identity,
                "alpha",
                credential_path,
                secrets,
            )

            persisted = credential_path.read_text(encoding="utf-8")
            self.assertNotIn("bootstrap-token", persisted)
            self.assertIn("AGORA_SENTINEL_MACHINE_TOKEN=machine-token", persisted)
            self.assertIn("HF_TOKEN=hf_private", persisted)
            self.assertEqual(secrets["AGORA_SENTINEL_RUNTIME_TOKEN_LABEL"], "alpha")
            self.assertEqual(credential_path.stat().st_mode & 0o777, 0o600)

    def test_failed_bootstrap_exchange_never_persists_the_one_time_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            credential_path = Path(directory) / "credential.env"
            exchange_client = mock.Mock(
                exchange_bootstrap=mock.Mock(side_effect=RuntimeError("offline"))
            )
            with mock.patch.object(agent, "SentinelIngressClient", return_value=exchange_client):
                with self.assertRaisesRegex(RuntimeError, "offline"):
                    agent.ensure_machine_credential(
                        "https://sentinel.example/api/machine-sentinel/observe",
                        identity(),
                        credential_path,
                        {},
                        5,
                        "bootstrap-token",
                    )
            self.assertFalse(credential_path.exists())


if __name__ == "__main__":
    unittest.main()
