import io
import json
import os
import unittest
from contextlib import redirect_stderr
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler
from unittest.mock import patch

import tides
from forecast import NoRedirectHandler, NonRetryableError, SourceDataError


START = datetime(2026, 10, 13, tzinfo=UTC)
END = START + timedelta(hours=2)


def response(data, **extra):
    return json.dumps({"predictions": data, **extra}).encode()


class TideMappingAndHorizonTests(unittest.TestCase):
    def test_mapping_requires_explicit_unique_safe_spot_ids(self):
        self.assertEqual(
            tides.parse_mappings(["ca-linda-mar=9414290", "ca-doheny=9411340"]),
            (("ca-linda-mar", "9414290"), ("ca-doheny", "9411340")),
        )
        for mappings in (
            [],
            ["ca-linda-mar=9414290", "ca-linda-mar=9411340"],
            ["=9414290"],
            ["ca linda=9414290"],
            ["ca-linda-mar="],
            ["ca-linda-mar=9414290=extra"],
            [" ca-linda-mar=9414290"],
            ["CA-linda-mar=9414290"],
            ["ca_linda_mar=9414290"],
            ["ca-linda-mar=cb0102"],
        ):
            with self.subTest(mappings=mappings), self.assertRaises(ValueError):
                tides.parse_mappings(mappings)
        too_many = [f"spot-{index}=9414290" for index in range(tides.MAX_MAPPINGS + 1)]
        with self.assertRaisesRegex(ValueError, "At most"):
            tides.parse_mappings(too_many)

    def test_cli_requires_operator_supplied_mapping(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            tides.build_parser().parse_args([])
        args = tides.build_parser().parse_args(["--mapping", "ca-linda-mar=9414290"])
        self.assertEqual(args.mapping, ["ca-linda-mar=9414290"])
        self.assertEqual(args.hours, tides.DEFAULT_HORIZON_HOURS)
        self.assertFalse(args.post)

    def test_horizon_is_bounded_and_request_window_is_utc_hour_aligned(self):
        self.assertEqual(tides.validate_horizon(1), 1)
        self.assertEqual(tides.validate_horizon(72), 72)
        self.assertEqual(tides.validate_horizon(144), 144)
        start, end = tides.request_window(datetime(2026, 10, 13, 10, 47, tzinfo=UTC), 72)
        self.assertEqual(start, datetime(2026, 10, 13, 10, tzinfo=UTC))
        self.assertEqual(end, datetime(2026, 10, 16, 10, tzinfo=UTC))
        for invalid in (0, -1, 145, True, 1.5):
            with self.subTest(hours=invalid), self.assertRaises(ValueError):
                tides.validate_horizon(invalid)
        with self.assertRaisesRegex(ValueError, "timezone"):
            tides.request_window(datetime(2026, 10, 13, 10), 1)

    def test_noaa_query_uses_exact_hourly_metric_gmt_mllw_prediction_options(self):
        url = tides.build_url("9414290", START, END)
        parsed = urlsplit(url)
        self.assertEqual(urlsplit(url).scheme, "https")
        self.assertEqual(parsed.netloc, "api.tidesandcurrents.noaa.gov")
        self.assertEqual(
            parse_qs(parsed.query),
            {
                "station": ["9414290"],
                "begin_date": ["20261013 00:00"],
                "end_date": ["20261013 02:00"],
                "product": ["predictions"],
                "datum": ["MLLW"],
                "time_zone": ["gmt"],
                "units": ["metric"],
                "interval": ["h"],
                "format": ["json"],
                "application": ["icron test v.2"],
            },
        )


class TideResponseTests(unittest.TestCase):
    def test_public_noaa_sample_shape_uses_predictions_without_required_metadata(self):
        payload = (
            b'{ "predictions" : [\n'
            b'{"t":"2026-10-13 00:00", "v":"0.482"},'
            b'{"t":"2026-10-13 01:00", "v":"0.14"}\n]}'
        )
        result = tides.parse_predictions(
            payload,
            "9414290",
            START,
            START + timedelta(hours=1),
        )
        self.assertEqual(
            result,
            [
                {"validAt": "2026-10-13T00:00:00Z", "heightM": 0.482},
                {"validAt": "2026-10-13T01:00:00Z", "heightM": 0.14},
            ],
        )

    def test_provider_times_become_canonical_utc_and_metric_height_is_retained(self):
        result = tides.parse_predictions(
            response(
                [
                    {"t": "2026-10-13 02:00", "v": "0.125"},
                    {"t": "2026-10-13 00:00", "v": "-0.35"},
                ]
            ),
            "9414290",
            START,
            END,
        )
        self.assertEqual(
            result,
            [
                {"validAt": "2026-10-13T00:00:00Z", "heightM": -0.35},
                {"validAt": "2026-10-13T02:00:00Z", "heightM": 0.125},
            ],
        )

    def test_missing_and_nonfinite_heights_are_omitted_without_substitution(self):
        result = tides.parse_predictions(
            response(
                [
                    {"t": "2026-10-13 00:00"},
                    {"t": "2026-10-13 01:00", "v": ""},
                    {"t": "2026-10-13 02:00", "v": "NaN"},
                ]
            ),
            "9414290",
            START,
            END,
        )
        self.assertEqual(result, [])

    def test_invalid_provider_responses_and_rows_fail_closed(self):
        invalid_payloads = (
            b"not json",
            json.dumps({"error": {"message": "bad station"}}).encode(),
            json.dumps({"data": "not an array"}).encode(),
            response([{"t": "2026-10-13 00:00", "v": "1"}], metadata={"id": "9414291"}),
            response([{"t": "2026-10-13 03:00", "v": "1"}]),
            response([{"t": "2026-10-13 01:30", "v": "1"}]),
            response([{"t": "2026-10-13 00:00", "v": "not-a-number"}]),
            response(["invalid row"]),
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(SourceDataError):
                tides.parse_predictions(payload, "9414290", START, END)

    def test_response_record_count_cannot_exceed_requested_hourly_horizon(self):
        with self.assertRaisesRegex(SourceDataError, "horizon"):
            tides.parse_predictions(
                response(
                    [
                        {"t": "2026-10-13 00:00", "v": "0"},
                        {"t": "2026-10-13 01:00", "v": "0"},
                        {"t": "2026-10-13 02:00", "v": "0"},
                        {"t": "2026-10-13 03:00", "v": "0"},
                    ]
                ),
                "9414290",
                START,
                END,
            )

    def test_collector_emits_shared_contract_and_omits_empty_prediction_payload(self):
        provider_data = response([{"t": "2026-10-13 00:00", "v": "-0.25"}])
        with patch("tides._retry", return_value=provider_data):
            payload = tides.collect_predictions("ca-linda-mar", "9414290", START, END)
        self.assertEqual(
            set(payload),
            {"spotId", "source", "stationId", "datum", "intervalMinutes", "fetchedAt", "values"},
        )
        self.assertEqual(payload["spotId"], "ca-linda-mar")
        self.assertEqual(payload["source"], "noaa-coops")
        self.assertEqual(payload["stationId"], "9414290")
        self.assertEqual(payload["datum"], "MLLW")
        self.assertEqual(payload["intervalMinutes"], 60)
        self.assertRegex(payload["fetchedAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(payload["values"], [{"validAt": "2026-10-13T00:00:00Z", "heightM": -0.25}])

        with patch("tides._retry", return_value=response([{"t": "2026-10-13 00:00", "v": ""}])):
            with self.assertRaisesRegex(SourceDataError, "no valid tide predictions"):
                tides.collect_predictions("ca-linda-mar", "9414290", START, END)


class TideTransportTests(unittest.TestCase):
    def test_provider_transport_rejects_redirect(self):
        opener_handlers = []

        class FakeOpener:
            def open(self, request, timeout):
                self.request = request
                self.timeout = timeout
                raise HTTPError(request.full_url, 302, "Found", None, None)

        fake_opener = FakeOpener()
        with patch(
            "tides.build_opener",
            side_effect=lambda *handlers: (opener_handlers.extend(handlers), fake_opener)[1],
        ):
            with self.assertRaisesRegex(NonRetryableError, "redirects are not allowed"):
                tides.fetch_bytes("https://api.example.test/data")
        self.assertTrue(any(isinstance(handler, NoRedirectHandler) for handler in opener_handlers))
        self.assertEqual(fake_opener.timeout, tides.REQUEST_TIMEOUT_SECONDS)

    def test_provider_response_read_is_bounded(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, size):
                self.size = size
                return b"x" * size

        class FakeOpener:
            def open(self, request, timeout):
                self.response = FakeResponse()
                return self.response

        fake_opener = FakeOpener()
        with patch("tides.build_opener", return_value=fake_opener):
            with self.assertRaisesRegex(SourceDataError, "download limit"):
                tides.fetch_bytes("https://api.example.test/data")
        self.assertEqual(fake_opener.response.size, tides.MAX_RESPONSE_BYTES + 1)

    def test_ingest_uses_validated_https_origin_endpoint_and_preserves_bearer_token(self):
        opener_handlers = []

        class Response:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, size):
                return b""

        class FakeOpener:
            def open(self, request, timeout):
                self.request = request
                self.timeout = timeout
                return Response()

        fake_opener = FakeOpener()
        with patch(
            "tides.build_opener",
            side_effect=lambda *handlers: (opener_handlers.extend(handlers), fake_opener)[1],
        ):
            tides.post_predictions({"values": []}, "https://ingest.example.test:8443/", "dummy-test-token")

        self.assertEqual(fake_opener.request.full_url, "https://ingest.example.test:8443/ingest/tides")
        self.assertEqual(fake_opener.request.get_header("Authorization"), "Bearer dummy-test-token")
        self.assertEqual(fake_opener.timeout, tides.REQUEST_TIMEOUT_SECONDS)
        self.assertTrue(any(isinstance(handler, NoRedirectHandler) for handler in opener_handlers))

    def test_ingest_rejects_redirect_without_forwarding_token_to_new_origin(self):
        calls = []
        opener_handlers = []

        class FakeOpener:
            def open(self, request, timeout):
                calls.append((request, timeout))
                raise HTTPError(request.full_url, 307, "Temporary Redirect", None, None)

        with patch(
            "tides.build_opener",
            side_effect=lambda *handlers: (opener_handlers.extend(handlers), FakeOpener())[1],
        ):
            with self.assertRaisesRegex(NonRetryableError, "redirects are not allowed"):
                tides.post_predictions({"values": []}, "https://ingest.example.test", "dummy-test-token")
        self.assertEqual(len(calls), 1)
        request, _timeout = calls[0]
        self.assertEqual(request.full_url, "https://ingest.example.test/ingest/tides")
        self.assertEqual(request.get_header("Authorization"), "Bearer dummy-test-token")
        no_redirect = next(handler for handler in opener_handlers if isinstance(handler, NoRedirectHandler))
        self.assertIsNone(
            no_redirect.redirect_request(
                request,
                object(),
                307,
                "Temporary Redirect",
                {},
                "https://attacker.example.test/collect",
            )
        )

    def test_loopback_ingest_disables_environment_proxies(self):
        handlers = []

        class Response:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, size):
                return b""

        class FakeOpener:
            def open(self, request, timeout):
                return Response()

        with patch(
            "tides.build_opener",
            side_effect=lambda *items: (handlers.extend(items), FakeOpener())[1],
        ):
            tides.post_predictions({"values": []}, "http://127.0.0.1:8787", "dummy-test-token")
        proxies = [handler for handler in handlers if isinstance(handler, ProxyHandler)]
        self.assertEqual(len(proxies), 1)
        self.assertEqual(proxies[0].proxies, {})

    def test_post_flag_rejects_invalid_origin_before_provider_fetch(self):
        stderr = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {"INGEST_URL": "http://ingest.example.test", "INGEST_TOKEN": "dummy-test-token"},
            ),
            patch("tides.collect_predictions") as collect,
            redirect_stderr(stderr),
            self.assertRaises(SystemExit),
        ):
            tides.main(["--mapping", "ca-linda-mar=9414290", "--post"])
        self.assertIn("HTTP is allowed only", stderr.getvalue())
        collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
