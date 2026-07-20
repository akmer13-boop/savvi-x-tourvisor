import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.operator_policy import (
    OperatorPolicyConfigurationError,
    load_operator_policy,
)


class OperatorPolicyTest(unittest.TestCase):
    def _write(self, payload: dict) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "operators.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_only_active_contract_ids_are_allowed(self):
        path = self._write(
            {
                "version": "2026-07-20.1",
                "operators": [
                    {"tourvisor_id": 13, "name": "Anex", "status": "active_contract"},
                    {"tourvisor_id": 53, "name": "Арт-Тур", "status": "approved_to_contract"},
                    {"tourvisor_id": 36, "name": "PAC GROUP", "status": "blocked"},
                ],
            }
        )
        policy = load_operator_policy(path, required=True)
        self.assertEqual(policy.active_ids, frozenset({13}))
        self.assertEqual(policy.active_count, 1)
        self.assertEqual(len(policy.short_hash), 12)

    def test_required_policy_rejects_empty_active_list(self):
        path = self._write({"version": "v1", "operators": []})
        with self.assertRaises(OperatorPolicyConfigurationError):
            load_operator_policy(path, required=True)

    def test_required_policy_rejects_duplicate_ids(self):
        path = self._write(
            {
                "version": "v1",
                "operators": [
                    {"tourvisor_id": 25, "name": "FUN&SUN", "status": "active_contract"},
                    {"tourvisor_id": 25, "name": "TUI", "status": "active_contract"},
                ],
            }
        )
        with self.assertRaises(OperatorPolicyConfigurationError):
            load_operator_policy(path, required=True)

    def test_optional_missing_policy_is_not_enforced(self):
        policy = load_operator_policy("/path/that/does/not/exist.json", required=False)
        self.assertFalse(policy.enforced)
        self.assertEqual(policy.active_count, 0)

    def test_real_runtime_refuses_unconfirmed_empty_registry(self):
        repository = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env.update(
            {
                "MOCK_TOURVISOR": "false",
                "TOURVISOR_JWT": "test-only-jwt",
                "SUVVY_WEBHOOK_TOKEN": "test-only-webhook-token",
                "OPERATOR_REGISTRY_PATH": "config/operator_registry.json",
            }
        )
        result = subprocess.run(
            [sys.executable, "-c", "import app.main"],
            cwd=repository,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("active_contract", result.stderr)

    def test_real_runtime_starts_with_valid_policy_without_calling_tourvisor(self):
        repository = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env.update(
            {
                "MOCK_TOURVISOR": "false",
                "TOURVISOR_JWT": "test-only-jwt",
                "SUVVY_WEBHOOK_TOKEN": "test-only-webhook-token",
                "OPERATOR_REGISTRY_PATH": "tests/fixtures/operator_registry.active.json",
            }
        )
        result = subprocess.run(
            [sys.executable, "-c", "import app.main; print('ready')"],
            cwd=repository,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ready")


if __name__ == "__main__":
    unittest.main()
