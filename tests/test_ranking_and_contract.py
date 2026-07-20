import unittest

import httpx

from app.models import TourOption, TourSearchRequest
from app.operator_policy import OperatorEntry, OperatorPolicy
from app.ranking import select_best_tours
from app.tourvisor_client import TourvisorClient


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


if __name__ == "__main__":
    unittest.main()
