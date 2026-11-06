#!/usr/bin/env python3
"""Collect a small set of NOAA and ECMWF forecasts for the seeded surf spots."""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from functools import wraps
from ipaddress import ip_address
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

try:
    import eccodes
except ImportError:  # The collector still reports a clear per-source error.
    eccodes = None  # type: ignore[assignment]


NOMADS = "https://nomads.ncep.noaa.gov"
MAX_ATTEMPTS = 2
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
MAX_SPOT_RESPONSE_BYTES = 1024 * 1024
MAX_PUBLIC_SPOTS = 30
# Keep one batch bounded even though storage writes are per run.
MAX_POST_VALUES = 50_000
NOMADS_MIN_INTERVAL = 0.51
ECMWF_HTTP_TIMEOUT = (15, 90)
BOX_DEGREES = 0.5
MAX_STEP_HOURS = 144
MAX_STEPS = 49
DEFAULT_STEPS = tuple(range(0, 91, 3)) + tuple(range(96, 145, 6))
SPOT_ID_PATTERN = re.compile(r"[a-z0-9-]{1,64}\Z")
SUPPORTED_REGIONS = frozenset(("bali", "california", "korea", "hawaii", "japan", "australia", "new-zealand", "tahiti"))
MODEL_NAMES = {
    "noaa-wave": "gfswave.global.0p25",
    "noaa-wind": "gfs.0p25",
    "noaa-sst": "rtgssthr.0p083",
    "ecmwf-wave": "ifs.wave.0p25",
    "ecmwf-wave-0p1": "ifs.wave.0p1",
    "ecmwf-wind-0p1": "ifs.0p1",
}

# Approximate development fixtures; scheduled collection always loads configured public points.
SPOTS = (
    {"id": "bali-kuta-beach", "lat": -8.7185, "lon": 115.1686},
    {"id": "bali-batu-bolong", "lat": -8.6595, "lon": 115.1300},
    {"id": "ca-linda-mar", "lat": 37.5946, "lon": -122.5025},
    {"id": "ca-doheny", "lat": 33.4610, "lon": -117.6870},
)
_nomads_request_lock = threading.Lock()
_nomads_next_start = 0.0
_request_counts = {"noaa": 0, "ecmwf": 0}
_request_count_lock = threading.Lock()


def _count_request(source: str) -> None:
    with _request_count_lock:
        _request_counts[source] += 1


class SourceDataError(Exception):
    """A source response could not be safely normalized."""


class NonRetryableError(SourceDataError):
    """A request was rejected permanently and should not be repeated."""


class CycleUnavailableError(SourceDataError):
    """No recent candidate has all required forecast indexes yet."""


class NoRedirectHandler(HTTPRedirectHandler):
    """Reject redirects to preserve origin boundaries and per-request accounting."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urlopen(request: Request, timeout: float):
    # A provider attempt is exactly one HTTP transaction, including failed redirects.
    return build_opener(NoRedirectHandler()).open(request, timeout=timeout)


@dataclass(frozen=True)
class GridCandidate:
    lat: float
    lon: float
    value: float
    distance_km: float


@dataclass(frozen=True)
class GridSample:
    short_name: str
    level_type: str
    level: int
    units: str
    issued_at: str
    valid_at: str
    missing_value: float
    candidates: tuple[GridCandidate, ...]
    start_step: float | None = None
    end_step: float | None = None
    step_type: str | None = None
    step_units: str | None = None


@dataclass(frozen=True)
class FieldSpec:
    short_name: str
    variable: str
    units: str
    level_type: str
    level: int


NOAA_WAVE_FIELDS = (
    FieldSpec("swh", "wave_height_m", "m", "surface", 1),
    FieldSpec("perpw", "wave_period_s", "s", "surface", 1),
    # The scalar GFS-Wave GRIB fields are already expressed as directions waves
    # come from. Do not apply the spectral/oceanographic 180-degree rotation.
    FieldSpec("dirpw", "wave_direction_deg", "degree true", "surface", 1),
    FieldSpec("shww", "wind_wave_height_m", "m", "surface", 1),
    FieldSpec("mpww", "wind_wave_period_s", "s", "surface", 1),
    FieldSpec("wvdir", "wind_wave_direction_deg", "degree true", "surface", 1),
    FieldSpec("shts", "swell_height_m", "m", "orderedSequenceData", 1),
    FieldSpec("mpts", "swell_period_s", "s", "orderedSequenceData", 1),
    FieldSpec("swdir", "swell_direction_deg", "degree true", "orderedSequenceData", 1),
    FieldSpec("shts", "swell2_height_m", "m", "orderedSequenceData", 2),
    FieldSpec("mpts", "swell2_period_s", "s", "orderedSequenceData", 2),
    FieldSpec("swdir", "swell2_direction_deg", "degree true", "orderedSequenceData", 2),
)
ECMWF_WAVE_FIELDS = (
    FieldSpec("swh", "wave_height_m", "m", "surface", 0),
    FieldSpec("mwp", "wave_period_s", "s", "surface", 0),
    FieldSpec("pp1d", "peak_period_s", "s", "surface", 0),
    # ECMWF scalar MWD is already meteorological: 0=north/from, 90=east/from.
    FieldSpec("mwd", "wave_direction_deg", "degree true", "surface", 0),
)
WIND_FIELDS = (
    FieldSpec("10u", "", "m s**-1", "heightAboveGround", 10),
    FieldSpec("10v", "", "m s**-1", "heightAboveGround", 10),
    FieldSpec("gust", "wind_gust_ms", "m s**-1", "surface", 0),
    FieldSpec("2t", "air_temperature_c", "K", "heightAboveGround", 2),
    FieldSpec("tp", "precipitation_mm", "kg m**-2", "surface", 0),
)
SST_FIELD = FieldSpec("t", "sea_surface_temp_c", "K", "surface", 0)


def parse_cycle(value: str) -> datetime:
    if re.fullmatch(r"\d{10}", value) is None:
        raise ValueError("Model cycle must be a real UTC date in YYYYMMDDHH format.")
    try:
        cycle = datetime.strptime(value, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError("Model cycle must be a real UTC date in YYYYMMDDHH format.") from exc
    if cycle.hour not in (0, 6, 12, 18):
        raise ValueError("Model cycle hour must be one of 00, 06, 12, or 18 UTC.")
    return cycle


def parse_sst_date(value: str) -> datetime:
    if re.fullmatch(r"\d{8}", value) is None:
        raise ValueError("SST date must be a real UTC date in YYYYMMDD format.")
    try:
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError("SST date must be a real UTC date in YYYYMMDD format.") from exc


def parse_steps(value: str) -> tuple[int, ...]:
    try:
        steps = tuple(int(part.strip()) for part in value.split(","))
    except ValueError as exc:
        raise ValueError(f"Steps must be comma-separated integers from 0 through {MAX_STEP_HOURS}.") from exc
    if (
        not steps
        or len(steps) > MAX_STEPS
        or steps != tuple(sorted(set(steps)))
        or any(step < 0 or step > MAX_STEP_HOURS or step % 3 for step in steps)
    ):
        raise ValueError(
            f"Steps must be unique, increasing 3-hour steps from 0 through {MAX_STEP_HOURS} "
            f"(max {MAX_STEPS})."
        )
    return steps


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def expected_valid_at(cycle: datetime, step: int) -> str:
    return iso_utc(cycle + timedelta(hours=step))


def kelvin_to_celsius(value: float) -> float:
    return value - 273.15


def wind_from_uv(u: float, v: float) -> tuple[float, float | None]:
    speed = math.hypot(u, v)
    if speed == 0:
        return speed, None
    direction = math.degrees(math.atan2(-u, -v)) % 360.0
    return speed, direction


def normalize_longitude(value: float) -> float:
    normalized = (value + 180.0) % 360.0 - 180.0
    return 0.0 if normalized == -0.0 else normalized


def grid_key(lat: float, lon: float) -> tuple[float, float]:
    return round(lat, 4), round(normalize_longitude(lon), 4)


def is_valid(candidate: GridCandidate, missing_value: float) -> bool:
    return math.isfinite(candidate.value) and candidate.value != missing_value


def nearest_valid_candidate(sample: GridSample) -> GridCandidate | None:
    valid = [candidate for candidate in sample.candidates if is_valid(candidate, sample.missing_value)]
    return min(valid, key=lambda candidate: candidate.distance_km) if valid else None


def _candidate_at(sample: GridSample, key: tuple[float, float]) -> GridCandidate | None:
    return next((candidate for candidate in sample.candidates if grid_key(candidate.lat, candidate.lon) == key), None)


def select_grid_values(
    fields: dict[str, GridSample], anchor: str
) -> tuple[dict[str, float] | None, tuple[float, float] | None]:
    """Choose the nearest valid anchor cell, then sample other fields at that same cell."""
    anchor_sample = fields.get(anchor)
    if not anchor_sample:
        return None, None
    candidate = nearest_valid_candidate(anchor_sample)
    if candidate is None:
        return None, None
    key = grid_key(candidate.lat, candidate.lon)
    values: dict[str, float] = {}
    for short_name, sample in fields.items():
        match = _candidate_at(sample, key)
        if match is not None and is_valid(match, sample.missing_value):
            values[short_name] = match.value
    return values, (candidate.lat, normalize_longitude(candidate.lon))


def require_stable_grid(
    previous: dict[str, float] | None, current: dict[str, float] | None
) -> dict[str, float] | None:
    """Keep one persisted run tied to one actual model grid cell."""
    if current is None:
        return previous
    if previous and grid_key(previous["lat"], previous["lon"]) != grid_key(current["lat"], current["lon"]):
        raise SourceDataError("nearest valid grid changed within run; step omitted")
    return previous or current


def _normalize_unit(value: str) -> str:
    return re.sub(r"[^a-z0-9*]+", "", value.lower())


UNIT_ALIASES = {
    "m": {"m"},
    "s": {"s"},
    "K": {"k"},
    "degree true": {"degreetrue", "degreestrue", "degtrue", "degree"},
    "m s**-1": {"ms**-1", "ms-1", "m/s"},
    "kg m**-2": {"kg m**-2", "kg m-2", "kg/m2"},
}


def _check_units(sample: GridSample, spec: FieldSpec) -> None:
    actual = _normalize_unit(sample.units)
    expected = {_normalize_unit(unit) for unit in UNIT_ALIASES[spec.units]}
    if actual not in expected:
        raise SourceDataError(f"unexpected units for {sample.short_name}")


def _step_hours(value: float | None, units: str | None) -> float | None:
    factors = {"0": 1 / 60, "1": 1, "2": 24, "10": 3, "11": 6, "12": 12, "13": 1 / 3600,
               "m": 1 / 60, "h": 1, "D": 24, "s": 1 / 3600}
    if value is None or units not in factors:
        return None
    return value * factors[units]


def _samples_for_specs(
    records: list[GridSample],
    specs: tuple[FieldSpec, ...],
    issue_at: str,
    valid_at: str,
) -> dict[str, GridSample]:
    matched_time = [record for record in records if record.issued_at == issue_at and record.valid_at == valid_at]
    if not matched_time:
        raise SourceDataError("GRIB issue or valid time does not match the requested cycle and step")
    fields: dict[str, GridSample] = {}
    for spec in specs:
        matches = [record for record in matched_time if record.short_name == spec.short_name
                   and record.level_type == spec.level_type and record.level == spec.level]
        if spec.variable == "precipitation_mm":
            lead = (datetime.fromisoformat(valid_at.replace("Z", "+00:00")) -
                    datetime.fromisoformat(issue_at.replace("Z", "+00:00"))).total_seconds() / 3600
            matches = [record for record in matches if record.step_type == "accum"
                       and _step_hours(record.start_step, record.step_units) == 0
                       and _step_hours(record.end_step, record.step_units) == lead and lead > 0]
        if not matches:
            continue
        sample = matches[0]
        if spec.variable == "precipitation_mm" and any(record != sample for record in matches[1:]):
            raise SourceDataError("inconsistent duplicate cumulative precipitation records")
        _check_units(sample, spec)
        fields[spec.variable or spec.short_name] = sample
    if not fields:
        raise SourceDataError("requested GRIB parameters are absent")
    return fields


def normalize_wave_step(
    records: list[GridSample],
    spot: dict[str, float | str],
    issue_at: str,
    valid_at: str,
    *,
    ecmwf: bool,
) -> tuple[dict[str, float] | None, list[dict[str, str | float]]]:
    specs = ECMWF_WAVE_FIELDS if ecmwf else NOAA_WAVE_FIELDS
    fields = _samples_for_specs(records, specs, issue_at, valid_at)
    anchor = "wave_height_m" if "wave_height_m" in fields else next(iter(fields))
    raw_values, grid = select_grid_values(fields, anchor)
    if raw_values is None or grid is None:
        return None, []
    values: list[dict[str, str | float]] = []
    for spec in specs:
        if spec.variable not in raw_values:
            continue
        value = raw_values[spec.variable]
        values.append({"validAt": valid_at, "variable": spec.variable, "value": value})
    return {"lat": grid[0], "lon": grid[1]}, values


def normalize_wind_step(
    records: list[GridSample], spot: dict[str, float | str], issue_at: str, valid_at: str
) -> tuple[dict[str, float] | None, list[dict[str, str | float]]]:
    fields = _samples_for_specs(records, WIND_FIELDS, issue_at, valid_at)
    raw_values, grid = select_grid_values(fields, "10u")
    if raw_values is None or grid is None or "10u" not in raw_values or "10v" not in raw_values:
        return None, []
    speed, direction = wind_from_uv(raw_values["10u"], raw_values["10v"])
    values: list[dict[str, str | float]] = [
        {"validAt": valid_at, "variable": "wind_speed_ms", "value": speed}
    ]
    if direction is not None:
        values.append({"validAt": valid_at, "variable": "wind_direction_deg", "value": direction})
    for variable in ("wind_gust_ms", "air_temperature_c"):
        if variable in raw_values:
            value = raw_values[variable]
            if variable == "air_temperature_c":
                value = kelvin_to_celsius(value)
            values.append({"validAt": valid_at, "variable": variable, "value": value})
    return {"lat": grid[0], "lon": grid[1]}, values


def normalize_sst_sample(
    records: list[GridSample], spot: dict[str, float | str], analysis_at: str
) -> tuple[dict[str, float] | None, list[dict[str, str | float]]]:
    fields = _samples_for_specs(records, (SST_FIELD,), analysis_at, analysis_at)
    raw_values, grid = select_grid_values(fields, "sea_surface_temp_c")
    if raw_values is None or grid is None or "sea_surface_temp_c" not in raw_values:
        return None, []
    return (
        {"lat": grid[0], "lon": grid[1]},
        [{"validAt": analysis_at, "variable": "sea_surface_temp_c", "value": kelvin_to_celsius(raw_values["sea_surface_temp_c"])}],
    )


def _grib_time(gid: int, date_key: str, time_key: str) -> str:
    date_value = int(eccodes.codes_get(gid, date_key))
    time_value = int(eccodes.codes_get(gid, time_key))
    return iso_utc(datetime.strptime(f"{date_value:08d}{time_value:04d}", "%Y%m%d%H%M").replace(tzinfo=UTC))


def decode_grib(payload: bytes, spots: tuple[dict[str, float | str], ...]) -> dict[str, list[GridSample]]:
    if eccodes is None:
        raise SourceDataError("ecCodes Python bindings are unavailable")
    if not payload.startswith(b"GRIB"):
        raise SourceDataError("source returned a non-GRIB response")
    decoded = {str(spot["id"]): [] for spot in spots}
    count = 0
    with tempfile.TemporaryFile() as stream:
        stream.write(payload)
        stream.seek(0)
        while (gid := eccodes.codes_grib_new_from_file(stream)) is not None:
            try:
                count += 1
                short_name = str(eccodes.codes_get(gid, "shortName"))
                level_type = str(eccodes.codes_get(gid, "typeOfLevel"))
                level = int(eccodes.codes_get(gid, "level"))
                units = str(eccodes.codes_get(gid, "units"))
                issued_at = _grib_time(gid, "dataDate", "dataTime")
                valid_at = _grib_time(gid, "validityDate", "validityTime")
                missing_value = float(eccodes.codes_get(gid, "missingValue"))
                start_step = float(eccodes.codes_get(gid, "startStep"))
                end_step = float(eccodes.codes_get(gid, "endStep"))
                step_type = str(eccodes.codes_get(gid, "stepType"))
                step_units = str(eccodes.codes_get(gid, "stepUnits"))
                for spot in spots:
                    nearest = eccodes.codes_grib_find_nearest(
                        gid, float(spot["lat"]), float(spot["lon"]), 0, 4
                    )
                    candidates = tuple(
                        GridCandidate(
                            float(point["lat"]),
                            float(point["lon"]),
                            float(point["value"]),
                            float(point["distance"]),
                        )
                        for point in nearest
                    )
                    decoded[str(spot["id"])].append(
                        GridSample(
                            short_name,
                            level_type,
                            level,
                            units,
                            issued_at,
                            valid_at,
                            missing_value,
                            candidates,
                            start_step, end_step, step_type, step_units,
                        )
                    )
            finally:
                eccodes.codes_release(gid)
    if not count:
        raise SourceDataError("source returned an empty GRIB response")
    return decoded


def _retry(operation: Callable[[], bytes]) -> bytes:
    last_error: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            return operation()
        except NonRetryableError:
            raise
        except HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code != 429:
                raise
            last_error = exc
            if attempt + 1 < MAX_ATTEMPTS:
                time.sleep(1)
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if isinstance(status, int) and 400 <= status < 500 and status != 429:
                raise
            last_error = exc
            if attempt + 1 < MAX_ATTEMPTS:
                time.sleep(1)
    if last_error is not None:
        raise last_error
    raise SourceDataError("source request failed")


def _blocks_source_after_retry(exc: Exception) -> bool:
    """Stop repeating a source across leads after a permanent or exhausted error."""
    if isinstance(exc, HTTPError):
        return exc.code >= 400
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status >= 400
    return isinstance(exc, (URLError, TimeoutError, ConnectionError, OSError)) or type(exc).__name__ in {
        "ChunkedEncodingError",
        "ConnectTimeout",
        "ReadTimeout",
    }


def _download(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "icron-test-v.2"})
    global _nomads_next_start
    if urlsplit(url).hostname == urlsplit(NOMADS).hostname:
        with _nomads_request_lock:
            delay = _nomads_next_start - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            _nomads_next_start = time.monotonic() + NOMADS_MIN_INTERVAL
            _count_request("noaa")
    # Pace starts, but let the two bounded workers wait for responses independently.
    response = urlopen(request, timeout=60)
    with response:
        payload = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(payload) > MAX_DOWNLOAD_BYTES:
        raise SourceDataError("source response exceeded the download limit")
    if not payload.startswith(b"GRIB"):
        raise SourceDataError("source returned a non-GRIB response")
    return payload


def cluster_spots(spots: tuple[dict[str, float | str], ...], max_span_deg: float = 2.0) -> list[tuple[dict[str, float | str], ...]]:
    clusters: list[list[dict[str, float | str]]] = []
    for spot in sorted(spots, key=lambda s: (str(s.get("region", "")), float(s["lat"]), float(s["lon"]), str(s["id"]))):
        for cluster in clusters:
            members = cluster + [spot]
            if (spot.get("region") == cluster[0].get("region")
                and max(float(s["lat"]) for s in members) - min(float(s["lat"]) for s in members) <= max_span_deg
                and max(float(s["lon"]) for s in members) - min(float(s["lon"]) for s in members) <= max_span_deg):
                cluster.append(spot)
                break
        else:
            clusters.append([spot])
    return [tuple(cluster) for cluster in clusters]


def _small_box(spots: tuple[dict[str, float | str], ...]) -> dict[str, str]:
    return {
        "leftlon": f"{math.floor((min(float(s['lon']) for s in spots) - BOX_DEGREES) * 10000) / 10000:.4f}",
        "rightlon": f"{math.ceil((max(float(s['lon']) for s in spots) + BOX_DEGREES) * 10000) / 10000:.4f}",
        "toplat": f"{min(90.0, math.ceil((max(float(s['lat']) for s in spots) + BOX_DEGREES) * 10000) / 10000):.4f}",
        "bottomlat": f"{max(-90.0, math.floor((min(float(s['lat']) for s in spots) - BOX_DEGREES) * 10000) / 10000):.4f}",
        "subregion": "",
    }


def _noaa_url(kind: str, cycle: datetime, step: int, spots: tuple[dict[str, float | str], ...]) -> str:
    date = cycle.strftime("%Y%m%d")
    hour = cycle.strftime("%H")
    forecast_hour = f"{step:03d}"
    if kind == "wave":
        endpoint = f"{NOMADS}/cgi-bin/filter_gfswave.pl"
        params = {
            "file": f"gfswave.t{hour}z.global.0p25.f{forecast_hour}.grib2",
            "var_HTSGW": "on",
            "var_PERPW": "on",
            "var_DIRPW": "on",
            "var_SWELL": "on",
            "var_SWPER": "on",
            "var_SWDIR": "on",
            "var_WVHGT": "on",
            "var_WVPER": "on",
            "var_WVDIR": "on",
            "lev_surface": "on",
            "lev_1_in_sequence": "on",
            "lev_2_in_sequence": "on",
            "dir": f"/gfs.{date}/{hour}/wave/gridded",
        }
    else:
        endpoint = f"{NOMADS}/cgi-bin/filter_gfs_0p25.pl"
        params = {
            "file": f"gfs.t{hour}z.pgrb2.0p25.f{forecast_hour}",
            "var_UGRD": "on",
            "var_VGRD": "on",
            "var_GUST": "on",
            "var_TMP": "on",
            "var_APCP": "on",
            "lev_surface": "on",
            "lev_2_m_above_ground": "on",
            "lev_10_m_above_ground": "on",
            "dir": f"/gfs.{date}/{hour}/atmos",
        }
    params.update(_small_box(spots))
    return f"{endpoint}?{urlencode(params)}"


def _run(
    spot_id: str,
    source: str,
    model: str,
    issued_at: str,
    fetched_at: str,
    grid: dict[str, float] | None,
    values: list[dict[str, str | float]],
) -> dict[str, object] | None:
    if not values:
        return None
    return {
        "spotId": spot_id,
        "source": source,
        "model": model,
        "issuedAt": issued_at,
        "fetchedAt": fetched_at,
        "grid": grid,
        "values": sorted(values, key=lambda value: (str(value["validAt"]), str(value["variable"]))),
    }


def _source_error(spot_id: str, source: str, model: str, step: int | None, exc: Exception, *, operation: str = "collect") -> dict[str, object]:
    http_error = exc.__cause__ if isinstance(exc, NonRetryableError) and isinstance(exc.__cause__, HTTPError) else exc
    status = http_error.code if isinstance(http_error, HTTPError) else getattr(getattr(http_error, "response", None), "status_code", None)
    if isinstance(exc, CycleUnavailableError):
        code = "cycle_unavailable"
    elif type(status) is int and 300 <= status <= 599:
        code = "http_error"
    elif isinstance(exc, (URLError, TimeoutError, ConnectionError)):
        code = "transport_error"
    elif isinstance(exc, SourceDataError):
        code = "invalid_data"
    else:
        code = "unexpected_error"
    if isinstance(exc, HTTPError):
        detail = f"HTTP {exc.code}"
    elif isinstance(exc, URLError):
        detail = f"network {type(exc.reason).__name__}"
    elif isinstance(getattr(getattr(exc, "response", None), "status_code", None), int):
        detail = f"HTTP {exc.response.status_code}"  # type: ignore[attr-defined]
    elif isinstance(exc, SourceDataError):
        detail = str(exc)
    else:
        detail = type(exc).__name__
    result: dict[str, object] = {"spotId": spot_id, "source": source, "model": model, "error": detail,
                                "operation": operation, "code": code}
    if code == "http_error":
        result["httpStatus"] = status
    if step is not None:
        result["step"] = step
    return result


def collect_noaa_cluster(
    spots: tuple[dict[str, float | str], ...], cycle: datetime, steps: tuple[int, ...]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    issue_at = iso_utc(cycle)
    states = {str(spot["id"]): {"wave_values": [], "wind_values": [], "wave_grid": None,
                                  "wind_grid": None, "previous_rain": None} for spot in spots}
    blocked: set[str] = set()
    errors: list[dict[str, object]] = []
    previous_step = None
    for step in steps:
        valid_at = expected_valid_at(cycle, step)
        for kind in ("wave", "wind"):
            if kind in blocked:
                continue
            model = MODEL_NAMES["noaa-" + kind]
            try:
                payload = _retry(lambda: _download(_noaa_url(kind, cycle, step, spots)))
                decoded = decode_grib(payload, spots)
            except Exception as exc:
                errors.extend(_source_error(str(spot["id"]), "noaa", model, step, exc) for spot in spots)
                if _blocks_source_after_retry(exc):
                    blocked.add(kind)
                if kind == "wind":
                    for state in states.values():
                        state["previous_rain"] = None
                continue
            for spot in spots:
                spot_id = str(spot["id"])
                state = states[spot_id]
                prior_rain = state["previous_rain"]
                if kind == "wind":
                    state["previous_rain"] = None
                try:
                    records = decoded[spot_id]
                    if kind == "wave":
                        grid, values = normalize_wave_step(records, spot, issue_at, valid_at, ecmwf=False)
                        state["wave_grid"] = require_stable_grid(state["wave_grid"], grid)
                    else:
                        grid, values = normalize_wind_step(records, spot, issue_at, valid_at)
                        state["wind_grid"] = require_stable_grid(state["wind_grid"], grid)
                        fields = _samples_for_specs(records, WIND_FIELDS, issue_at, valid_at) if records else {}
                        rain = fields.get("precipitation_mm")
                        if rain is not None and grid is not None:
                            key = grid_key(grid["lat"], grid["lon"])
                            candidate = _candidate_at(rain, key)
                            if candidate is not None and is_valid(candidate, rain.missing_value) and candidate.value >= 0:
                                state["previous_rain"] = (step, candidate.value, key)
                                amount = None
                                interval = None
                                if step == 3 and previous_step in (None, 0):
                                    amount, interval = candidate.value, 3
                                elif prior_rain is not None and prior_rain[0] == previous_step and prior_rain[2] == key:
                                    amount = candidate.value - prior_rain[1]
                                    interval = step - prior_rain[0]
                                if amount is not None and amount >= 0 and interval is not None and interval > 0:
                                    values.extend([
                                        {"validAt": valid_at, "variable": "precipitation_mm", "value": amount},
                                        {"validAt": valid_at, "variable": "precipitation_interval_hours", "value": interval},
                                    ])
                    state[kind + "_values"].extend(values)
                except Exception as exc:
                    errors.append(_source_error(spot_id, "noaa", model, step, exc))
        previous_step = step
    fetched_at = iso_utc(datetime.now(UTC))
    runs = []
    for spot in spots:
        spot_id = str(spot["id"])
        state = states[spot_id]
        for kind in ("wave", "wind"):
            model = MODEL_NAMES["noaa-" + kind]
            run = _run(spot_id, "noaa", model, issue_at, fetched_at, state[kind + "_grid"], state[kind + "_values"])
            if run:
                runs.append(run)
            elif not any(error["spotId"] == spot_id and error["model"] == model for error in errors):
                errors.append(_source_error(spot_id, "noaa", model, None, SourceDataError("no valid grid values")))
    return runs, errors


def collect_noaa_spot(spot: dict[str, float | str], cycle: datetime, steps: tuple[int, ...]):
    return collect_noaa_cluster((spot,), cycle, steps)


def _ecmwf_payload(client: object, cycle: datetime, step: int, kind: str = "wave") -> bytes:
    def retrieve() -> bytes:
        with tempfile.TemporaryDirectory(prefix="icron-ecmwf-") as directory:
            target = str(Path(directory) / "wave.grib2")
            with redirect_stdout(sys.stderr):
                client.retrieve(  # type: ignore[attr-defined]
                    date=cycle.strftime("%Y%m%d"),
                    time=cycle.hour,
                    stream="wave" if kind == "wave" else "oper",
                    type="fc",
                    step=step,
                    levtype="sfc",
                    param=["swh", "mwd", "mwp", "pp1d"] if kind == "wave" else ["10u", "10v", "2t"],
                    target=target,
                )
            with Path(target).open("rb") as data:
                payload = data.read(MAX_DOWNLOAD_BYTES + 1)
        if len(payload) > MAX_DOWNLOAD_BYTES:
            raise SourceDataError("ECMWF GRIB exceeds the download size limit")
        if not payload.startswith(b"GRIB"):
            raise SourceDataError("ECMWF Open Data returned a non-GRIB response")
        return payload

    return _retry(retrieve)


def _set_ecmwf_timeouts(client: object) -> None:
    """Apply finite HTTP timeouts to the library's requests session."""
    session = client.session  # type: ignore[attr-defined]
    if hasattr(session, "send"):
        original_send = session.send

        @wraps(original_send)
        def counted_send(*args: object, **kwargs: object):
            _count_request("ecmwf")
            return original_send(*args, **kwargs)

        session.send = counted_send
    for method_name in ("get", "head"):
        original = getattr(session, method_name)

        @wraps(original)
        def bounded(*args: object, _original=original, **kwargs: object):
            kwargs["timeout"] = ECMWF_HTTP_TIMEOUT
            return _original(*args, **kwargs)

        setattr(session, method_name, bounded)


def _set_ecmwf_0p1_paths(client: object) -> None:
    """Adapt 0.3.34's data URLs to the 0.1-degree surface file naming."""
    original = client.get_parts  # type: ignore[attr-defined]

    @wraps(original)
    def surface_parts(urls: list[str], for_index: object):
        corrected = []
        for url in urls:
            if not url.endswith("-fc.grib2"):
                raise SourceDataError("unexpected ECMWF 0.1-degree data path")
            corrected.append(url[:-len(".grib2")] + "-sfc.grib2")
        # Single-range entries keep multiurl on the bounded, counted client session.
        # Its non-multipart fallback otherwise sends later ranges via requests.get.
        groups = original(corrected, for_index)
        return [(url, (part,)) for url, parts in groups for part in parts]

    client.get_parts = surface_parts  # type: ignore[attr-defined]


def collect_ecmwf(
    spots: tuple[dict[str, float | str], ...], cycle: datetime, steps: tuple[int, ...],
    resolution: str = "0p25",
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    products = (("wave", MODEL_NAMES["ecmwf-wave"]),) if resolution == "0p25" else (
        ("wave", MODEL_NAMES["ecmwf-wave-0p1"]), ("wind", MODEL_NAMES["ecmwf-wind-0p1"]),
    )
    try:
        from ecmwf.opendata import Client

        with redirect_stdout(sys.stderr):
            client = Client(
                source="google" if resolution == "0p1" else "ecmwf", model="ifs", resol=resolution, maximum_retries=0,
                retry_after=1, use_server_retry_after=False,
            )
        _set_ecmwf_timeouts(client)
        if resolution == "0p1":
            _set_ecmwf_0p1_paths(client)
    except Exception as exc:
        return [], [
            _source_error(str(spot["id"]), "ecmwf", model, None, exc)
            for spot in spots for _, model in products
        ]
    issue_at = iso_utc(cycle)
    errors: list[dict[str, object]] = []
    runs: list[dict[str, object]] = []
    for kind, model in products:
        values_by_spot = {str(spot["id"]): [] for spot in spots}
        grid_by_spot: dict[str, dict[str, float] | None] = {str(spot["id"]): None for spot in spots}
        for step in steps:
            valid_at = expected_valid_at(cycle, step)
            try:
                payload = _ecmwf_payload(client, cycle, step, kind)
            except Exception as exc:
                errors.extend(_source_error(str(spot["id"]), "ecmwf", model, step, exc) for spot in spots)
                if _blocks_source_after_retry(exc):
                    break
                continue
            try:
                decoded = decode_grib(payload, spots)
            except Exception as exc:
                errors.extend(_source_error(str(spot["id"]), "ecmwf", model, step, exc) for spot in spots)
                continue
            for spot in spots:
                spot_id = str(spot["id"])
                try:
                    if kind == "wave":
                        grid, values = normalize_wave_step(decoded[spot_id], spot, issue_at, valid_at, ecmwf=True)
                    else:
                        grid, values = normalize_wind_step(decoded[spot_id], spot, issue_at, valid_at)
                    grid_by_spot[spot_id] = require_stable_grid(grid_by_spot[spot_id], grid)
                    values_by_spot[spot_id].extend(values)
                except Exception as exc:
                    errors.append(_source_error(spot_id, "ecmwf", model, step, exc))
        fetched_at = iso_utc(datetime.now(UTC))
        for spot in spots:
            spot_id = str(spot["id"])
            run = _run(spot_id, "ecmwf", model, issue_at, fetched_at, grid_by_spot[spot_id], values_by_spot[spot_id])
            if run:
                runs.append(run)
            elif not any(error["spotId"] == spot_id and error["model"] == model for error in errors):
                errors.append(_source_error(spot_id, "ecmwf", model, None, SourceDataError("no valid grid values")))
    return runs, errors


def _sst_url(sst_date: datetime) -> str:
    date = sst_date.strftime("%Y%m%d")
    return (
        f"{NOMADS}/pub/data/nccf/com/nsst/prod/nsst.{date}/"
        "rtgssthr_grb_0.083.grib2"
    )


def validate_ingest_origin(value: str) -> str:
    """Validate and normalize a bare HTTPS origin, or loopback HTTP origin."""
    if not value or value != value.strip() or any(
        char.isspace() or unicodedata.category(char).startswith("C") for char in value
    ):
        raise ValueError("INGEST_URL must be a valid HTTPS origin (or loopback HTTP origin).")
    if any(marker in value for marker in ("?", "#", "\\")):
        raise ValueError("INGEST_URL must not contain a query, fragment, or backslash.")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("INGEST_URL has an invalid host or port.") from exc
    if parsed.scheme not in ("https", "http") or not parsed.netloc or not host:
        raise ValueError("INGEST_URL must be a valid HTTPS origin (or loopback HTTP origin).")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("INGEST_URL must not include username or password credentials.")
    if parsed.path not in ("", "/"):
        raise ValueError("INGEST_URL must be an origin without an API path.")
    if parsed.netloc.endswith(":") or (port is not None and not 1 <= port <= 65535):
        raise ValueError("INGEST_URL has an invalid port.")
    if "%" in host or not _valid_hostname(host):
        raise ValueError("INGEST_URL has an invalid host.")
    if parsed.scheme == "http" and not _is_loopback_host(host):
        raise ValueError("HTTP is allowed only for localhost or a loopback IP address.")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def parse_public_spots(payload: bytes) -> tuple[dict[str, float | str], ...]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NonRetryableError("spot list returned invalid JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("spots"), list):
        raise NonRetryableError("spot list returned an invalid response")

    spots: list[dict[str, float | str]] = []
    seen_ids: set[str] = set()
    for record in document["spots"]:
        if not isinstance(record, dict):
            raise NonRetryableError("spot list contained an invalid record")
        spot_id = record.get("id")
        region = record.get("region")
        lat = record.get("lat")
        lon = record.get("lon")
        if not isinstance(spot_id, str) or SPOT_ID_PATTERN.fullmatch(spot_id) is None:
            raise NonRetryableError("public spot has an invalid id")
        if spot_id in seen_ids:
            raise NonRetryableError("spot list contains duplicate ids")
        if not isinstance(region, str) or region not in SUPPORTED_REGIONS:
            raise NonRetryableError("public spot has an unsupported region")
        if (
            isinstance(lat, bool)
            or isinstance(lon, bool)
            or not isinstance(lat, (int, float))
            or not isinstance(lon, (int, float))
            or not -90 <= lat <= 90
            or not -180 <= lon <= 180
            or not math.isfinite(lat)
            or not math.isfinite(lon)
        ):
            raise NonRetryableError("public spot has invalid coordinates")
        seen_ids.add(spot_id)
        spots.append({"id": spot_id, "region": region, "lat": lat, "lon": lon})
        if len(spots) > MAX_PUBLIC_SPOTS:
            raise NonRetryableError(f"spot list exceeds the {MAX_PUBLIC_SPOTS}-spot limit")
    if not spots:
        raise NonRetryableError("spot list contains no public spots")
    return tuple(spots)


def fetch_public_spots(ingest_url: str) -> tuple[dict[str, float | str], ...]:
    origin = validate_ingest_origin(ingest_url)
    url = origin + "/spots"
    handlers = [NoRedirectHandler()]
    if origin.startswith("http://"):
        handlers.insert(0, ProxyHandler({}))
    opener = build_opener(*handlers)

    def receive() -> bytes:
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "icron-test-v.2",
            },
            method="GET",
        )
        try:
            response = opener.open(request, timeout=30)
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise NonRetryableError("spots redirects are not allowed") from exc
            raise
        with response:
            if 300 <= response.status < 400:
                raise NonRetryableError("spots redirects are not allowed")
            if response.status < 200 or response.status >= 300:
                raise NonRetryableError(f"spots HTTP {response.status}")
            payload = response.read(MAX_SPOT_RESPONSE_BYTES + 1)
        if len(payload) > MAX_SPOT_RESPONSE_BYTES:
            raise NonRetryableError("spot list exceeded the 1 MiB response limit")
        return payload

    return parse_public_spots(_retry(receive))


def _valid_hostname(host: str) -> bool:
    try:
        ip_address(host)
        return True
    except ValueError:
        if ":" in host or re.fullmatch(r"\d+(?:\.\d+){3}", host):
            return False
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if len(ascii_host) > 253:
        return False
    if ascii_host.endswith(".."):
        return False
    labels = (ascii_host[:-1] if ascii_host.endswith(".") else ascii_host).split(".")
    return all(
        0 < len(label) <= 63
        and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label) is not None
        for label in labels
    )


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def collect_sst(
    spots: tuple[dict[str, float | str], ...], sst_date: datetime
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    model = MODEL_NAMES["noaa-sst"]
    expected_at = iso_utc(sst_date)
    try:
        payload = _retry(lambda: _download(_sst_url(sst_date)))
        decoded = decode_grib(payload, spots)
    except Exception as exc:
        return [], [_source_error(str(spot["id"]), "noaa", model, None, exc) for spot in spots]
    fetched_at = iso_utc(datetime.now(UTC))
    runs: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []
    for spot in spots:
        spot_id = str(spot["id"])
        try:
            grid, values = normalize_sst_sample(decoded[spot_id], spot, expected_at)
            run = _run(spot_id, "noaa", model, expected_at, fetched_at, grid, values)
            if run:
                runs.append(run)
            else:
                errors.append(_source_error(spot_id, "noaa", model, None, SourceDataError("no valid grid values")))
        except Exception as exc:
            errors.append(_source_error(spot_id, "noaa", model, None, exc))
    return runs, errors


def post_run(run: dict[str, object], ingest_url: str, token: str) -> None:
    body = json.dumps(run, separators=(",", ":")).encode("utf-8")
    origin = validate_ingest_origin(ingest_url)
    url = origin + "/ingest/forecast"
    handlers = [NoRedirectHandler()]
    if origin.startswith("http://"):
        # Local HTTP development endpoints must not be proxied with a bearer token.
        handlers.insert(0, ProxyHandler({}))
    opener = build_opener(*handlers)

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
            with opener.open(request, timeout=30) as response:
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


def _index_urls(source: str, cycle: datetime, step: int, resolution: str = "0p25") -> tuple[str, ...]:
    date, hour = cycle.strftime("%Y%m%d"), cycle.strftime("%H")
    if source == "noaa":
        base = f"{NOMADS}/pub/data/nccf/com/gfs/prod/gfs.{date}/{hour}"
        return (f"{base}/wave/gridded/gfswave.t{hour}z.global.0p25.f{step:03d}.grib2.idx",
                f"{base}/atmos/gfs.t{hour}z.pgrb2.0p25.f{step:03d}.idx")
    if resolution == "0p1":
        base = f"https://storage.googleapis.com/ecmwf-open-data/{date}/{hour}z/ifs/0p1"
        return tuple(f"{base}/{stream}/{date}{hour}0000-{step}h-{stream}-fc-sfc.index" for stream in ("wave", "oper"))
    stream = "scwv" if cycle.date() < datetime(2026, 5, 12).date() and cycle.hour in (6, 18) else "wave"
    return (f"https://data.ecmwf.int/forecasts/{date}/{hour}z/ifs/0p25/{stream}/"
            f"{date}{hour}0000-{step}h-{stream}-fc.index",)


def _index_exists(source: str, url: str) -> bool:
    def probe() -> bytes:
        request = Request(url, headers={"User-Agent": "icron-test-v.2"}, method="HEAD")
        global _nomads_next_start
        if source == "noaa":
            with _nomads_request_lock:
                delay = _nomads_next_start - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                _nomads_next_start = time.monotonic() + NOMADS_MIN_INTERVAL
                _count_request(source)
        else:
            _count_request(source)
        response = urlopen(request, timeout=60)
        with response:
            return b""
    try:
        _retry(probe)
        return True
    except HTTPError as exc:
        # NOMADS answers 403 while a cycle directory is still being published.
        if exc.code == 404 or (source == "noaa" and exc.code == 403):
            return False
        raise


def select_cycle(source: str, now: datetime, last_step: int) -> datetime:
    now = now.astimezone(UTC)
    boundary = now.replace(hour=(now.hour // 6) * 6, minute=0, second=0, microsecond=0)
    # Publication can lag past the next boundary; never select an issue >=18h old.
    for lag in (0, 6, 12):
        cycle = boundary - timedelta(hours=lag)
        if all(_index_exists(source, url) for url in _index_urls(source, cycle, last_step)):
            return cycle
    raise CycleUnavailableError("no recent candidate cycle has the required last-lead indexes")


def select_ecmwf_source(now: datetime, last_step: int, explicit_cycle: datetime | None = None) -> tuple[datetime, str]:
    """Prefer a complete 0.1-degree pair; only missing indexes permit 0.25 fallback."""
    now = now.astimezone(UTC)
    boundary = now.replace(hour=(now.hour // 6) * 6, minute=0, second=0, microsecond=0)
    candidates = (explicit_cycle,) if explicit_cycle else tuple(boundary - timedelta(hours=lag) for lag in (0, 6, 12))
    for cycle in candidates:
        # The 06/18 IFS runs end at 90 hours.
        if cycle.date() < datetime(2026, 10, 20).date() or (last_step > 90 and cycle.hour in (6, 18)):
            continue
        if all(_index_exists("ecmwf", url) for url in _index_urls("ecmwf", cycle, last_step, "0p1")):
            return cycle, "0p1"
    if explicit_cycle is not None:
        if all(_index_exists("ecmwf", url) for url in _index_urls("ecmwf", explicit_cycle, last_step)):
            return explicit_cycle, "0p25"
        raise CycleUnavailableError("requested cycle has no required forecast indexes")
    return select_cycle("ecmwf", now, last_step), "0p25"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-cycle", help="Explicit UTC model issue cycle YYYYMMDDHH (00/06/12/18).")
    parser.add_argument("--sst-date", help="RTG SST analysis date YYYYMMDD; defaults to yesterday UTC.")
    parser.add_argument(
        "--steps",
        default=",".join(str(step) for step in DEFAULT_STEPS),
        help=f"Forecast hours, up to {MAX_STEPS} 3-hour steps in 0..{MAX_STEP_HOURS}.",
    )
    parser.add_argument("--spot", action="append", help="Spot id; repeat for multiple spots.")
    parser.add_argument("--source", action="append", choices=("noaa", "ecmwf"), help="Source to collect; repeatable. Defaults to both.")
    parser.add_argument("--public-spots", action="store_true", help="Use public spots from INGEST_URL/spots for release collection.")
    parser.add_argument("--post", action="store_true", help="POST each run to INGEST_URL/ingest/forecast with INGEST_TOKEN.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cycle = parse_cycle(args.model_cycle) if args.model_cycle else None
        steps = parse_steps(args.steps)
        sst_date = parse_sst_date(args.sst_date) if args.sst_date else datetime.now(UTC).date() - timedelta(days=1)
        if not isinstance(sst_date, datetime):
            sst_date = datetime.combine(sst_date, datetime.min.time(), tzinfo=UTC)
    except ValueError as exc:
        parser.error(str(exc))
    ingest_url = os.environ.get("INGEST_URL", "")
    if args.spot and any(SPOT_ID_PATTERN.fullmatch(spot_id) is None for spot_id in args.spot):
        parser.error("Each --spot must be a lowercase id containing 1..64 letters, digits, or hyphens.")
    if args.public_spots and not ingest_url:
        parser.error("INGEST_URL is required with --public-spots.")
    if args.post:
        ingest_token = os.environ.get("INGEST_TOKEN", "")
        if not ingest_url or not ingest_token:
            parser.error("INGEST_URL and INGEST_TOKEN are required with --post.")
        try:
            ingest_url = validate_ingest_origin(ingest_url)
        except ValueError as exc:
            parser.error(str(exc))
    elif args.public_spots:
        try:
            ingest_url = validate_ingest_origin(ingest_url)
        except ValueError as exc:
            parser.error(str(exc))
    if args.public_spots:
        try:
            available_spots = fetch_public_spots(ingest_url)
        except Exception as exc:
            detail = _source_error("", "ingest", "spots", None, exc)["error"]
            parser.error(f"Could not load public spots: {detail}")
    else:
        available_spots = SPOTS
    requested_spots = set(args.spot or ())
    if requested_spots - {str(spot["id"]) for spot in available_spots}:
        parser.error("A requested --spot was not found in the selected spot list.")
    spots = tuple(
        spot for spot in available_spots if not requested_spots or spot["id"] in requested_spots
    )
    sources = tuple(dict.fromkeys(args.source or ("noaa", "ecmwf")))
    with _request_count_lock:
        _request_counts.update(noaa=0, ecmwf=0)
    starts = {source: time.monotonic() for source in sources}
    ends: dict[str, float] = {}
    cycles: dict[str, datetime] = {}
    errors: list[dict[str, object]] = []
    ecmwf_resolution = "0p25"
    for source in sources:
        try:
            if source == "ecmwf":
                cycles[source], ecmwf_resolution = select_ecmwf_source(datetime.now(UTC), steps[-1], cycle)
            else:
                cycles[source] = cycle if cycle is not None else select_cycle(source, datetime.now(UTC), steps[-1])
        except Exception as exc:
            models = (MODEL_NAMES["noaa-wave"], MODEL_NAMES["noaa-wind"]) if source == "noaa" else (MODEL_NAMES["ecmwf-wave"],)
            errors.extend(_source_error(str(spot["id"]), source, model, None, exc, operation="select_cycle") for spot in spots for model in models)
            ends[source] = time.monotonic()
    clusters = cluster_spots(spots)
    print(f"NOAA clusters: {len(clusters)}", file=sys.stderr)
    tasks: list[tuple[str, Callable[[], tuple[list[dict[str, object]], list[dict[str, object]]]]]] = []
    if "noaa" in sources:
        if "noaa" in cycles:
            tasks.extend(("noaa", lambda cluster=cluster: collect_noaa_cluster(cluster, cycles["noaa"], steps)) for cluster in clusters)
        tasks.append(("noaa", lambda: collect_sst(spots, sst_date)))
    if "ecmwf" in cycles:
        tasks.append(("ecmwf", lambda: collect_ecmwf(spots, cycles["ecmwf"], steps, ecmwf_resolution)))

    runs: list[dict[str, object]] = []
    # Start the shared model alongside NOAA, rather than after every region's queue.
    tasks.sort(key=lambda task: task[0] != "ecmwf")
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(task): source for source, task in tasks}
        for future in as_completed(futures):
            source = futures[future]
            ends[source] = time.monotonic()
            try:
                task_runs, task_errors = future.result()
                runs.extend(task_runs)
                errors.extend(task_errors)
            except Exception as exc:
                errors.append(_source_error("", source, "", None, exc))
    for source in sources:
        ends.setdefault(source, time.monotonic())

    if args.post and sum(len(run["values"]) for run in runs) > MAX_POST_VALUES:
        errors.append({"source": "ingest", "error": "forecast batch exceeds the free-tier value budget; nothing posted"})
    elif args.post:
        for run in runs:
            try:
                post_run(run, ingest_url, ingest_token)
            except Exception as exc:
                errors.append(_source_error(str(run["spotId"]), str(run["source"]), str(run["model"]), None, exc, operation="post"))

    result = {
        "clusters": len(clusters),
        "cycles": {source: iso_utc(value) for source, value in cycles.items()},
        "timings": {source: {"seconds": round(ends[source] - starts[source], 3), "requests": _request_counts[source]} for source in sources},
        "runs": sorted(runs, key=lambda run: (str(run["spotId"]), str(run["source"]), str(run["model"]))),
        "errors": sorted(errors, key=lambda error: (str(error.get("spotId", "")), str(error.get("source", "")), str(error.get("model", "")), str(error.get("step", "")))),
    }
    print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
