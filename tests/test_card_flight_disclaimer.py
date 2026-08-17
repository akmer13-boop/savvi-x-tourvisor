import unittest

from app.formatting import format_tour_card_text, format_tours_compact_for_suvvy
from app.models import TourOption, TourSearchRequest


DISCLAIMER = (
    "ℹ️ Цена без актуализации перелёта. "
    "Наличие рейса и итоговую стоимость тура подтвердит менеджер."
)


class CardFlightDisclaimerTest(unittest.TestCase):
    def setUp(self):
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
    def _tour(index: int, *, flight_actualized: bool = False) -> TourOption:
        return TourOption(
            country="Турция",
            resort="Стамбул",
            hotel=f"Hotel {index}",
            stars=5,
            meal="BB - Только завтрак",
            departure_city="Москва",
            fly_date="2026-11-11",
            nights=7,
            adults=2,
            children=0,
            price=478_709,
            currency="RUB",
            room="Standard Room",
            flight_actualized=flight_actualized,
            flight_included=True if flight_actualized else None,
            flight_is_direct=True if flight_actualized else None,
            flight_origin="Москва" if flight_actualized else None,
            flight_destination="Стамбул" if flight_actualized else None,
            flight_forward_date="2026-11-11" if flight_actualized else None,
            flight_forward_departure_time="05:40" if flight_actualized else None,
            flight_forward_arrival_time="10:15" if flight_actualized else None,
            flight_backward_date="2026-11-18" if flight_actualized else None,
            flight_backward_departure_time="12:30" if flight_actualized else None,
            flight_backward_arrival_time="17:05" if flight_actualized else None,
        )

    def test_compact_output_repeats_disclaimer_inside_every_unactualized_card(self):
        tours = [self._tour(index) for index in range(1, 4)]

        text = format_tours_compact_for_suvvy(
            tours,
            self.request,
            room_images_per_tour=0,
        )

        self.assertEqual(text.count(DISCLAIMER), 3)
        self.assertIn("💰 от 478 709 ₽\n" + DISCLAIMER, text)

    def test_full_card_places_disclaimer_immediately_after_price(self):
        card = format_tour_card_text(self._tour(1), self.request, 1)

        self.assertIn("💰 Стоимость: от 478 709 ₽\n" + DISCLAIMER, card)

    def test_actualized_flight_card_does_not_show_disclaimer(self):
        tour = self._tour(1, flight_actualized=True)
        tour.search_price = 470_000

        text = format_tours_compact_for_suvvy(
            [tour],
            self.request,
            room_images_per_tour=0,
        )

        self.assertNotIn(DISCLAIMER, text)
        self.assertIn("💰 Итоговая стоимость тура: 478 709 ₽", text)


if __name__ == "__main__":
    unittest.main()
