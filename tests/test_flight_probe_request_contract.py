import unittest
from datetime import date

from probe.main import FreshProbeStartRequest, ProbeStartResponse


class FlightProbeRequestContractTest(unittest.TestCase):
    def test_fresh_request_defaults_match_control_case(self):
        request = FreshProbeStartRequest(date_from=date(2026, 11, 11))

        self.assertEqual(request.departure_city, "Москва")
        self.assertEqual(request.country, "Турция")
        self.assertEqual(request.nights_from, 7)
        self.assertEqual(request.adults, 2)
        self.assertEqual(request.price_to, 500000)
        self.assertEqual(request.read_timeout_seconds, 180)

    def test_start_response_allows_tour_id_to_be_pending_for_fresh_mode(self):
        response = ProbeStartResponse(
            job_id="abc123",
            status="queued",
            poll_path="/probe/abc123",
            tour_id=None,
            read_timeout_seconds=180,
            mode="fresh_search",
        )

        self.assertIsNone(response.tour_id)
        self.assertEqual(response.mode, "fresh_search")


if __name__ == "__main__":
    unittest.main()
