# icron test v.2

Bounded NOAA GFS/GFS-Wave, ECMWF wave and NOAA RTG SST collection. Sources, models, grids and issue times remain separate. Missing data is omitted. No safety assessment is performed.

Install pinned dependencies in `requirements.txt`. Offline tests: `python -m unittest discover -p 'test_*.py'`.

The scheduled workflow uses Ubuntu standard hosted runners at `33 5,11,17,23 * * *`. It requires `COLLECTION_ENABLED=1`, has read-only repository permissions, and has no pull-request trigger.

Configure existing `INGEST_URL` (bare HTTPS origin) and `INGEST_TOKEN` as repository secrets. Never commit either. The endpoint must implement public `GET /spots` and authenticated `POST /ingest/forecast`. Optional `TIDE_MAPPINGS` is a whitespace-separated secret of reviewed `point-id=station-id` pairs; `TIDES_ENABLED=1` enables that step. No station mapping is inferred.

Scheduled collection loads configured public points, at most30, without development fallback. Each source selects the newest available of three explicit cycles, at the current UTC boundary, six hours before it or twelve hours before it, by checking its final requested lead index. The oldest automatic candidate is less than18hours old at selection; actual issue times are preserved. Only unavailable indexes (404 and the existing NOAA-only403 handling) advance to an older cycle; other HTTP/transport failures remain failures. An explicit `model_cycle` input bypasses selection. Default40 leads:0–90h every3h then96–144h every6h. NOAA requests group same-region points within2degrees latitude/longitude span and share a0.51s minimum start interval. A batch is bounded to50,000 values; consumers must independently validate each run.

The wrapper emits only whitelisted counts, selected cycles, source timings and grouped error diagnostics (fixed source/operation/code and numeric HTTP status). Raw payloads, configured origins, provider logs and endpoint errors stay in temporary invocation files and are never uploaded as artifacts. Do not add debug dumps or untrusted actions to secret-bearing workflows. Forks have no configured credentials.

Provider references: [NOAA NOMADS](https://nomads.ncep.noaa.gov/), [ECMWF Open Data](https://www.ecmwf.int/en/forecasts/datasets/open-data), [NOAA CO-OPS](https://api.tidesandcurrents.noaa.gov/api/prod/). Preserve their units and source semantics. Model forecasts and dated analyses do not guarantee local conditions.
