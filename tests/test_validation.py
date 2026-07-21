import unittest
from datetime import date, timedelta
from unittest.mock import patch

from pydantic import ValidationError

from app.budget import BudgetPolicy
from app.models import TourSearchRequest
from app.validation import (
    ManagerRoutingRequired,
    SearchInputError,
    normalize_budget_rub,
    validate_and_normalize_search_request,
)


class SearchValidationTest(unittest.TestCase):
    def _request(self, **updates) -> TourSearchRequest:
        start = date.today() + timedelta(days=30)
        payload = {
            "departure_city": "Москва",
            "country": "Турция",
            "date_from": start.isoformat(),
            "date_to": (start + timedelta(days=6)).isoformat(),
            "nights_from": 7,
            "nights_to": 10,
            "adults": 2,
            "children": 0,
            "budget": 500,
        }
        payload.update(updates)
        return TourSearchRequest(**payload)

    def test_budget_500_is_normalized_to_500000(self):
        normalized = validate_and_normalize_search_request(self._request())
        self.assertEqual(normalized.budget, 500_000)

    def test_all_budget_modes_have_canonical_bounds(self):
        cases = (
            ({"budget": None, "budget_type": "max", "budget_to": 500}, (None, 500_000)),
            ({"budget": None, "budget_type": "min", "budget_from": 300}, (300_000, None)),
            (
                {"budget": None, "budget_type": "approx", "budget_to": 500_001},
                (450_001, 550_001),
            ),
            (
                {
                    "budget": None,
                    "budget_type": "range",
                    "budget_from": 300,
                    "budget_to": 500,
                },
                (300_000, 500_000),
            ),
            ({"budget": None, "budget_type": "unknown"}, (None, None)),
        )
        for payload, expected_bounds in cases:
            with self.subTest(mode=payload["budget_type"]):
                normalized = validate_and_normalize_search_request(self._request(**payload))
                policy = BudgetPolicy.from_request(normalized)
                self.assertEqual((policy.price_from, policy.price_to), expected_bounds)

    def test_approx_validation_is_idempotent(self):
        once = validate_and_normalize_search_request(
            self._request(budget=None, budget_type="approx", budget_to=500_001)
        )
        twice = validate_and_normalize_search_request(once)
        self.assertEqual(twice.budget_to, 500_001)
        self.assertEqual(BudgetPolicy.from_request(twice).price_to, 550_001)

    def test_conflicting_budget_fields_are_rejected(self):
        conflicts = (
            {"budget": 500, "budget_type": "range", "budget_from": 300, "budget_to": 500},
            {"budget": None, "budget_type": "max", "budget_from": 300, "budget_to": 500},
            {"budget": None, "budget_type": "min", "budget_from": 300, "budget_to": 500},
            {"budget": None, "budget_type": "unknown", "budget_to": 500},
            {"budget": None, "budget_type": "range", "budget_from": 500, "budget_to": 300},
        )
        for payload in conflicts:
            with self.subTest(payload=payload):
                with self.assertRaises(SearchInputError) as context:
                    validate_and_normalize_search_request(self._request(**payload))
                self.assertEqual(context.exception.reason, "INVALID_BUDGET")

    def test_matching_legacy_and_new_max_is_allowed_during_migration(self):
        normalized = validate_and_normalize_search_request(
            self._request(budget=500, budget_type="max", budget_to=500)
        )
        self.assertEqual(normalized.budget, 500_000)
        self.assertEqual(normalized.budget_to, 500_000)

    def test_operator_ids_from_client_are_rejected(self):
        with self.assertRaises(ValidationError):
            self._request(operatorIds=[13, 53])
        with self.assertRaises(ValidationError):
            self._request(operator_ids=[13, 53])

    def test_refresh_requested_defaults_false_and_accepts_explicit_true(self):
        self.assertFalse(self._request().refresh_requested)
        self.assertTrue(self._request(refresh_requested=True).refresh_requested)

    def test_numeric_chat_id_is_normalized_but_bool_is_rejected(self):
        self.assertEqual(self._request(chat_id=-10012345).chat_id, "-10012345")
        with self.assertRaises(ValidationError):
            self._request(chat_id=True)

    def test_no_preference_placeholders_become_null(self):
        request = self._request(resort="Любой курорт", meal="неважно")
        self.assertIsNone(request.resort)
        self.assertIsNone(request.meal)

    def test_budget_string_with_spaces_is_accepted(self):
        request = self._request(budget="500 000")
        self.assertEqual(request.budget, 500_000)

    def test_budget_boundaries(self):
        self.assertEqual(normalize_budget_rub(50), 50_000)
        self.assertEqual(normalize_budget_rub(999), 999_000)
        self.assertEqual(normalize_budget_rub(50_000), 50_000)
        for value in (None, 0, 1, 49, 1_000, 49_999):
            with self.subTest(value=value):
                with self.assertRaises(SearchInputError):
                    normalize_budget_rub(value)

    def test_departure_window_is_at_most_seven_calendar_days(self):
        start = date.today() + timedelta(days=30)
        request = self._request(date_to=(start + timedelta(days=7)).isoformat())
        with self.assertRaises(SearchInputError) as context:
            validate_and_normalize_search_request(request)
        self.assertEqual(context.exception.reason, "INVALID_DATES")

    def test_past_departure_date_is_rejected(self):
        start = date.today() - timedelta(days=1)
        request = self._request(
            date_from=start.isoformat(),
            date_to=(start + timedelta(days=1)).isoformat(),
        )
        with self.assertRaises(SearchInputError) as context:
            validate_and_normalize_search_request(request)
        self.assertEqual(context.exception.reason, "INVALID_DATES")

    def test_past_date_uses_business_calendar_not_container_timezone(self):
        business_today = date(2026, 7, 22)
        with patch("app.validation._business_today", return_value=business_today):
            with self.assertRaises(SearchInputError) as context:
                validate_and_normalize_search_request(
                    self._request(
                        date_from="2026-07-21",
                        date_to="2026-07-21",
                    )
                )
        self.assertEqual(context.exception.reason, "INVALID_DATES")

    def test_suvvy_nights_are_validated_not_recalculated(self):
        request = self._request(nights_from=7, nights_to=18)
        with self.assertRaises(SearchInputError) as context:
            validate_and_normalize_search_request(request)
        self.assertEqual(context.exception.reason, "INVALID_NIGHTS")

    def test_children_require_exactly_one_age_each(self):
        request = self._request(children=2, children_ages=[8])
        with self.assertRaises(SearchInputError) as context:
            validate_and_normalize_search_request(request)
        self.assertEqual(context.exception.reason, "CHILD_AGES_REQUIRED")

    def test_no_children_requires_no_extra_question(self):
        normalized = validate_and_normalize_search_request(
            self._request(children=0, children_ages=[])
        )
        self.assertEqual(normalized.children, 0)

    def test_china_and_hainan_are_routed_without_search(self):
        for updates in (
            {"country": "Китай"},
            {"country": "Китайская Народная Республика"},
            {"country": "China"},
            {"country": "CN"},
            {"country": "CHN"},
            {"country": "Хайнань"},
            {"resort": "Хайнань"},
            {"resort": "Санья"},
        ):
            with self.subTest(updates=updates):
                with self.assertRaises(ManagerRoutingRequired) as context:
                    validate_and_normalize_search_request(self._request(**updates))
                self.assertEqual(context.exception.reason, "DESTINATION_BLOCKED")

    def test_four_or_more_children_are_routed_to_manager(self):
        request = self._request(children=4, children_ages=[2, 5, 8, 12])
        with self.assertRaises(ManagerRoutingRequired) as context:
            validate_and_normalize_search_request(request)
        self.assertEqual(
            context.exception.reason,
            "MANAGER_REQUIRED_TOO_MANY_CHILDREN",
        )


if __name__ == "__main__":
    unittest.main()
