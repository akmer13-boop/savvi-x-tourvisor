import asyncio
import unittest
from unittest.mock import patch

from app import main
from app.config import settings


class ReadyFlightRuntimeConfigTest(unittest.TestCase):
    def test_ready_exposes_effective_flight_runtime_config(self):
        with (
            patch.object(main, "search_guard", None),
            patch.object(settings, "tourvisor_enable_flight_actualization", True),
            patch.object(settings, "tourvisor_flight_actualization_limit", 1),
            patch.object(settings, "tourvisor_flight_actualization_concurrency", 3),
            patch.object(settings, "tourvisor_flight_timeout_seconds", 45),
        ):
            payload = asyncio.run(main.ready())

        self.assertIsInstance(payload, dict)
        self.assertTrue(payload["flight_actualization_enabled"])
        self.assertEqual(payload["flight_actualization_limit"], 1)
        self.assertEqual(payload["flight_actualization_concurrency"], 3)
        self.assertEqual(payload["flight_actualization_effective_concurrency"], 1)
        self.assertEqual(payload["flight_timeout_seconds"], 45)


if __name__ == "__main__":
    unittest.main()
