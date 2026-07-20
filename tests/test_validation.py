import unittest
from datetime import date, timedelta

from app.models import TourSearchRequest
from app.validation import (
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


if __name__ == "__main__":
    unittest.main()
