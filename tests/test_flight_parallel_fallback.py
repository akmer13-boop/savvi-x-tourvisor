import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.config import Settings, settings
from app.formatting import format_tours_compact_for_suvvy
from app.models import TourOption, TourSearchRequest
from app.operator_policy import OperatorEntry, OperatorPolicy
from app.tourvisor_client import TourvisorClient


class ParallelFlightFallbackTest(unittest.TestCase):
    def setUp(self):
        self.policy = OperatorPolicy(
            version="test-v1",
            entries=(OperatorEntry(13, "Anex", "active_contract"),),
            sha256="f" * 64,
            enforced=True,
        )
        self.request = TourSearchRequest(
            departure_city="Москва",
            country="Турция",
            date_from="2026-11-11",
            date_to="2026-11-11",
            nights_from=7,
            nights_to=7,
            adults=2,
            children=0,
            budget_type="max",
            budget_to=500_000,
        )

    @staticmethod
    def flight_payload(price: int) -> dict:
        return {
            "error": {"code": 0, "reason": ""},
            "flights": [
                {
                    "isDefault": True,
                    "dateForward": "2026-11-11",
                    "dateBackward": "2026-11-18",
                    "price": {"currency": "RUB", "value": price},
                    "forward": [
                        {
                            "departure": {
                                "date": "2026-11-11",
                                "time": "05:40",
                                "port": {"name": "Москва", "shortName": "VKO"},
                            },
                            "arrival": {
                                "date": "2026-11-11",
                                "time": "10:15",
                                "port": {"name": "Анталья", "shortName": "AYT"},
                            },
                            "noPlaces": False,
                        }
                    ],
                    "backward": [
                        {
                            "departure": {
                                "date": "2026-11-18",
                                "time": "12:30",
                                "port": {"name": "Анталья", "shortName": "AYT"},
                            },
                            "arrival": {
                                "date": "2026-11-18",
                                "time": "17:05",
                                "port": {"name": "Москва", "shortName": "VKO"},
                            },
                            "noPlaces": False,
                        }
                    ],
                }
            ],
            "info": {
                "flags": {
                    "noFlight": False,
                    "noInsurance": False,
                    "noMeal": False,
                    "noTransfer": False,
                }
            },
        }

    @staticmethod
    def tours() -> list[TourOption]:
        return [
            TourOption(
                country="Турция",
                resort="Анталья",
                hotel=f"Parallel Hotel {index}",
                stars=5,
                departure_city="Москва",
                fly_date="2026-11-11",
                nights=7,
                adults=2,
                children=0,
                price=470_000 + index * 1_000,
                currency="RUB",
                tour_id=str(index),
                room="Standard Room",
            )
            for index in range(1, 4)
        ]

    def test_production_defaults_target_three_parallel_cards_with_sixty_second_timeout(self):
        defaults = Settings(_env_file=None)
        self.assertFalse(defaults.tourvisor_enable_flight_actualization)
        self.assertEqual(defaults.tourvisor_flight_actualization_limit, 3)
        self.assertEqual(defaults.tourvisor_flight_actualization_concurrency, 3)
        self.assertEqual(defaults.tourvisor_flight_timeout_seconds, 60)

    def test_three_flight_calls_run_concurrently(self):
        client = TourvisorClient(policy=self.policy)
        tours = self.tours()
        active = 0
        max_active = 0
        lock = asyncio.Lock()

        async def fake_get(http_client, path, params=None):
            nonlocal active, max_active
            async with lock:
                active += 1
                max_active = max(max_active, active)
            await asyncio.sleep(0.03)
            async with lock:
                active -= 1
            tour_id = path.rsplit("/", 2)[-2]
            return self.flight_payload(480_000 + int(tour_id))

        client._get = AsyncMock(side_effect=fake_get)

        with (
            patch.object(settings, "mock_tourvisor", False),
            patch.object(settings, "tourvisor_enable_flight_actualization", True),
            patch.object(settings, "tourvisor_flight_actualization_limit", 3),
            patch.object(settings, "tourvisor_flight_actualization_concurrency", 3),
            patch.object(settings, "tourvisor_flight_timeout_seconds", 60),
            patch.object(settings, "tourvisor_flight_diagnostic_mode", False),
        ):
            asyncio.run(client.enrich_tours_with_flight_details(tours))

        self.assertEqual(client._get.await_count, 3)
        self.assertEqual(max_active, 3)
        self.assertTrue(all(tour.flight_actualized for tour in tours))

    def test_one_timeout_keeps_that_card_with_disclaimer_while_other_two_are_actualized(self):
        client = TourvisorClient(policy=self.policy)
        tours = self.tours()

        async def fake_get(http_client, path, params=None):
            tour_id = path.rsplit("/", 2)[-2]
            if tour_id == "2":
                raise httpx.ReadTimeout("operator flight actualization timeout")
            return self.flight_payload(490_000 + int(tour_id))

        client._get = AsyncMock(side_effect=fake_get)

        with (
            patch.object(settings, "mock_tourvisor", False),
            patch.object(settings, "tourvisor_enable_flight_actualization", True),
            patch.object(settings, "tourvisor_flight_actualization_limit", 3),
            patch.object(settings, "tourvisor_flight_actualization_concurrency", 3),
            patch.object(settings, "tourvisor_flight_timeout_seconds", 60),
            patch.object(settings, "tourvisor_flight_diagnostic_mode", False),
        ):
            result = asyncio.run(client.enrich_tours_with_flight_details(tours))

        self.assertTrue(result[0].flight_actualized)
        self.assertFalse(result[1].flight_actualized)
        self.assertTrue(result[2].flight_actualized)
        self.assertEqual(result[1].price, 472_000)

        text = format_tours_compact_for_suvvy(result, self.request, room_images_per_tour=0)
        disclaimer = (
            "ℹ️ Цена без актуализации перелёта. "
            "Наличие рейса и итоговую стоимость тура подтвердит менеджер."
        )
        self.assertEqual(text.count(disclaimer), 1)
        self.assertEqual(text.count("💰 Итоговая стоимость тура:"), 2)
        self.assertIn("🏨 2. Parallel Hotel 2 5★", text)
        self.assertIn("💰 от 472 000 ₽", text)


if __name__ == "__main__":
    unittest.main()
