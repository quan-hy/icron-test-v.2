import io
import json
import os
import sys
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from dataclasses import replace
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler
from unittest.mock import patch

import forecast
from forecast import (
    GridCandidate,
    GridSample,
    NonRetryableError,
    NoRedirectHandler,
    SourceDataError,
    _run,
    _ecmwf_payload,
    _retry,
    DEFAULT_STEPS,
    expected_valid_at,
    iso_utc,
    kelvin_to_celsius,
    normalize_sst_sample,
    normalize_wave_step,
    normalize_wind_step,
    collect_noaa_spot,
    collect_ecmwf,
    main,
    parse_cycle,
    parse_sst_date,
    parse_steps,
    require_stable_grid,
    select_grid_values,
    wind_from_uv,
    build_parser,
    post_run,
    validate_ingest_origin,
    fetch_public_spots,
    parse_public_spots,
)


ISSUE = "2026-10-13T00:00:00Z"
VALID = "2026-10-13T03:00:00Z"
SPOT = {"id": "bali-kuta-beach", "lat": -8.7185, "lon": 115.1686}
MISSING = 9999.0


def json_bytes(value):
    return json.dumps(value, allow_nan=True).encode("utf-8")


def sample(
    short_name,
    value,
    *,
    units,
    level_type="surface",
    level=1,
    candidates=None,
    issued_at=ISSUE,
    valid_at=VALID,
):
    if candidates is None:
        candidates = (GridCandidate(-8.75, 115.25, value, 9.6),)
    return GridSample(
        short_name=short_name,
        level_type=level_type,
        level=level,
        units=units,
        issued_at=issued_at,
        valid_at=valid_at,
        missing_value=MISSING,
        candidates=tuple(candidates),
    )


class ForecastNormalizationTests(unittest.TestCase):
    def test_noaa_wave_directions_preserve_scalar_coming_from_values(self):
        records = [
            sample("swh", 2.15, units="m"),
            sample("perpw", 15.82, units="s"),
            sample("dirpw", 197.75, units="Degree true"),
            sample("shts", 2.13, units="m", level_type="orderedSequenceData"),
            sample("mpts", 15.82, units="s", level_type="orderedSequenceData"),
            sample("swdir", 198.83, units="Degree true", level_type="orderedSequenceData"),
        ]
        grid, values = normalize_wave_step(records, SPOT, ISSUE, VALID, ecmwf=False)
        by_name = {item["variable"]: item["value"] for item in values}

        self.assertEqual(grid, {"lat": -8.75, "lon": 115.25})
        self.assertEqual(by_name["wave_direction_deg"], 197.75)
        self.assertEqual(by_name["swell_direction_deg"], 198.83)
        self.assertEqual(by_name["wave_height_m"], 2.15)

    def test_ecmwf_surface_level_zero_and_direction_are_normalized(self):
        records = [
            sample("swh", 2.28, units="m", level=0),
            sample("mwp", 14.05, units="s", level=0),
            sample("mwd", 202.61, units="Degree true", level=0),
        ]
        grid, values = normalize_wave_step(records, SPOT, ISSUE, VALID, ecmwf=True)

        self.assertEqual(grid, {"lat": -8.75, "lon": 115.25})
        self.assertIn({"validAt": VALID, "variable": "wave_direction_deg", "value": 202.61}, values)
        self.assertIn({"validAt": VALID, "variable": "wave_period_s", "value": 14.05}, values)

    def test_missing_anchor_selects_nearest_valid_cell_and_omits_missing_fields(self):
        near_missing = GridCandidate(-8.5, 115.25, MISSING, 25.8)
        near_valid = GridCandidate(-8.75, 115.25, 2.4, 9.6)
        further = GridCandidate(-8.75, 115.0, 2.8, 18.8)
        fields = {
            "swh": sample("swh", 0, units="m", candidates=(near_missing, near_valid, further)),
            "perpw": sample(
                "perpw",
                0,
                units="s",
                candidates=(
                    GridCandidate(-8.5, 115.25, MISSING, 25.8),
                    GridCandidate(-8.75, 115.25, MISSING, 9.6),
                    GridCandidate(-8.75, 115.0, 15.0, 18.8),
                ),
            ),
        }

        raw, grid = select_grid_values(fields, "swh")

        self.assertEqual(grid, (-8.75, 115.25))
        self.assertEqual(raw, {"swh": 2.4})

    def test_wind_direction_is_from_and_calm_direction_is_missing(self):
        self.assertEqual(wind_from_uv(0, -3), (3.0, 0.0))
        self.assertEqual(wind_from_uv(-4, 0), (4.0, 90.0))
        self.assertEqual(wind_from_uv(0, 4), (4.0, 180.0))
        self.assertEqual(wind_from_uv(4, 0), (4.0, 270.0))
        speed, direction = wind_from_uv(0, 0)
        self.assertEqual(speed, 0)
        self.assertIsNone(direction)

        grid, values = normalize_wind_step(
            [
                sample("10u", -4, units="m s**-1", level_type="heightAboveGround", level=10),
                sample("10v", 0, units="m s**-1", level_type="heightAboveGround", level=10),
            ],
            SPOT,
            ISSUE,
            VALID,
        )
        self.assertEqual(grid, {"lat": -8.75, "lon": 115.25})
        self.assertIn({"validAt": VALID, "variable": "wind_direction_deg", "value": 90}, values)

    def test_new_wave_partitions_and_ecmwf_peak(self):
        records = [sample("swh", 2, units="m"),
                   sample("swdir", 220, units="degree true", level_type="orderedSequenceData", level=2),
                   sample("swdir", 180, units="degree true", level_type="orderedSequenceData", level=1),
                   sample("shww", 1, units="m"), sample("mpww", 6, units="s"),
                   sample("wvdir", 90, units="degree true")]
        _, values = normalize_wave_step(records, SPOT, ISSUE, VALID, ecmwf=False)
        by_name = {value["variable"]: value["value"] for value in values}
        self.assertEqual(by_name["swell_direction_deg"], 180)
        self.assertEqual(by_name["swell2_direction_deg"], 220)
        self.assertEqual(by_name["wind_wave_direction_deg"], 90)
        _, values = normalize_wave_step([sample("mwp", 9, units="s", level=0),
                                        sample("pp1d", 13, units="s", level=0)], SPOT, ISSUE, VALID, ecmwf=True)
        self.assertEqual({v["variable"]: v["value"] for v in values}, {"wave_period_s": 9, "peak_period_s": 13})

    def test_wind_additions_and_cumulative_precipitation_intervals(self):
        cycle = parse_cycle("2026101300")
        def records(step):
            valid = expected_valid_at(cycle, step)
            common = {"valid_at": valid}
            wind = [sample("10u", 3, units="m s**-1", level_type="heightAboveGround", level=10, **common),
                    sample("10v", 4, units="m s**-1", level_type="heightAboveGround", level=10, **common),
                    sample("gust", 8, units="m s**-1", level=0, **common),
                    sample("2t", 300, units="K", level_type="heightAboveGround", level=2, **common)]
            if step:
                rain = replace(sample("tp", {3: 2, 6: 5, 9: 4}[step], units="kg m**-2", level=0, **common),
                               start_step=0, end_step=step, step_type="accum", step_units="1")
                wind += [replace(rain, start_step=step - 3, candidates=(GridCandidate(-8.75, 115.25, 99, 9.6),)), rain] if step > 3 else [rain, rain]
            return wind
        current = {"step": 0}
        def download(url):
            if "gfswave" in url:
                raise HTTPError(url, 404, "Not Found", None, None)
            current["step"] = int(url.split(".f")[1][:3])
            return b"GRIB"
        with patch("forecast._download", side_effect=download), patch("forecast.decode_grib", side_effect=lambda *args: {SPOT["id"]: records(current["step"])}):
            runs, _ = collect_noaa_spot(SPOT, cycle, (0, 3, 6, 9))
        values = runs[0]["values"]
        rain = [v for v in values if v["variable"] == "precipitation_mm"]
        self.assertEqual([(v["validAt"], v["value"]) for v in rain], [(expected_valid_at(cycle, 3), 2), (expected_valid_at(cycle, 6), 3)])
        self.assertEqual(len([v for v in values if v["variable"] == "precipitation_interval_hours"]), 2)
        self.assertTrue(any(v["variable"] == "wind_gust_ms" and v["value"] == 8 for v in values))
        self.assertTrue(any(v["variable"] == "air_temperature_c" and abs(v["value"] - 26.85) < 1e-8 for v in values))

    def test_failed_previous_lead_does_not_create_a_long_interval(self):
        cycle = parse_cycle("2026101300")
        current = {"step": 0}
        def download(url):
            if "gfswave" in url:
                raise HTTPError(url, 404, "Not Found", None, None)
            current["step"] = int(url.split(".f")[1][:3])
            if current["step"] == 6:
                raise SourceDataError("bad lead")
            return b"GRIB"
        def decode(*args):
            step = current["step"]
            common = {"valid_at": expected_valid_at(cycle, step)}
            rain = replace(sample("tp", step, units="kg m**-2", level=0, **common),
                           start_step=0, end_step=step, step_type="accum", step_units="1")
            return {SPOT["id"]: [sample("10u", 1, units="m s**-1", level_type="heightAboveGround", level=10, **common),
                                  sample("10v", 1, units="m s**-1", level_type="heightAboveGround", level=10, **common), rain]}
        with patch("forecast._download", side_effect=download), patch("forecast.decode_grib", side_effect=decode), patch("forecast.time.sleep"):
            runs, _ = collect_noaa_spot(SPOT, cycle, (3, 6, 9, 12))
        rain = [v for v in runs[0]["values"] if v["variable"] == "precipitation_mm"]
        self.assertEqual([(v["validAt"], v["value"]) for v in rain], [(expected_valid_at(cycle, 3), 3), (expected_valid_at(cycle, 12), 3)])

    def test_precip_duplicate_and_time_unit_validation(self):
        base = replace(sample("tp", 2, units="kg m**-2", level=0), start_step=0, end_step=180, step_type="accum", step_units="0")
        specs = (forecast.WIND_FIELDS[-1],)
        self.assertEqual(forecast._samples_for_specs([base, base], specs, ISSUE, VALID)["precipitation_mm"], base)
        other = replace(base, candidates=(GridCandidate(-8.75, 115.25, 3, 9.6),))
        with self.assertRaisesRegex(SourceDataError, "inconsistent duplicate"):
            forecast._samples_for_specs([base, other], specs, ISSUE, VALID)
        with self.assertRaisesRegex(SourceDataError, "absent"):
            forecast._samples_for_specs([replace(base, step_units="unknown")], specs, ISSUE, VALID)

    def test_sst_kelvin_conversion_and_analysis_time(self):
        analysis_at = "2026-10-12T00:00:00Z"
        record = sample(
            "t",
            299.63,
            units="K",
            level=0,
            issued_at=analysis_at,
            valid_at=analysis_at,
        )
        grid, values = normalize_sst_sample([record], SPOT, analysis_at)

        self.assertEqual(grid, {"lat": -8.75, "lon": 115.25})
        self.assertAlmostEqual(values[0]["value"], 26.48)
        self.assertEqual(values[0]["validAt"], analysis_at)
        self.assertAlmostEqual(kelvin_to_celsius(299.63), 26.48)

    def test_issue_time_is_distinct_from_fetch_time(self):
        run = _run("spot", "noaa", "gfswave.global.0p25", ISSUE, "2026-10-13T04:12:00Z", None, [
            {"validAt": VALID, "variable": "wave_height_m", "value": 2.1}
        ])
        self.assertIsNotNone(run)
        self.assertEqual(run["issuedAt"], ISSUE)
        self.assertEqual(run["fetchedAt"], "2026-10-13T04:12:00Z")

    def test_grid_cell_must_stay_stable_in_one_run(self):
        first = {"lat": -8.75, "lon": 115.25}
        same_rounded_cell = {"lat": -8.75001, "lon": 115.25001}
        other_cell = {"lat": -8.75, "lon": 115.0}

        self.assertEqual(require_stable_grid(first, same_rounded_cell), first)
        with self.assertRaisesRegex(SourceDataError, "grid changed"):
            require_stable_grid(first, other_cell)

    def test_retry_is_limited_to_two_total_attempts(self):
        calls = 0

        def fail():
            nonlocal calls
            calls += 1
            raise OSError("unavailable")

        with patch("forecast.time.sleep"):
            with self.assertRaises(OSError):
                _retry(fail)
        self.assertEqual(calls, 2)

    def test_unavailable_lead_stops_that_noaa_product_but_keeps_wind(self):
        calls = []

        def download(url):
            calls.append(url)
            if "filter_gfswave.pl" in url:
                raise HTTPError(url, 404, "Not Found", None, None)
            return b"GRIB\x00"

        with (
            patch("forecast._download", side_effect=download),
            patch("forecast.decode_grib", return_value={SPOT["id"]: []}),
            patch(
                "forecast.normalize_wind_step",
                return_value=({"lat": -8.75, "lon": 115.25}, [{"validAt": VALID, "variable": "wind_speed_ms", "value": 3.0}]),
            ),
        ):
            runs, errors = collect_noaa_spot(SPOT, parse_cycle("2026101300"), (0, 3, 6))

        wave_requests = [url for url in calls if "filter_gfswave.pl" in url]
        wind_requests = [url for url in calls if "filter_gfs_0p25.pl" in url]
        self.assertEqual(len(wave_requests), 1)
        self.assertEqual(len(wind_requests), 3)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["model"], "gfs.0p25")
        self.assertEqual(errors[0]["error"], "HTTP 404")

    def test_unavailable_ecmwf_cycle_stops_later_leads(self):
        url_error = HTTPError("https://example.invalid/model", 404, "Not Found", None, None)
        class FakeSession:
            def get(self, *args, **kwargs):
                return None

            def head(self, *args, **kwargs):
                return None

        client_args = {}

        class FakeClient:
            def __init__(self, **kwargs):
                client_args.update(kwargs)
                self.session = FakeSession()

        ecmwf_module = types.ModuleType("ecmwf")
        open_data_module = types.ModuleType("ecmwf.opendata")
        open_data_module.Client = FakeClient
        ecmwf_module.opendata = open_data_module
        with (
            patch.dict(sys.modules, {"ecmwf": ecmwf_module, "ecmwf.opendata": open_data_module}),
            patch("forecast._ecmwf_payload", side_effect=url_error) as fetch,
        ):
            runs, errors = collect_ecmwf((SPOT,), parse_cycle("2026101300"), (0, 3, 6))

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(client_args["maximum_retries"], 0)
        self.assertEqual(runs, [])
        self.assertEqual(errors[0]["error"], "HTTP 404")

    def test_exhausted_transport_retries_stop_later_leads_for_that_product(self):
        calls = []

        def download(url):
            calls.append(url)
            if "filter_gfswave.pl" in url:
                raise OSError("offline")
            return b"GRIB\x00"

        with (
            patch("forecast.time.sleep"),
            patch("forecast._download", side_effect=download),
            patch("forecast.decode_grib", return_value={SPOT["id"]: []}),
            patch(
                "forecast.normalize_wind_step",
                return_value=({"lat": -8.75, "lon": 115.25}, [{"validAt": VALID, "variable": "wind_speed_ms", "value": 3.0}]),
            ),
        ):
            collect_noaa_spot(SPOT, parse_cycle("2026101300"), (0, 3, 6))

        wave_requests = [url for url in calls if "filter_gfswave.pl" in url]
        wind_requests = [url for url in calls if "filter_gfs_0p25.pl" in url]
        self.assertEqual(len(wave_requests), 2)
        self.assertEqual(len(wind_requests), 3)

    def test_ecmwf_attribution_does_not_pollute_json_stdout(self):
        class FakeClient:
            def retrieve(self, **kwargs):
                print("ECMWF attribution")
                Path(kwargs["target"]).write_bytes(b"GRIB\x00")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            payload = _ecmwf_payload(FakeClient(), datetime(2026, 10, 13, tzinfo=UTC), 3)

        self.assertEqual(payload, b"GRIB\x00")
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("ECMWF attribution", stderr.getvalue())

    def test_ingest_origin_accepts_https_and_loopback_http_only(self):
        self.assertEqual(
            validate_ingest_origin("https://ingest.example.test/"),
            "https://ingest.example.test",
        )
        self.assertEqual(
            validate_ingest_origin("http://127.0.0.1:8787"),
            "http://127.0.0.1:8787",
        )
        self.assertEqual(validate_ingest_origin("http://[::1]:8787/"), "http://[::1]:8787")
        for invalid in (
            "http://ingest.example.test",
            "https://user:pass@ingest.example.test",
            "https://@ingest.example.test",
            "https://ingest.example.test/api",
            "https://ingest.example.test?",
            "https://ingest.example.test#",
            "https://bad host.test",
            "https://ingest.example.test..",
            "https://ingest.example.test:bad",
            "https://999.1.1.1",
            "https://ingest.example.test\u0085",
            "https://ingest.example.test\x00",
        ):
            with self.subTest(url=invalid), self.assertRaises(ValueError):
                validate_ingest_origin(invalid)

    def test_external_http_is_rejected_before_forecast_collection(self):
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {"INGEST_URL": "http://ingest.example.test", "INGEST_TOKEN": "dummy-test-token"}),
            patch("forecast.collect_noaa_cluster") as noaa,
            patch("forecast.collect_sst") as sst,
            patch("forecast.collect_ecmwf") as ecmwf,
            patch("forecast.build_opener") as opener,
            redirect_stderr(stderr),
            self.assertRaises(SystemExit),
        ):
            main(["--model-cycle", "2026101300", "--post"])

        self.assertIn("HTTP is allowed only", stderr.getvalue())
        noaa.assert_not_called()
        sst.assert_not_called()
        ecmwf.assert_not_called()
        opener.assert_not_called()

    def test_post_refuses_redirect_without_forwarding_token(self):
        requests = []
        handlers = []

        class FakeOpener:
            def open(self, request, timeout):
                requests.append((request, timeout))
                raise HTTPError(request.full_url, 302, "Found", None, None)

        run = {
            "spotId": "spot",
            "source": "noaa",
            "model": "gfs.0p25",
            "issuedAt": ISSUE,
            "fetchedAt": VALID,
            "grid": None,
            "values": [{"validAt": VALID, "variable": "wind_speed_ms", "value": 3.0}],
        }
        with patch("forecast.build_opener", side_effect=lambda *args: (handlers.extend(args), FakeOpener())[1]):
            with self.assertRaisesRegex(NonRetryableError, "redirects are not allowed"):
                post_run(run, "https://ingest.example.test/", "dummy-test-token")

        self.assertEqual(len(requests), 1)
        request, timeout = requests[0]
        self.assertEqual(request.full_url, "https://ingest.example.test/ingest/forecast")
        self.assertEqual(request.get_header("Authorization"), "Bearer dummy-test-token")
        self.assertEqual(timeout, 30)
        no_redirect = next(handler for handler in handlers if isinstance(handler, NoRedirectHandler))
        self.assertIsNone(
            no_redirect.redirect_request(
                request, object(), 307, "Temporary Redirect", {}, "https://attacker.example.test/collect"
            )
        )

    def test_loopback_http_ingest_disables_environment_proxies(self):
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
                self.request = request
                return Response()

        with patch("forecast.build_opener", side_effect=lambda *args: (handlers.extend(args), FakeOpener())[1]) as opener:
            post_run({"values": []}, "http://localhost:8787", "dummy-test-token")

        proxies = [handler for handler in handlers if isinstance(handler, ProxyHandler)]
        self.assertEqual(len(proxies), 1)
        self.assertEqual(proxies[0].proxies, {})
        self.assertEqual(opener.call_count, 1)

    def test_unit_and_time_mismatches_are_rejected(self):
        with self.assertRaisesRegex(SourceDataError, "unexpected units"):
            normalize_wave_step(
                [sample("swh", 5, units="ft")], SPOT, ISSUE, VALID, ecmwf=False
            )
        wrong_time = sample("swh", 2.1, units="m", issued_at="2026-10-12T18:00:00Z")
        with self.assertRaisesRegex(SourceDataError, "issue or valid time"):
            normalize_wave_step([wrong_time], SPOT, ISSUE, VALID, ecmwf=False)

    def test_cycle_and_steps_are_strict_and_bounded(self):
        self.assertEqual(iso_utc(parse_cycle("2026101300")), ISSUE)
        self.assertEqual(parse_steps("0,3,6,90"), (0, 3, 6, 90))
        self.assertEqual(parse_steps(build_parser().get_default("steps")), DEFAULT_STEPS)
        self.assertEqual(len(DEFAULT_STEPS), 40)
        self.assertEqual(DEFAULT_STEPS[-1], 144)
        maximum_steps = parse_steps(",".join(str(step) for step in range(0, 145, 3)))
        self.assertEqual(len(maximum_steps), 49)
        self.assertEqual(iso_utc(parse_sst_date("20261012")), "2026-10-12T00:00:00Z")
        for bad_cycle in ("2026101301", "2026023000", "202610130"):
            with self.subTest(cycle=bad_cycle), self.assertRaises(ValueError):
                parse_cycle(bad_cycle)
        too_many = ",".join(str(step) for step in range(0, 148, 3))
        for bad_steps in ("", "3,0", "0,3,3", "0,4", "147", too_many):
            with self.subTest(steps=bad_steps), self.assertRaises(ValueError):
                parse_steps(bad_steps)
        self.assertEqual(expected_valid_at(parse_cycle("2026101300"), 3), VALID)


class PublicSpotInputTests(unittest.TestCase):
    def test_parser_accepts_pacific_regions_without_lifting_the_catalog_limit(self):
        regions = ("hawaii", "japan", "australia", "new-zealand", "tahiti")
        records = [{"id": f"pacific-{i}", "region": region, "lat": -17.8, "lon": 177.2}
                   for i, region in enumerate(regions)]
        self.assertEqual(parse_public_spots(json_bytes({"spots": records})), tuple(records))

    def test_parser_collects_every_public_record_regardless_of_review(self):
        payload = b'{"spots":[{"id":"draft-spot","region":"bali","lat":-8.7,"lon":115,"review_status":"unreviewed"},{"id":"reviewed-bali","region":"bali","lat":-8.7185,"lon":115.1686,"review_status":"reviewed"}]}'

        self.assertEqual(
            parse_public_spots(payload),
            (
                {"id": "draft-spot", "region": "bali", "lat": -8.7, "lon": 115},
                {"id": "reviewed-bali", "region": "bali", "lat": -8.7185, "lon": 115.1686},
            ),
        )

    def test_parser_rejects_invalid_reviewed_spot_records(self):
        valid = {"id": "reviewed-bali", "region": "bali", "lat": -8.7, "lon": 115.1, "review_status": "reviewed"}
        invalid_records = (
            {**valid, "id": "Uppercase"},
            {**valid, "id": "x" * 65},
            {**valid, "region": "unknown"},
            {**valid, "lat": "-8.7"},
            {**valid, "lon": True},
            {**valid, "lat": 90.1},
            {**valid, "lon": -180.1},
            {**valid, "lat": float("nan")},
            {**valid, "lon": float("inf")},
            {**valid, "lat": 10**400},
        )
        for record in invalid_records:
            with self.subTest(record=record), self.assertRaises(NonRetryableError):
                parse_public_spots(json_bytes({"spots": [record]}))

        for records in (
            [],
            [valid, valid],
            [
                {**valid, "id": f"spot-{index}"}
                for index in range(31)
            ],
        ):
            with self.subTest(records=len(records)), self.assertRaises(NonRetryableError):
                parse_public_spots(json_bytes({"spots": records}))

    def test_fetch_uses_public_get_without_auth_and_reuses_origin_safety(self):
        handlers = []
        requests = []

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, size):
                self.read_size = size
                return b'{"spots":[{"id":"release-spot","region":"california","lat":37.5,"lon":-122.5,"review_status":"reviewed"}]}'

        class FakeOpener:
            def open(self, request, timeout):
                requests.append((request, timeout))
                return Response()

        with patch("forecast.build_opener", side_effect=lambda *args: (handlers.extend(args), FakeOpener())[1]):
            spots = fetch_public_spots("https://ingest.example.test/")

        self.assertEqual(spots[0]["id"], "release-spot")
        request, timeout = requests[0]
        self.assertEqual(request.full_url, "https://ingest.example.test/spots")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(timeout, 30)
        self.assertEqual(sum(isinstance(handler, NoRedirectHandler) for handler in handlers), 1)
        self.assertFalse(any(isinstance(handler, ProxyHandler) for handler in handlers))

    def test_loopback_reviewed_spot_get_disables_environment_proxies(self):
        handlers = []

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, size):
                return b'{"spots":[{"id":"local-spot","region":"korea","lat":37.5,"lon":127.0,"review_status":"reviewed"}]}'

        class FakeOpener:
            def open(self, request, timeout):
                return Response()

        with patch("forecast.build_opener", side_effect=lambda *args: (handlers.extend(args), FakeOpener())[1]):
            self.assertEqual(fetch_public_spots("http://localhost:8787")[0]["id"], "local-spot")

        proxies = [handler for handler in handlers if isinstance(handler, ProxyHandler)]
        self.assertEqual(len(proxies), 1)
        self.assertEqual(proxies[0].proxies, {})

    def test_fetch_rejects_redirects_and_oversized_public_lists(self):
        class Response:
            def __init__(self, status, payload):
                self.status = status
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, size):
                self.read_size = size
                return self.payload

        class FakeOpener:
            def __init__(self, response):
                self.response = response

            def open(self, request, timeout):
                return self.response

        for response, message in (
            (Response(302, b""), "redirects are not allowed"),
            (Response(200, b"x" * (1024 * 1024 + 1)), "1 MiB"),
        ):
            with (
                self.subTest(message=message),
                patch("forecast.build_opener", return_value=FakeOpener(response)),
                self.assertRaisesRegex(NonRetryableError, message),
            ):
                fetch_public_spots("https://ingest.example.test")

    def test_reviewed_mode_accepts_dynamic_ids_and_passes_coordinates_to_all_sources(self):
        spot = {"id": "release-spot", "region": "california", "lat": 37.5, "lon": -122.5}
        empty_result = ([], [])
        with (
            patch.dict(os.environ, {"INGEST_URL": "https://ingest.example.test"}, clear=True),
            patch("forecast.fetch_public_spots", return_value=(spot,)),
            patch("forecast.collect_noaa_cluster", return_value=empty_result) as noaa,
            patch("forecast.collect_sst", return_value=empty_result) as sst,
            patch("forecast.collect_ecmwf", return_value=empty_result) as ecmwf,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                main(["--model-cycle", "2026101300", "--sst-date", "20261012", "--public-spots", "--spot", "release-spot"]),
                0,
            )

        noaa.assert_called_once_with((spot,), parse_cycle("2026101300"), DEFAULT_STEPS)
        sst.assert_called_once_with((spot,), parse_sst_date("20261012"))
        ecmwf.assert_called_once_with((spot,), parse_cycle("2026101300"), DEFAULT_STEPS)

    def test_post_budget_checks_the_entire_batch_before_any_write(self):
        for count, expected_status in ((forecast.MAX_POST_VALUES, 0), (forecast.MAX_POST_VALUES + 1, 1)):
            runs = [{"spotId": SPOT["id"], "source": "noaa", "model": "gfs.0p25",
                     "values": [{"validAt": VALID, "variable": "wind_gust_ms", "value": 5}] * count}]
            with (
                self.subTest(count=count),
                patch.dict(os.environ, {"INGEST_URL": "https://ingest.example.test", "INGEST_TOKEN": "test-token"}, clear=True),
                patch("forecast.collect_noaa_cluster", return_value=(runs, [])),
                patch("forecast.collect_sst", return_value=([], [])),
                patch("forecast.post_run") as post,
                redirect_stdout(io.StringIO()),
            ):
                status = main(["--model-cycle", "2026101300", "--spot", SPOT["id"], "--source", "noaa", "--post"])
                self.assertEqual(status, expected_status)
                self.assertEqual(post.call_count, 1 if expected_status == 0 else 0)

    def test_reviewed_mode_requires_url_and_never_falls_back_to_fixtures(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("forecast.fetch_public_spots") as fetch,
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            main(["--model-cycle", "2026101300", "--public-spots"])
        fetch.assert_not_called()

        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {"INGEST_URL": "https://ingest.example.test"}, clear=True),
            patch("forecast.fetch_public_spots", side_effect=NonRetryableError("spot list contains no public spots")),
            patch("forecast.collect_noaa_cluster") as noaa,
            patch("forecast.collect_sst") as sst,
            patch("forecast.collect_ecmwf") as ecmwf,
            redirect_stderr(stderr),
            self.assertRaises(SystemExit),
        ):
            main(["--model-cycle", "2026101300", "--public-spots"])

        self.assertIn("no public spots", stderr.getvalue())
        noaa.assert_not_called()
        sst.assert_not_called()
        ecmwf.assert_not_called()

    def test_reviewed_mode_sanitizes_credentials_from_origin_errors(self):
        username = "private-user"
        password = "private-password"
        token = "private-ingest-token"
        stderr = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "INGEST_URL": f"https://{username}:{password}@ingest.example.test",
                    "INGEST_TOKEN": token,
                },
                clear=True,
            ),
            patch("forecast.fetch_public_spots") as fetch,
            redirect_stderr(stderr),
            self.assertRaises(SystemExit),
        ):
            main(["--model-cycle", "2026101300", "--public-spots", "--post"])

        fetch.assert_not_called()
        for secret in (username, password, token):
            self.assertNotIn(secret, stderr.getvalue())

    def test_slow_nomads_response_does_not_block_another_paced_start(self):
        first_started = threading.Event()
        second_started = threading.Event()
        starts = []
        guard = threading.Lock()

        def open_nomads(request, timeout):
            with guard:
                first = not starts
                starts.append(request.full_url)
            if first:
                first_started.set()
                if not second_started.wait(5):
                    raise AssertionError("HTTP response wait serialized independent requests")
            else:
                second_started.set()
            return io.BytesIO(b"GRIB")

        with patch.object(forecast, "_nomads_next_start", 0.0), patch("forecast.urlopen", side_effect=open_nomads):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(forecast._download, f"{forecast.NOMADS}/first.grib2")
                self.assertTrue(first_started.wait(5))
                second = executor.submit(forecast._download, f"{forecast.NOMADS}/second.grib2")
                self.assertEqual(first.result(timeout=6), b"GRIB")
                self.assertEqual(second.result(timeout=6), b"GRIB")
        self.assertEqual(len(starts), 2)

    def test_nomads_attempts_are_paced_across_threads_and_retries_only(self):
        class FakeClock:
            def __init__(self):
                self.now = 100.0
                self.lock = threading.Lock()
                self.starts = []

            def monotonic(self):
                with self.lock:
                    return self.now

            def sleep(self, seconds):
                with self.lock:
                    self.now += seconds

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, size):
                return b"GRIB"

        clock = FakeClock()
        barrier = threading.Barrier(3)

        def open_nomads(request, timeout):
            clock.starts.append(clock.monotonic())
            return Response()

        def download_together():
            barrier.wait()
            return forecast._download(f"{forecast.NOMADS}/test.grib2")

        with (
            patch.object(forecast, "_nomads_next_start", 0.0),
            patch.object(forecast.time, "monotonic", clock.monotonic),
            patch.object(forecast.time, "sleep", clock.sleep),
            patch("forecast.urlopen", side_effect=open_nomads),
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(download_together) for _ in range(2)]
                barrier.wait()
                self.assertEqual([future.result() for future in futures], [b"GRIB", b"GRIB"])

        self.assertEqual(len(clock.starts), 2)
        self.assertGreaterEqual(max(clock.starts) - min(clock.starts), 0.51)

        clock = FakeClock()
        retry_starts = []
        attempts = 0

        def fail_once(request, timeout):
            nonlocal attempts
            attempts += 1
            retry_starts.append(clock.monotonic())
            if attempts == 1:
                raise OSError("temporary")
            return Response()

        with (
            patch.object(forecast, "_nomads_next_start", 0.0),
            patch.object(forecast.time, "monotonic", clock.monotonic),
            patch.object(forecast.time, "sleep", clock.sleep),
            patch("forecast.urlopen", side_effect=fail_once),
        ):
            forecast._retry(lambda: forecast._download(f"{forecast.NOMADS}/retry.grib2"))

        self.assertEqual(attempts, 2)
        self.assertGreaterEqual(retry_starts[1] - retry_starts[0], 0.51)

        clock = FakeClock()
        sleep_calls = []

        def record_sleep(seconds):
            sleep_calls.append(seconds)
            clock.sleep(seconds)

        with (
            patch.object(forecast, "_nomads_next_start", clock.now + 10),
            patch.object(forecast.time, "monotonic", clock.monotonic),
            patch.object(forecast.time, "sleep", record_sleep),
            patch("forecast.urlopen", side_effect=open_nomads),
        ):
            forecast._download("https://example.test/data.grib2")
        self.assertEqual(sleep_calls, [])


if __name__ == "__main__":
    unittest.main()


class ClusterAndCycleTests(unittest.TestCase):
    def test_provider_opener_rejects_redirects_without_an_extra_transaction(self):
        error = HTTPError("https://provider.example/", 302, "redirect", None, None)
        with patch("forecast.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(HTTPError):
                forecast.urlopen(forecast.Request("https://provider.example/"), 60)
        self.assertIsInstance(opener.call_args.args[0], forecast.NoRedirectHandler)
        opener.return_value.open.assert_called_once()

    def test_clusters_are_deterministic_and_boxes_contain_spots(self):
        spots = ({"id": "a", "region": "x", "lat": 0, "lon": 179.8},
                 {"id": "b", "region": "x", "lat": 0.2, "lon": -179.8},
                 {"id": "c", "region": "x", "lat": 0.50003, "lon": 179.60003},
                 {"id": "d", "region": "y", "lat": 0.50003, "lon": 179.60003})
        clusters = forecast.cluster_spots(spots)
        self.assertEqual(clusters, forecast.cluster_spots(tuple(reversed(spots))))
        self.assertEqual(sorted(len(c) for c in clusters), [1, 1, 2])
        for cluster in clusters:
            box = forecast._small_box(cluster)
            for spot in cluster:
                self.assertLessEqual(float(box["leftlon"]), spot["lon"] - forecast.BOX_DEGREES)
                self.assertGreaterEqual(float(box["rightlon"]), spot["lon"] + forecast.BOX_DEGREES)
                self.assertLessEqual(float(box["bottomlat"]), spot["lat"] - forecast.BOX_DEGREES)
                self.assertGreaterEqual(float(box["toplat"]), spot["lat"] + forecast.BOX_DEGREES)

    def test_cluster_failure_is_isolated(self):
        spots = ({"id": "a", "region": "x", "lat": 0, "lon": 0},
                 {"id": "b", "region": "x", "lat": 1, "lon": 1},
                 {"id": "c", "region": "x", "lat": 8, "lon": 8})
        clusters = forecast.cluster_spots(spots)
        with patch("forecast._download", side_effect=HTTPError("url", 404, "missing", None, None)):
            _, errors = forecast.collect_noaa_cluster(clusters[0], parse_cycle("2026101300"), (0, 3))
        self.assertEqual({e["spotId"] for e in errors}, {"a", "b"})
        self.assertEqual(len(errors), 4)

    def test_cycle_selection_and_transport_failure(self):
        now = datetime(2026, 11, 1, 11, tzinfo=UTC)
        with patch("forecast._index_exists", return_value=True):
            self.assertEqual(forecast.select_cycle("noaa", now, 144), parse_cycle("2026110106"))
        with patch("forecast._index_exists", side_effect=[False, True]):
            self.assertEqual(forecast.select_cycle("ecmwf", now, 144), parse_cycle("2026110100"))
        with patch("forecast._index_exists", side_effect=forecast.URLError("offline")):
            with self.assertRaises(forecast.URLError):
                forecast.select_cycle("ecmwf", now, 144)
        with patch("forecast._index_exists", return_value=False):
            with self.assertRaises(SourceDataError):
                forecast.select_cycle("ecmwf", now, 144)

    def test_ecmwf_index_path_matches_client_version(self):
        url, = forecast._index_urls("ecmwf", parse_cycle("2026110106"), 144)
        self.assertEqual(url, "https://data.ecmwf.int/forecasts/20261101/06z/ifs/0p25/wave/20261101060000-144h-wave-fc.index")

    def test_ecmwf_counts_each_session_send(self):
        class Session:
            def send(self, *args, **kwargs): return None
            def get(self, *args, **kwargs): return self.send()
            def head(self, *args, **kwargs): return self.send()
        client = types.SimpleNamespace(session=Session())
        before = forecast._request_counts["ecmwf"]
        forecast._set_ecmwf_timeouts(client)
        client.session.get("url")
        client.session.head("url")
        self.assertEqual(forecast._request_counts["ecmwf"] - before, 2)

    def test_noaa_requires_both_last_lead_indexes(self):
        now = datetime(2026, 11, 1, 11, tzinfo=UTC)
        with patch("forecast._index_exists", side_effect=[True, False, True, True]) as probe:
            self.assertEqual(forecast.select_cycle("noaa", now, 144), parse_cycle("2026110100"))
        self.assertEqual(probe.call_count, 4)
        wave, wind = forecast._index_urls("noaa", parse_cycle("2026110100"), 144)
        self.assertTrue(wave.endswith("/wave/gridded/gfswave.t00z.global.0p25.f144.grib2.idx"))
        self.assertTrue(wind.endswith("/atmos/gfs.t00z.pgrb2.0p25.f144.idx"))
