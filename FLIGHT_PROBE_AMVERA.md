# Standalone Tourvisor Flight Probe on Amvera

This probe is intentionally separate from Savvi and Bitrix. The production bridge keeps
`TOURVISOR_ENABLE_FLIGHT_ACTUALIZATION=false`. A second Amvera application runs the same
repository image with `APP_MODULE=probe.main:app`.

## Modes

### Fresh-search mode — recommended

`POST /probe/fresh-start` first performs one normal Tourvisor tour search using the same active-contract
operator registry as the production bridge. It selects a current `tour_id`, verifies it through
`GET /tours/{tourId}`, and only then performs exactly one `/flights` request.

This removes the main weakness of the original diagnostic flow: saved Tourvisor `tour_id` values can
expire and start returning `404` before the timing test is run.

The job records separately:

- `fresh_search_total_ms` — dictionary resolution + one current tour search + polling/results;
- `preflight_ms` — current-tour validation;
- `headers_ms` — time from starting `/flights` until HTTP headers arrive;
- `body_ms` — time from headers until the full response body is received;
- `total_ms` — `/flights` request time;
- `fresh_search_call_started` and `flight_call_started` — explicit call flags;
- safe selected-tour metadata and compact flight result.

### Existing tour-id mode

`POST /probe/start` is still available when a known current Tourvisor `tour_id` already exists. It does
not create a fresh tour search. If preflight returns `404`, `/flights` is not called.

The raw response body, Tourvisor JWT and Authorization headers are never returned or logged. Only one
probe job can run at a time.

## Amvera variables

```env
APP_MODULE=probe.main:app
TOURVISOR_API_BASE_URL=https://api.tourvisor.ru
TOURVISOR_JWT=<same Tourvisor JWT as the bridge>
PROBE_TOKEN=<long random secret>
PROBE_CONNECT_TIMEOUT_SECONDS=10
PROBE_PREFLIGHT_TIMEOUT_SECONDS=20
PROBE_MAX_READ_TIMEOUT_SECONDS=300

# /flights safety guard
PROBE_MAX_BILLABLE_JOBS_PER_PROCESS=5

# Fresh-search safety guard and polling
PROBE_MAX_FRESH_SEARCHES_PER_PROCESS=3
PROBE_SEARCH_POLL_ATTEMPTS=10
PROBE_SEARCH_POLL_INTERVAL_SECONDS=2
PROBE_SEARCH_RESULTS_LIMIT=25
PROBE_OPERATOR_REGISTRY_PATH=config/operator_registry.json

PROBE_JOB_HISTORY_LIMIT=20
LOG_LEVEL=INFO
```

`/ready` should show probe version `0.2.0`, `fresh_search_ready=true`, the active operator count, and
separate counters for fresh searches and `/flights` calls.

## Recommended controlled PowerShell test

The example below requests a current Moscow → Turkey tour for 11 November 2026, 7 nights, 2 adults,
up to 500,000 RUB, then measures one `/flights` call for the selected current tour.

```powershell
$base = "https://<PROBE_URL>"
$headers = @{ Authorization = "Bearer <PROBE_TOKEN>" }

$body = @{
  departure_city = "Москва"
  country = "Турция"
  date_from = "2026-11-11"
  date_to = "2026-11-11"
  nights_from = 7
  nights_to = 7
  adults = 2
  children_ages = @()
  price_to = 500000
  currency = "RUB"
  read_timeout_seconds = 180
  label = "fresh-mow-turkey-180s"
} | ConvertTo-Json

$start = Invoke-RestMethod `
  -Method Post `
  -Uri "$base/probe/fresh-start" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body

$start | ConvertTo-Json -Depth 10
```

Poll the returned job without holding one long HTTP connection:

```powershell
do {
  $result = Invoke-RestMethod `
    -Method Get `
    -Uri "$base/probe/$($start.job_id)" `
    -Headers $headers

  $result | ConvertTo-Json -Depth 10

  if ($result.status -in @("queued", "running")) {
    Start-Sleep -Seconds 10
  }
} while ($result.status -in @("queued", "running"))
```

## Interpretation

- `success` + `headers_ms=65000` — `/flights` works; the real upstream wait is about 65 seconds.
- `timeout`, `phase=headers`, `total_ms≈180000` — no `/flights` HTTP headers arrived in 180 seconds.
- `timeout`, `phase=body` — headers arrived but the body stalled.
- `fresh_search_no_results` — the fresh search returned no eligible active-contract tour under the requested ceiling; `/flights` was not called.
- `preflight_failed` after a successful fresh search — Tourvisor returned a current search result but the selected tour was already unavailable at the ordinary tour endpoint; `/flights` was not called.
- `http_error` — `/flights` returned an HTTP error; inspect `flight_http_status`.

Recommended sequence: one 180-second fresh job first. Only if it reaches `/flights` and times out at
`phase=headers`, run one explicitly approved 300-second job.
