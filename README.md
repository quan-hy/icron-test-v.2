# icron test v.2

Bounded NOAA GFS/GFS-Wave, ECMWF wave/10 m wind and NOAA RTG SST collection. Sources, models, grids and issue times remain separate. Missing data is omitted. No safety assessment is performed.

Install pinned dependencies in `requirements.txt`. Offline tests: `python -m unittest discover -p 'test_*.py'`.

The scheduled workflow uses Ubuntu standard hosted runners at `33 5,11,17,23 * * *`. It requires `COLLECTION_ENABLED=1`, has read-only repository permissions, and has no pull-request trigger.

Configure existing `INGEST_URL` (bare HTTPS origin) and `INGEST_TOKEN` as repository secrets. Never commit either. The endpoint must implement public `GET /spots` and authenticated `POST /ingest/forecast`. Optional `TIDE_MAPPINGS` is a whitespace-separated secret of reviewed `point-id=station-id` pairs; `TIDES_ENABLED=1` enables that step. No station mapping is inferred.

Scheduled collection loads configured public points, at most30, without development fallback. Each source selects the newest available of three explicit cycles, at the current UTC boundary, six hours before it or twelve hours before it, by checking its final requested lead index. The oldest automatic candidate is less than18hours old at selection; actual issue times are preserved. Only unavailable indexes (404 and the existing NOAA-only403 handling) advance to an older cycle; other HTTP/transport failures remain failures. An explicit `model_cycle` input bypasses selection. Default40 leads:0–90h every3h then96–144h every6h. NOAA requests group same-region points within2degrees latitude/longitude span and share a0.51s minimum start interval. A batch is bounded to50,000 values; consumers must independently validate each run.

The wrapper emits only whitelisted counts, selected cycles, source timings and grouped error diagnostics (fixed source/operation/code and numeric HTTP status). Raw payloads, configured origins, provider logs and endpoint errors stay in temporary invocation files and are never uploaded as artifacts. Do not add debug dumps or untrusted actions to secret-bearing workflows. Forks have no configured credentials.

Provider references: [NOAA NOMADS](https://nomads.ncep.noaa.gov/), [ECMWF Open Data](https://www.ecmwf.int/en/forecasts/datasets/open-data), [NOAA CO-OPS](https://api.tidesandcurrents.noaa.gov/api/prod/). Preserve their units and source semantics. Model forecasts and dated analyses do not guarantee local conditions.

## ECMWF finer grid

The collector prefers the ECMWF IFS 0.1° open distribution, with separate
`ifs.wave.0p1` and `ifs.0p1` model identifiers. The native atmosphere and wave
models are approximately 9 km; coastal masks can still map adjacent points to the
same ocean cell. Data are provided by ECMWF under CC BY 4.0. Mean wave period is
not interchangeable with NOAA primary-wave period.

The official Google mirror supplies the 0.1° files. Availability checks require
both wave and atmosphere final-lead indexes. The 144-hour collection uses 00/12
UTC cycles; 06/18 UTC 0.1° forecasts end at 90 hours. Missing publication falls
back to legacy 0.25° wave collection, while NOAA remains independent. Transport
and authorization failures stay visible. The pinned SDK's surface filenames and
single-range requests are adapted without changing dependencies, request timeouts,
concurrency, batch limits, source timestamps or missing-data handling.

## Weekly regional research candidates

A separate `Weekly content candidates` workflow runs Mondays at 03:17 UTC (12:17 KST) on the standard Ubuntu runner. It uses no secrets, paid APIs, AI generation, image downloads or application writes. The existing forecast workflow and its enablement are unchanged.

Eight official regional pages contribute titles and same-origin related links only. Robots restrictions, redirects, authentication and response limits are respected conservatively; a blocked page remains a manual review item. Wikimedia Commons searches return at most six license-filtered photo candidates per region. Search topics rotate weekly. Collection failures remain visible rather than being treated as empty successful results.

The `regional-candidates-<run id>` artifact is retained for 14 days. All candidates are pending human review; source timestamps are collection times, not publication dates or field verification. Verify exact photo location, author attribution, license and depicted-person restrictions before reuse. Community posts are never collected or exported. No automatic publishing is configured.

Offline validation: `node --experimental-strip-types --test tests/content-discovery.test.ts`. Preview sources without network: `node --experimental-strip-types tools/content/collect.ts`. Authorized collection: `node --experimental-strip-types tools/content/collect.ts --execute --output /tmp/ivysurf-candidates`.

Public scheduled workflows may be disabled after 60 days without repository activity; check Actions status during the weekly editorial review.
