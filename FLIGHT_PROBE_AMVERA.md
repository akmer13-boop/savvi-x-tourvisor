# Standalone Tourvisor Flight Probe on Amvera

This probe is intentionally separate from Savvi and Bitrix. The production bridge keeps
`TOURVISOR_ENABLE_FLIGHT_ACTUALIZATION=false`. A second Amvera application runs the same
repository image with `APP_MODULE=probe.main:app`.

## What it measures

For one Tourvisor `tour_id` the probe performs:

1. `GET /search/api/v1/tours/{tourId}?currency=RUB` as a preflight check.
2. Exactly one `GET /search/api/v1/tours/{tourId}/flights?currency=RUB` billable call.
3. It records:
   - `preflight_ms`;
   - `headers_ms` — time from starting `/flights` until HTTP headers arrive;
   - `body_ms` — time from headers until the full body arrives;
   - `total_ms`;
   - HTTP status and safe response metadata;
   - a compact parsed flight summary (count, route, times, price, Tourvisor error/flags).

The raw response body, Tourvisor JWT and Authorization headers are never returned or logged.
Only one probe job can run at a time. The process also refuses to start more than the configured
number of billable jobs.

## Amvera application

Create a **new** Amvera application from this repository. Do not reuse the production
`savvi-x-tourvisor` application.

Environment variables:

```env
APP_MODULE=probe.main:app
TOURVISOR_API_BASE_URL=https://api.tourvisor.ru
TOURVISOR_JWT=<same Tourvisor JWT as the bridge>
PROBE_TOKEN=<long random secret>
PROBE_CONNECT_TIMEOUT_SECONDS=10
PROBE_PREFLIGHT_TIMEOUT_SECONDS=20
PROBE_MAX_READ_TIMEOUT_SECONDS=300
PROBE_MAX_BILLABLE_JOBS_PER_PROCESS=5
LOG_LEVEL=INFO
```

After deploy:

```text
GET /health
GET /ready
```

`/ready` must show `app_module=probe.main:app`, both token/JWT flags as true, and no secret values.

## Controlled test from PowerShell

Replace `<PROBE_URL>` and `<PROBE_TOKEN>`.

```powershell
$base = "https://<PROBE_URL>"
$headers = @{ Authorization = "Bearer <PROBE_TOKEN>" }

$body = @{
  tour_id = "53266342454481"
  currency = "RUB"
  read_timeout_seconds = 180
  label = "control-180s"
} | ConvertTo-Json

$start = Invoke-RestMethod `
  -Method Post `
  -Uri "$base/probe/start" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body

$start
```

Copy `job_id` from the response and poll without holding one long external HTTP connection:

```powershell
Invoke-RestMethod `
  -Method Get `
  -Uri "$base/probe/$($start.job_id)" `
  -Headers $headers | ConvertTo-Json -Depth 8
```

Repeat every 10–20 seconds until `status` is no longer `queued`/`running`.

## Interpretation

- `success`, `headers_ms=65000`, `total_ms=67000` — `/flights` works, but 45 seconds was too short.
- `timeout`, `phase=headers`, `total_ms≈180000` — Tourvisor did not send HTTP headers within 180 seconds.
- `timeout`, `phase=body` — headers arrived, but response body stalled.
- `preflight_failed` — the tour ID is invalid/expired or the ordinary Tour endpoint is unavailable; the billable `/flights` call is not made.
- `http_error` — Tourvisor returned an HTTP error; check `flight_http_status`.

Recommended controlled sequence: one 180-second job first. Only if it times out at `phase=headers`,
run one explicitly approved 300-second job. Avoid repeated calls with the same goal: `/flights` is
billable/search-quota traffic.
