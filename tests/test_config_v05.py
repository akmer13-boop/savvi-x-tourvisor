import unittest

from app.config import Settings


class RuntimeConfigurationV05Test(unittest.TestCase):
    def _real_settings(self, **updates) -> Settings:
        values = {
            "app_environment": "development",
            "mock_tourvisor": False,
            "tourvisor_jwt": "test-only-jwt",
            "suvvy_webhook_token": "test-only-webhook",
            "git_commit_sha": "a" * 40,
        }
        values.update(updates)
        return Settings(_env_file=None, **values)

    def test_guard_requires_hmac_secret_when_enabled(self):
        configured = self._real_settings(search_guard_enabled=True)
        with self.assertRaisesRegex(ValueError, "SEARCH_GUARD_HMAC_SECRET"):
            configured.validate_runtime_configuration(active_operator_count=15)

    def test_production_rejects_mock_mode(self):
        configured = Settings(
            _env_file=None,
            app_environment="production",
            mock_tourvisor=True,
            git_commit_sha="a" * 40,
        )
        with self.assertRaisesRegex(ValueError, "MOCK_TOURVISOR"):
            configured.validate_runtime_configuration(active_operator_count=15)

    def test_production_rejects_unknown_commit(self):
        configured = self._real_settings(
            app_environment="production",
            git_commit_sha="unknown",
        )
        with self.assertRaisesRegex(ValueError, "GIT_COMMIT_SHA"):
            configured.validate_runtime_configuration(active_operator_count=15)

    def test_valid_guard_configuration_is_accepted(self):
        configured = self._real_settings(
            search_guard_enabled=True,
            search_guard_hmac_secret="test-only-guard-secret",
            search_guard_persistence_verified=True,
        )
        configured.validate_runtime_configuration(active_operator_count=15)

    def test_guard_rejects_disabled_cleanup_loop(self):
        configured = self._real_settings(
            search_guard_enabled=True,
            search_guard_hmac_secret="test-only-guard-secret",
            search_guard_persistence_verified=True,
            search_guard_prune_interval_seconds=0,
        )
        with self.assertRaisesRegex(ValueError, "SEARCH_GUARD_PRUNE_INTERVAL_SECONDS"):
            configured.validate_runtime_configuration(active_operator_count=15)

    def test_replay_and_cleanup_are_physically_bounded_by_60_seconds(self):
        configured = self._real_settings(
            search_guard_enabled=True,
            search_guard_hmac_secret="test-only-guard-secret",
            search_guard_persistence_verified=True,
            search_result_replay_ttl_seconds=46,
            search_guard_prune_interval_seconds=15,
        )
        with self.assertRaisesRegex(ValueError, "must not exceed 60"):
            configured.validate_runtime_configuration(active_operator_count=15)

    def test_guard_rejects_retention_over_three_days_and_replay_over_60_seconds(self):
        for update, expected in (
            ({"search_guard_ttl_seconds": 259_201}, "SEARCH_GUARD_TTL_SECONDS"),
            (
                {"search_result_replay_ttl_seconds": 61},
                "SEARCH_RESULT_REPLAY_TTL_SECONDS",
            ),
        ):
            with self.subTest(update=update):
                configured = self._real_settings(
                    search_guard_enabled=True,
                    search_guard_hmac_secret="test-only-guard-secret",
                    search_guard_persistence_verified=True,
                    **update,
                )
                with self.assertRaisesRegex(ValueError, expected):
                    configured.validate_runtime_configuration(active_operator_count=15)

    def test_price_from_gate_requires_verified_contract_version(self):
        configured = self._real_settings(
            tourvisor_price_from_enabled=True,
            tourvisor_api_contract_version="unverified",
        )
        with self.assertRaisesRegex(ValueError, "TOURVISOR_API_CONTRACT_VERSION"):
            configured.validate_runtime_configuration(active_operator_count=15)

        verified = self._real_settings(
            tourvisor_price_from_enabled=True,
            tourvisor_api_contract_version="tourvisor-verified-2026-07-21",
        )
        verified.validate_runtime_configuration(active_operator_count=15)

    def test_max_budget_corridors_are_validated(self):
        invalid_primary = self._real_settings(
            tourvisor_max_budget_primary_corridor=0,
            tourvisor_max_budget_fallback_corridor=100_000,
        )
        with self.assertRaisesRegex(
            ValueError,
            "TOURVISOR_MAX_BUDGET_PRIMARY_CORRIDOR",
        ):
            invalid_primary.validate_runtime_configuration(
                active_operator_count=15
            )

        invalid_fallback = self._real_settings(
            tourvisor_max_budget_primary_corridor=100_000,
            tourvisor_max_budget_fallback_corridor=25_000,
        )
        with self.assertRaisesRegex(
            ValueError,
            "TOURVISOR_MAX_BUDGET_FALLBACK_CORRIDOR",
        ):
            invalid_fallback.validate_runtime_configuration(
                active_operator_count=15
            )

        valid = self._real_settings(
            tourvisor_max_budget_primary_corridor=25_000,
            tourvisor_max_budget_fallback_corridor=100_000,
        )
        valid.validate_runtime_configuration(active_operator_count=15)

    def test_flight_concurrency_above_limit_does_not_block_startup(self):
        configured = self._real_settings(
            tourvisor_flight_actualization_limit=1,
            tourvisor_flight_actualization_concurrency=3,
            tourvisor_flight_timeout_seconds=45,
        )
        configured.validate_runtime_configuration(active_operator_count=15)
        self.assertEqual(configured.effective_flight_actualization_concurrency, 1)

    def test_invalid_flight_runtime_values_are_rejected(self):
        for update, expected in (
            ({"tourvisor_flight_actualization_limit": 0}, "TOURVISOR_FLIGHT_ACTUALIZATION_LIMIT"),
            ({"tourvisor_flight_actualization_concurrency": 0}, "TOURVISOR_FLIGHT_ACTUALIZATION_CONCURRENCY"),
            ({"tourvisor_flight_timeout_seconds": 0}, "TOURVISOR_FLIGHT_TIMEOUT_SECONDS"),
        ):
            with self.subTest(update=update):
                configured = self._real_settings(**update)
                with self.assertRaisesRegex(ValueError, expected):
                    configured.validate_runtime_configuration(active_operator_count=15)

    def test_guard_requires_explicit_persistence_restart_acknowledgement(self):
        configured = self._real_settings(
            search_guard_enabled=True,
            search_guard_hmac_secret="test-only-guard-secret",
            search_guard_persistence_verified=False,
        )
        with self.assertRaisesRegex(ValueError, "SEARCH_GUARD_PERSISTENCE_VERIFIED"):
            configured.validate_runtime_configuration(active_operator_count=15)

    def test_invalid_business_timezone_is_rejected(self):
        configured = self._real_settings(business_timezone="Not/A-Timezone")
        with self.assertRaisesRegex(ValueError, "BUSINESS_TIMEZONE"):
            configured.validate_runtime_configuration(active_operator_count=15)


if __name__ == "__main__":
    unittest.main()
