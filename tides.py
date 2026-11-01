#!/usr/bin/env python3
"""Collect hourly NOAA CO-OPS tide predictions for operator-mapped spots."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
import math
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from forecast import (
    NoRedirectHandler,
    NonRetryableError,
    SourceDataError,
    _retry,
    iso_utc,
    validate_ingest_origin,
)


NOAA_API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
MAX_HORIZON_HOURS = 144
DEFAULT_HORIZON_HOURS = 144
MAX_MAPPINGS = 30
MAX_RESPONSE_BYTES = 128 * 1024
REQUEST_TIMEOUT_SECONDS = 30


def parse_mappings(values: list[str]) -> tuple[tuple[str, str], ...]:
    """Parse explicit SPOT_ID=STATION_ID pairs without inventing defaults."""
    if not values:
        raise ValueError("At least one --mapping SPOT_ID=STATION_ID is required.")
    if len(values) > MAX_MAPPINGS:
        raise ValueError(f"At most {MAX_MAPPINGS} spot-to-station mappings are allowed.")

    mappings: list[tuple[str, str]] = []
    seen_spots: set[str] = set()
    for value in values:
        if value != value.strip() or value.count("=") != 1:
            raise ValueError("Each mapping must be SPOT_ID=STATION_ID with no surrounding spaces.")
        spot_id, station_id = value.split("=", 1)
        if re.fullmatch(r"[a-z0-9-]{1,64}", spot_id) is None:
            raise ValueError("Spot IDs must be 1 to 64 lowercase letters, digits, or hyphens.")
        if re.fullmatch(r"\d{7}", station_id) is None:
            raise ValueError("NOAA tide prediction station IDs must be exactly 7 digits.")
        if spot_id in seen_spots:
            raise ValueError(f"Spot ID is mapped more than once: {spot_id}.")
        seen_spots.add(spot_id)
        mappings.append((spot_id, station_id))
    return tuple(mappings)


def validate_horizon(hours: int) -> int:
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= MAX_HORIZON_HOURS:
        raise ValueError(f"Hours must be an integer from 1 through {MAX_HORIZON_HOURS}.")
    return hours


def request_window(now: datetime, hours: int) -> tuple[datetime, datetime]:
    validate_horizon(hours)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Request time must include a timezone.")
    start = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return start, start + timedelta(hours=hours)


def _noaa_time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("NOAA request times must include a timezone.")
    return value.astimezone(UTC).strftime("%Y%m%d %H:%M")


def build_url(station_id: str, start: datetime, end: datetime) -> str:
    params = {
        "station": station_id,
        "begin_date": _noaa_time(start),
        "end_date": _noaa_time(end),
        "product": "predictions",
        "datum": "MLLW",
        "time_zone": "gmt",
        "units": "metric",
        "interval": "h",
        "format": "json",
        "application": "icron test v.2",
    }
    return f"{NOAA_API}?{urlencode(params)}"


def _parse_noaa_time(value: object) -> datetime:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", value) is None:
        raise SourceDataError("NOAA prediction has an invalid timestamp")
    try:
        result = datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
    except ValueError as exc:
        raise SourceDataError("NOAA prediction has an invalid timestamp") from exc
    if result.minute != 0:
        raise SourceDataError("NOAA prediction timestamp is not hourly")
    return result


def parse_predictions(
    payload: bytes,
    station_id: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, str | float]]:
    try:
        response = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceDataError("NOAA returned invalid JSON") from exc
    if not isinstance(response, dict) or "error" in response:
        raise SourceDataError("NOAA returned an error response")
    metadata = response.get("metadata")
    if isinstance(metadata, dict) and metadata.get("id") is not None:
        if str(metadata["id"]) != station_id:
            raise SourceDataError("NOAA response station does not match the requested station")
    predictions = response.get("predictions")
    if not isinstance(predictions, list):
        raise SourceDataError("NOAA response is missing prediction data")
    if len(predictions) > int((end - start).total_seconds() // 3600) + 1:
        raise SourceDataError("NOAA response exceeded the requested hourly horizon")

    seen_times: set[datetime] = set()
    values: list[dict[str, str | float]] = []
    for row in predictions:
        if not isinstance(row, dict):
            raise SourceDataError("NOAA response contains an invalid prediction row")
        valid_at = _parse_noaa_time(row.get("t"))
        if valid_at < start or valid_at > end:
            raise SourceDataError("NOAA prediction is outside the requested horizon")
        if valid_at in seen_times:
            raise SourceDataError("NOAA response contains a duplicate prediction time")
        seen_times.add(valid_at)

        raw_height = row.get("v")
        if raw_height is None or raw_height == "":
            continue
        if isinstance(raw_height, bool):
            raise SourceDataError("NOAA prediction height is not numeric")
        try:
            height_m = float(raw_height)
        except (TypeError, ValueError) as exc:
            raise SourceDataError("NOAA prediction height is not numeric") from exc
        if not math.isfinite(height_m):
            continue
        values.append({"validAt": iso_utc(valid_at), "heightM": height_m})
    return sorted(values, key=lambda value: str(value["validAt"]))


def fetch_bytes(url: str) -> bytes:
    opener = build_opener(NoRedirectHandler())
    request = Request(url, headers={"User-Agent": "icron-test-v.2"})
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        if 300 <= exc.code < 400:
            raise NonRetryableError("NOAA redirects are not allowed") from exc
        raise
    if len(payload) > MAX_RESPONSE_BYTES:
        raise SourceDataError("NOAA response exceeded the download limit")
    return payload


def collect_predictions(
    spot_id: str,
    station_id: str,
    start: datetime,
    end: datetime,
) -> dict[str, object]:
    url = build_url(station_id, start, end)
    values = parse_predictions(_retry(lambda: fetch_bytes(url)), station_id, start, end)
    if not values:
        raise SourceDataError("NOAA returned no valid tide predictions")
    return {
        "spotId": spot_id,
        "source": "noaa-coops",
        "stationId": station_id,
        "datum": "MLLW",
        "intervalMinutes": 60,
        "fetchedAt": iso_utc(datetime.now(UTC)),
        "values": values,
    }


def post_predictions(payload: dict[str, object], ingest_url: str, token: str) -> None:
    origin = validate_ingest_origin(ingest_url)
    url = origin + "/ingest/tides"
    handlers = [NoRedirectHandler()]
    if origin.startswith("http://"):
        handlers.insert(0, ProxyHandler({}))
    opener = build_opener(*handlers)
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    def send() -> bytes:
        request = Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "icron-test-v.2",
            },
            method="POST",
        )
        try:
            with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                if response.status < 200 or response.status >= 300:
                    raise SourceDataError(f"ingest HTTP {response.status}")
                return response.read(4096)
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise NonRetryableError("ingest redirects are not allowed") from exc
            if exc.code < 500:
                raise NonRetryableError(f"ingest HTTP {exc.code}") from exc
            raise

    _retry(send)


def _error_message(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, URLError):
        return f"network {type(exc.reason).__name__}"
    if isinstance(exc, SourceDataError):
        return str(exc)
    return type(exc).__name__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mapping",
        action="append",
        required=True,
        metavar="SPOT_ID=STATION_ID",
        help="Explicit configured spot to NOAA CO-OPS station mapping; repeat as needed.",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=DEFAULT_HORIZON_HOURS,
        help=(
            f"Hourly prediction horizon, 1 through {MAX_HORIZON_HOURS} hours "
            f"(default: {DEFAULT_HORIZON_HOURS})."
        ),
    )
    parser.add_argument(
        "--post",
        action="store_true",
        help="POST each prediction payload to INGEST_URL/ingest/tides with INGEST_TOKEN.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        mappings = parse_mappings(args.mapping)
        validate_horizon(args.hours)
    except ValueError as exc:
        parser.error(str(exc))

    ingest_url = ""
    ingest_token = ""
    if args.post:
        ingest_url = os.environ.get("INGEST_URL", "")
        ingest_token = os.environ.get("INGEST_TOKEN", "")
        if not ingest_url or not ingest_token:
            parser.error("INGEST_URL and INGEST_TOKEN are required with --post.")
        try:
            ingest_url = validate_ingest_origin(ingest_url)
        except ValueError as exc:
            parser.error(str(exc))

    start, end = request_window(datetime.now(UTC), args.hours)
    runs: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    for spot_id, station_id in mappings:
        try:
            runs.append(collect_predictions(spot_id, station_id, start, end))
        except Exception as exc:
            errors.append(
                {
                    "spotId": spot_id,
                    "source": "noaa-coops",
                    "stationId": station_id,
                    "error": _error_message(exc),
                }
            )

    if args.post:
        for run in runs:
            try:
                post_predictions(run, ingest_url, ingest_token)
            except Exception as exc:
                errors.append(
                    {
                        "spotId": str(run["spotId"]),
                        "source": "noaa-coops",
                        "stationId": str(run["stationId"]),
                        "operation": "post",
                        "error": _error_message(exc),
                    }
                )

    print(json.dumps({"runs": runs, "errors": errors}, separators=(",", ":"), sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
