import unittest

from app.formatting import format_tours_with_images_for_client
from app.models import TourOption, TourSearchRequest
from app.ranking import select_best_tours


class BusinessRulesTest(unittest.TestCase):
    def setUp(self):
        self.request = TourSearchRequest(
            departure_city="Москва",
            country="Турция",
            resort="Сиде",
            date_from="2026-08-10",
            date_to="2026-08-20",
            nights_from=7,
            nights_to=7,
            adults=2,
            children=0,
            budget=250000,
            meal="all inclusive",
            hotel_stars=5,
        )

    def test_rating_filter_and_output(self):
        tours = [
            TourOption(
                country="Турция",
                resort="Сиде",
                hotel="GOOD HOTEL",
                stars=5,
                meal="AI",
                fly_date="2026-08-19",
                nights=7,
                price=200000,
                rating=4.4,
                operator="Internal operator",
                operator_id=101,
                tour_picture="https://example.com/hotel.jpg",
                room="standard",
                room_images=["https://example.com/room1.jpg"],
            ),
            TourOption(
                country="Турция",
                resort="Сиде",
                hotel="LOW RATING",
                stars=5,
                meal="AI",
                fly_date="2026-08-19",
                nights=7,
                price=150000,
                rating=3.9,
            ),
        ]
        selected = select_best_tours(tours, self.request)
        self.assertEqual([tour.hotel for tour in selected], ["GOOD HOTEL"])

        text = format_tours_with_images_for_client(selected, self.request)
        self.assertNotIn("Рейтинг", text)
        self.assertNotIn("Туроператор", text)
        self.assertIn("19 августа 2026 года", text)
        self.assertLess(text.index("hotel.jpg"), text.index("room1.jpg"))
        self.assertNotIn("Хотите, я передам", text)


if __name__ == "__main__":
    unittest.main()
