import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.formatting import format_tours_compact_for_suvvy
from app.models import TourOption, TourSearchRequest
from app.operator_policy import OperatorEntry, OperatorPolicy
from app.tourvisor_client import TourvisorClient


class TourvisorFlightActualizationTest(unittest.TestCase):
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
    def flight_payload(price: int = 479_425) -> dict:
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
                },
                "surcharges": [],
            },
        }

    def test_actualizes_default_flight_and_final_price(self):
        client = TourvisorClient(policy=self.policy)
        client._get = AsyncMock(return_value=self.flight_payload())
        tour = TourOption(
            country="Турция",
            resort="Лара",
            hotel="LARA BARUT COLLECTION",
            stars=5,
            meal="UAI - Ультра Всё Включено",
            departure_city="Москва",
            fly_date="2026-11-11",
            nights=7,
            adults=2,
            children=0,
            price=470_000,
            currency="RUB",
            tour_id="12345",
            operator_id=13,
            room="Penthouse Suite",
        )

        with (
            patch.object(settings, "mock_tourvisor", False),
            patch.object(settings, "tourvisor_enable_flight_actualization", True),
            patch.object(settings, "tourvisor_flight_actualization_limit", 3),
            patch.object(settings, "tourvisor_flight_actualization_concurrency", 3),
        ):
            result = asyncio.run(client.enrich_tours_with_flight_details([tour]))

        self.assertIs(result[0], tour)
        self.assertTrue(tour.flight_actualized)
        self.assertTrue(tour.flight_included)
        self.assertTrue(tour.flight_is_direct)
        self.assertEqual(tour.search_price, 470_000)
        self.assertEqual(tour.price, 479_425)
        self.assertEqual(tour.flight_origin, "Москва")
        self.assertEqual(tour.flight_destination, "Анталья")
        self.assertEqual(tour.flight_forward_date, "2026-11-11")
        self.assertEqual(tour.flight_forward_departure_time, "05:40")
        self.assertEqual(tour.flight_forward_arrival_time, "10:15")
        self.assertEqual(tour.flight_backward_date, "2026-11-18")
        self.assertEqual(tour.flight_backward_departure_time, "12:30")
        self.assertEqual(tour.flight_backward_arrival_time, "17:05")
        client._get.assert_awaited_once()
        self.assertIn("/tours/12345/flights", client._get.await_args.args[1])
        self.assertEqual(client._get.await_args.kwargs["params"], {"currency": "RUB"})

    def test_compact_output_uses_requested_route_shape_and_price_delta(self):
        tour = TourOption(
            country="Турция",
            resort="Лара",
            hotel="LARA BARUT COLLECTION",
            stars=5,
            meal="UAI - Ультра Всё Включено",
            departure_city="Москва",
            fly_date="2026-11-11",
            nights=7,
            adults=2,
            children=0,
            search_price=470_000,
            price=479_425,
            currency="RUB",
            room="Penthouse Suite",
            flight_actualized=True,
            flight_included=True,
            flight_is_direct=True,
            flight_origin="Москва",
            flight_destination="Анталья",
            flight_forward_date="2026-11-11",
            flight_forward_departure_time="05:40",
            flight_forward_arrival_time="10:15",
            flight_backward_date="2026-11-18",
            flight_backward_departure_time="12:30",
            flight_backward_arrival_time="17:05",
        )

        text = format_tours_compact_for_suvvy(
            [tour],
            self.request,
            room_images_per_tour=0,
        )

        self.assertIn("✈️ Москва → Анталья → Москва", text)
        self.assertIn("11 ноября: 05:40 → 10:15", text)
        self.assertIn("18 ноября: 12:30 → 17:05", text)
        self.assertIn("💺 Перелёт: +9 425 ₽ к найденной цене", text)
        self.assertIn("🌙 7 ночей", text)
        self.assertIn("💰 Итоговая стоимость тура: 479 425 ₽", text)
        self.assertNotIn("💰 от 479 425 ₽", text)
        self.assertIn("Рейсы и стоимость актуализированы на момент поиска", text)

    def test_compact_output_marks_flight_without_surcharge(self):
        tour = TourOption(
            country="Турция",
            hotel="No Surcharge Hotel",
            departure_city="Москва",
            search_price=470_000,
            price=470_000,
            currency="RUB",
            flight_actualized=True,
            flight_included=True,
            flight_is_direct=True,
            flight_origin="Москва",
            flight_destination="Анталья",
            flight_forward_date="2026-11-11",
            flight_forward_departure_time="05:40",
            flight_forward_arrival_time="10:15",
            flight_backward_date="2026-11-18",
            flight_backward_departure_time="12:30",
            flight_backward_arrival_time="17:05",
        )

        text = format_tours_compact_for_suvvy(
            [tour],
            self.request,
            room_images_per_tour=0,
        )

        self.assertIn("💺 Перелёт: без доплаты к найденной цене", text)
        self.assertIn("💰 Итоговая стоимость тура: 470 000 ₽", text)

    def test_unavailable_flight_does_not_replace_search_price(self):
        payload = self.flight_payload(price=490_000)
        payload["flights"][0]["forward"][0]["noPlaces"] = True
        client = TourvisorClient(policy=self.policy)
        client._get = AsyncMock(return_value=payload)
        tour = TourOption(
            country="Турция",
            hotel="No Seats Hotel",
            departure_city="Москва",
            price=470_000,
            currency="RUB",
            tour_id="777",
        )

        with (
            patch.object(settings, "mock_tourvisor", False),
            patch.object(settings, "tourvisor_enable_flight_actualization", True),
        ):
            asyncio.run(client.enrich_tours_with_flight_details([tour]))

        self.assertFalse(tour.flight_actualized)
        self.assertEqual(tour.price, 470_000)
        self.assertIsNone(tour.search_price)

    def test_billable_actualization_is_capped_to_configured_limit(self):
        client = TourvisorClient(policy=self.policy)
        client._get = AsyncMock(return_value=self.flight_payload())
        tours = [
            TourOption(
                country="Турция",
                hotel=f"Hotel {index}",
                departure_city="Москва",
                price=470_000,
                currency="RUB",
                tour_id=str(index),
            )
            for index in range(1, 5)
        ]

        with (
            patch.object(settings, "mock_tourvisor", False),
            patch.object(settings, "tourvisor_enable_flight_actualization", True),
            patch.object(settings, "tourvisor_flight_actualization_limit", 3),
            patch.object(settings, "tourvisor_flight_actualization_concurrency", 3),
        ):
            asyncio.run(client.enrich_tours_with_flight_details(tours))

        self.assertEqual(client._get.await_count, 3)
        self.assertTrue(all(tour.flight_actualized for tour in tours[:3]))
        self.assertFalse(tours[3].flight_actualized)


if __name__ == "__main__":
    unittest.main()
