import unittest

from probe.main import _effective_read_timeout, _summarize_flight_payload


class FlightProbeTest(unittest.TestCase):
    def test_summarizes_default_flight_without_raw_payload(self):
        payload = {
            "error": {"code": 0, "reason": ""},
            "flights": [
                {
                    "isDefault": True,
                    "dateForward": "2026-11-11",
                    "dateBackward": "2026-11-18",
                    "price": {"currency": "RUB", "value": 479425},
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
                        }
                    ],
                }
            ],
        }

        summary = _summarize_flight_payload(payload)

        self.assertEqual(summary["flights_count"], 1)
        self.assertEqual(summary["price"]["value"], 479425)
        self.assertEqual(summary["forward"]["from"], "VKO")
        self.assertEqual(summary["forward"]["to"], "AYT")
        self.assertEqual(summary["backward"]["from"], "AYT")
        self.assertEqual(summary["backward"]["to"], "VKO")
        self.assertNotIn("flights", summary)

    def test_effective_timeout_is_bounded(self):
        self.assertGreaterEqual(_effective_read_timeout(1), 30)
        self.assertLessEqual(_effective_read_timeout(9999), 300)
        self.assertEqual(_effective_read_timeout(180), 180)


if __name__ == "__main__":
    unittest.main()
