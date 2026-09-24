import io
import json
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest import mock

from flight_tracker.models import Offer, Tracker
from flight_tracker.providers.base import ProviderError
from flight_tracker.providers.duffel import DuffelProvider

OFFER = {
    "id": "off_123",
    "total_amount": "412.50",
    "total_currency": "USD",
    "owner": {"name": "Duffel Airways"},
    "expires_at": "2030-01-01T12:00:00Z",
    "passengers": [{"id": "pas_1", "type": "adult"}],
    "slices": [{"segments": [
        {"origin": {"iata_code": "JFK"}, "destination": {"iata_code": "DUB"}, "departing_at": "2030-02-01T18:00:00"},
        {"origin": {"iata_code": "DUB"}, "destination": {"iata_code": "LHR"}, "departing_at": "2030-02-02T08:00:00"},
    ]}],
}
PAX = {"title": "ms", "gender": "f", "given_name": "Ada", "family_name": "Lovelace",
       "born_on": "1990-01-01", "email": "a@example.com", "phone_number": "+15555550100"}


def response(data):
    return mock.MagicMock(__enter__=lambda s: io.BytesIO(json.dumps({"data": data}).encode()),
                          __exit__=lambda *a: False)


class DuffelTest(unittest.TestCase):
    def setUp(self):
        self.p = DuffelProvider("duffel_test_abc")

    def test_search_builds_round_trip_request_and_parses_offers(self):
        t = Tracker(origin="JFK", destination="LHR", depart_date="2030-02-01", return_date="2030-02-10",
                    adults=1, max_price=900, created_at=datetime.now(timezone.utc),
                    deadline=datetime.now(timezone.utc), max_connections=1)
        with mock.patch("urllib.request.urlopen", return_value=response({"offers": [OFFER]})) as m:
            offers = self.p.search(t)
        req = m.call_args[0][0]
        body = json.loads(req.data)["data"]
        self.assertEqual(req.get_header("Duffel-version"), "v2")
        self.assertEqual(len(body["slices"]), 2)
        self.assertEqual(body["slices"][1]["origin"], "LHR")
        self.assertEqual(body["max_connections"], 1)
        self.assertEqual(offers[0].price, 412.50)
        self.assertEqual(offers[0].passenger_ids, ["pas_1"])
        self.assertEqual(offers[0].summary, "JFK-DUB-LHR dep 2030-02-01T18:00:00")

    def test_book_sends_balance_payment_and_passenger_ids(self):
        offer = Offer("off_123", 412.5, "USD", "Duffel Airways", passenger_ids=["pas_1"])
        order = {"id": "ord_9", "booking_reference": "XYZ789", "total_amount": "412.50", "total_currency": "USD"}
        with mock.patch("urllib.request.urlopen", return_value=response(order)) as m:
            booking = self.p.book(offer, [PAX])
        body = json.loads(m.call_args[0][0].data)["data"]
        self.assertEqual(body["payments"], [{"type": "balance", "currency": "USD", "amount": "412.50"}])
        self.assertEqual(body["passengers"][0]["id"], "pas_1")
        self.assertEqual(booking.booking_reference, "XYZ789")

    def test_book_rejects_incomplete_passenger(self):
        offer = Offer("off_123", 412.5, "USD", "X", passenger_ids=["pas_1"])
        with self.assertRaises(ProviderError):
            self.p.book(offer, [{**PAX, "born_on": ""}])

    def test_http_errors_become_provider_errors(self):
        err = urllib.error.HTTPError("u", 422, "bad", {}, io.BytesIO(b'{"errors":[{"message":"Offer expired"}]}'))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaisesRegex(ProviderError, "Offer expired"):
                self.p.refresh_offer(Offer("off_1", 1, "USD", "X"))

    def test_requires_token(self):
        with self.assertRaises(ProviderError):
            DuffelProvider("")


if __name__ == "__main__":
    unittest.main()
