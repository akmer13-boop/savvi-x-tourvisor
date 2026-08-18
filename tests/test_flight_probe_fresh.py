import unittest

from probe.main import _find_by_name, _load_active_operator_ids, _select_fresh_tour


class FreshFlightProbeTest(unittest.TestCase):
    def test_dictionary_lookup_accepts_russian_name(self):
        items = [
            {"id": 1, "name": "Moscow", "russianName": "Москва"},
            {"id": 2, "name": "Saint Petersburg", "russianName": "Санкт-Петербург"},
        ]

        result = _find_by_name(items, "Москва")

        self.assertIsNotNone(result)
        self.assertEqual(result["id"], 1)

    def test_operator_registry_contains_active_contracts(self):
        operator_ids = _load_active_operator_ids()

        self.assertGreaterEqual(len(operator_ids), 1)
        self.assertEqual(len(operator_ids), len(set(operator_ids)))

    def test_selects_highest_allowed_price_under_ceiling(self):
        results = [
            {
                "id": 100,
                "name": "Hotel A",
                "rating": 4.5,
                "tours": [
                    {
                        "id": 111,
                        "price": 420000,
                        "currency": "RUB",
                        "date": "2026-11-11",
                        "nights": 7,
                        "operator": {"id": 13, "name": "Anex"},
                    }
                ],
            },
            {
                "id": 200,
                "name": "Hotel B",
                "rating": 4.7,
                "tours": [
                    {
                        "id": 222,
                        "price": 489000,
                        "currency": "RUB",
                        "date": "2026-11-11",
                        "nights": 7,
                        "operator": {"id": 12, "name": "Pegas Touristik"},
                    }
                ],
            },
            {
                "id": 300,
                "name": "Hotel C",
                "rating": 4.9,
                "tours": [
                    {
                        "id": 333,
                        "price": 520000,
                        "currency": "RUB",
                        "date": "2026-11-11",
                        "nights": 7,
                        "operator": {"id": 13, "name": "Anex"},
                    }
                ],
            },
        ]

        selected = _select_fresh_tour(
            results,
            price_to=500000,
            operator_ids={12, 13},
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected["tour_id"], "222")
        self.assertEqual(selected["search_price"], 489000)
        self.assertEqual(selected["operator_id"], 12)

    def test_rejects_disallowed_operator_and_low_rating(self):
        results = [
            {
                "id": 100,
                "name": "Low Rating",
                "rating": 3.8,
                "tours": [
                    {
                        "id": 111,
                        "price": 450000,
                        "operator": {"id": 13, "name": "Anex"},
                    }
                ],
            },
            {
                "id": 200,
                "name": "Wrong Operator",
                "rating": 4.8,
                "tours": [
                    {
                        "id": 222,
                        "price": 470000,
                        "operator": {"id": 999, "name": "Other"},
                    }
                ],
            },
        ]

        selected = _select_fresh_tour(
            results,
            price_to=500000,
            operator_ids={13},
        )

        self.assertIsNone(selected)


if __name__ == "__main__":
    unittest.main()
