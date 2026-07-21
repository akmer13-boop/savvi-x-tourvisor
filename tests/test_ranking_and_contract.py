import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.config import settings
from app.models import TourOption, TourSearchRequest
from app.operator_policy import OperatorEntry, OperatorPolicy
from app.ranking import select_best_tours
from app.tourvisor_client import (
    TourvisorClient,
    TourvisorContractConfigurationError,
    UserInputError,
)


class RankingAndTourvisorContractTest(unittest.TestCase):
    def setUp(self):
        self.policy = OperatorPolicy(
            version="test-v1",
            entries=(
                OperatorEntry(13, "Anex", "active_contract"),
                OperatorEntry(53, "Арт-Тур", "active_contract"),
                OperatorEntry(36, "PAC GROUP", "blocked"),
            ),
            sha256="a" * 64,
            enforced=True,
        )
        self.request = TourSearchRequest(
            departure_city="Москва",
            country="Турция",
            date_from="2027-08-10",
            date_to="2027-08-15",
            nights_from=7,
            nights_to=10,
            budget=250_000,
        )

    def test_strict_budget_and_whitelist(self):
        tours = [
            TourOption(country="Турция", hotel="OK", price=250_000, rating=4.5, operator_id=13),
            TourOption(country="Турция", hotel="OVER", price=250_001, rating=4.8, operator_id=13),
            TourOption(country="Турция", hotel="NO PRICE", price=None, rating=4.8, operator_id=13),
            TourOption(country="Турция", hotel="BLOCKED", price=200_000, rating=4.8, operator_id=36),
            TourOption(country="Турция", hotel="NO ID", price=200_000, rating=4.8, operator_id=None),
        ]
        selected = select_best_tours(tours, self.request, policy=self.policy)
        self.assertEqual([tour.hotel for tour in selected], ["OK"])

    def test_max_budget_prioritizes_final_100k_corridor(self):
        tours = [
            TourOption(country="Турция", hotel="CHEAP", price=140_000, rating=5.0, operator_id=13),
            TourOption(country="Турция", hotel="CORRIDOR", price=150_000, rating=4.0, operator_id=13),
        ]
        selected = select_best_tours(tours, self.request, policy=self.policy)
        self.assertEqual([tour.hotel for tour in selected], ["CORRIDOR", "CHEAP"])

    def test_min_range_approx_and_unknown_post_filters(self):
        tours = [
            TourOption(country="Турция", hotel="LOW", price=200_000, rating=4.5, operator_id=13),
            TourOption(country="Турция", hotel="MID", price=300_000, rating=4.5, operator_id=13),
            TourOption(country="Турция", hotel="HIGH", price=600_000, rating=4.5, operator_id=13),
            TourOption(country="Турция", hotel="NO PRICE", price=None, rating=4.5, operator_id=13),
        ]
        cases = (
            ({"budget": None, "budget_type": "min", "budget_from": 300_000}, ["MID", "HIGH"]),
            (
                {
                    "budget": None,
                    "budget_type": "range",
                    "budget_from": 250_000,
                    "budget_to": 500_000,
                },
                ["MID"],
            ),
            ({"budget": None, "budget_type": "approx", "budget_to": 300_000}, ["MID"]),
            (
                {"budget": None, "budget_type": "unknown"},
                ["LOW", "MID", "HIGH"],
            ),
        )
        for updates, expected in cases:
            with self.subTest(mode=updates["budget_type"]):
                request = self.request.model_copy(update=updates)
                selected = select_best_tours(tours, request, policy=self.policy)
                self.assertEqual([tour.hotel for tour in selected], expected)

    def test_operator_ids_are_serialized_as_repeated_query_parameters(self):
        client = TourvisorClient(policy=self.policy)
        params = client._build_search_params(
            request=self.request.model_copy(
                update={"hotel_stars": 5, "meal": "all inclusive"}
            ),
            departure_id=1,
            country_id=4,
            region_id=23,
            meal_id=7,
        )
        url = str(httpx.Request("GET", "https://example.test/search", params=params).url)
        self.assertEqual(url.count("operatorIds="), 2)
        self.assertIn("operatorIds=13", url)
        self.assertIn("operatorIds=53", url)
        self.assertNotIn("operatorIds=36", url)
        self.assertIn("hotelCategory=5", url)
        self.assertIn("regionIds=23", url)
        self.assertIn("meal=7", url)
        self.assertIn("priceTo=250000", url)
        self.assertNotIn("priceFrom=", url)

    def test_price_from_contract_and_unknown_query_shape(self):
        client = TourvisorClient(policy=self.policy)
        min_request = self.request.model_copy(
            update={"budget": None, "budget_type": "min", "budget_from": 300_000}
        )
        unknown_request = self.request.model_copy(
            update={"budget": None, "budget_type": "unknown"}
        )
        with (
            patch.object(settings, "tourvisor_price_from_enabled", True),
            patch.object(
                settings,
                "tourvisor_api_contract_version",
                "tourvisor-verified-2026-07-21",
            ),
        ):
            min_params = client._build_search_params(min_request, 1, 4, None, None)
        with (
            patch.object(settings, "tourvisor_price_from_enabled", False),
            patch.object(settings, "tourvisor_api_contract_version", "unverified"),
        ):
            unknown_params = client._build_search_params(
                unknown_request,
                1,
                4,
                None,
                None,
            )
        self.assertEqual(min_params["priceFrom"], 300_000)
        self.assertNotIn("priceTo", min_params)
        self.assertNotIn("priceFrom", unknown_params)
        self.assertNotIn("priceTo", unknown_params)

    def test_unverified_price_contract_fails_closed(self):
        client = TourvisorClient(policy=self.policy)
        request = self.request.model_copy(
            update={"budget": None, "budget_type": "range", "budget_from": 200_000, "budget_to": 300_000}
        )
        with patch.object(settings, "tourvisor_price_from_enabled", False):
            with self.assertRaises(TourvisorContractConfigurationError):
                client._build_search_params(request, 1, 4, None, None)
        with (
            patch.object(settings, "tourvisor_price_from_enabled", True),
            patch.object(settings, "tourvisor_api_contract_version", "unverified"),
        ):
            with self.assertRaises(TourvisorContractConfigurationError):
                client._build_search_params(request, 1, 4, None, None)

    def test_before_dispatch_runs_once_for_mock_search(self):
        client = TourvisorClient(policy=self.policy)
        before_dispatch = AsyncMock()
        with patch.object(settings, "mock_tourvisor", True):
            asyncio.run(client.search_tours(self.request, before_dispatch=before_dispatch))
        before_dispatch.assert_awaited_once_with()

    def test_explicit_unknown_region_or_meal_requires_clarification(self):
        client = TourvisorClient(policy=self.policy)
        with patch.object(client, "_get", new=AsyncMock(return_value=[])):
            with self.assertRaises(UserInputError) as region_error:
                asyncio.run(client._resolve_region(None, "Неизвестный курорт", 4))
            with self.assertRaises(UserInputError) as meal_error:
                asyncio.run(client._resolve_meal(None, "Неизвестное питание"))
        self.assertEqual(region_error.exception.reason, "REGION_NOT_FOUND")
        self.assertEqual(meal_error.exception.reason, "MEAL_NOT_FOUND")

    def test_parser_drops_results_without_an_allowed_operator(self):
        client = TourvisorClient(policy=self.policy)
        data = [
            {
                "id": 1,
                "name": "Hotel",
                "category": 5,
                "rating": 4.5,
                "tours": [
                    {"id": 1, "price": 200000, "operator": {"id": 36, "name": "Blocked"}},
                    {"id": 2, "price": 210000, "operator": {"id": 13, "name": "Anex"}},
                ],
            }
        ]
        tours = client._parse_search_results(data, self.request)
        self.assertEqual(len(tours), 1)
        self.assertEqual(tours[0].operator_id, 13)

    def test_parser_filters_budget_before_choosing_hotel_option(self):
        client = TourvisorClient(policy=self.policy)
        request = self.request.model_copy(
            update={"budget": None, "budget_type": "min", "budget_from": 250_000}
        )
        data = [
            {
                "id": 1,
                "name": "Hotel",
                "category": 5,
                "rating": 4.5,
                "tours": [
                    {"id": 1, "price": 200_000, "operator": {"id": 13, "name": "Anex"}},
                    {"id": 2, "price": 300_000, "operator": {"id": 13, "name": "Anex"}},
                ],
            }
        ]
        tours = client._parse_search_results(data, request)
        self.assertEqual([tour.price for tour in tours], [300_000])

    def test_parser_excludes_unknown_price_even_without_budget_bounds(self):
        client = TourvisorClient(policy=self.policy)
        request = self.request.model_copy(update={"budget": None, "budget_type": "unknown"})
        data = [
            {
                "id": 1,
                "name": "Hotel",
                "category": 5,
                "rating": 4.5,
                "tours": [{"id": 1, "operator": {"id": 13, "name": "Anex"}}],
            }
        ]
        self.assertEqual(client._parse_search_results(data, request), [])

    def test_zero_tour_price_uses_positive_hotel_price_or_is_excluded(self):
        client = TourvisorClient(policy=self.policy)
        request = self.request.model_copy(
            update={"budget": None, "budget_type": "unknown"}
        )
        data = [
            {
                "id": 1,
                "name": "Fallback price",
                "category": 5,
                "rating": 4.5,
                "price": 240_000,
                "tours": [
                    {
                        "id": 1,
                        "price": 0,
                        "operator": {"id": 13, "name": "Anex"},
                    }
                ],
            },
            {
                "id": 2,
                "name": "No real price",
                "category": 5,
                "rating": 4.5,
                "price": 0,
                "tours": [
                    {
                        "id": 2,
                        "price": 0,
                        "operator": {"id": 13, "name": "Anex"},
                    }
                ],
            },
        ]
        tours = client._parse_search_results(data, request)
        self.assertEqual(
            [(tour.hotel, tour.price) for tour in tours],
            [("Fallback price", 240_000)],
        )


if __name__ == "__main__":
    unittest.main()
