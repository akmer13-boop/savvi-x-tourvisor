from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
import unicodedata
import uuid
from collections import OrderedDict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(levelname)s:%(name)s:%(message)s",
)
logger = logging.getLogger(__name__)

PROBE_VERSION = "0.2.0"
BASE_URL = os.getenv("TOURVISOR_API_BASE_URL", "https://api.tourvisor.ru").rstrip("/")
TOURVISOR_JWT = os.getenv("TOURVISOR_JWT", "").strip()
PROBE_TOKEN = os.getenv("PROBE_TOKEN", "").strip()
CONNECT_TIMEOUT = max(int(os.getenv("PROBE_CONNECT_TIMEOUT_SECONDS", "10")), 1)
PREFLIGHT_TIMEOUT = max(int(os.getenv("PROBE_PREFLIGHT_TIMEOUT_SECONDS", "20")), 1)
MAX_READ_TIMEOUT = max(int(os.getenv("PROBE_MAX_READ_TIMEOUT_SECONDS", "300")), 30)
MAX_FLIGHT_CALLS = max(int(os.getenv("PROBE_MAX_BILLABLE_JOBS_PER_PROCESS", "5")), 1)
MAX_FRESH_SEARCHES = max(int(os.getenv("PROBE_MAX_FRESH_SEARCHES_PER_PROCESS", "3")), 1)
HISTORY_LIMIT = max(int(os.getenv("PROBE_JOB_HISTORY_LIMIT", "20")), 5)
SEARCH_POLL_ATTEMPTS = max(int(os.getenv("PROBE_SEARCH_POLL_ATTEMPTS", "10")), 1)
SEARCH_POLL_INTERVAL = max(float(os.getenv("PROBE_SEARCH_POLL_INTERVAL_SECONDS", "2")), 0.0)
SEARCH_RESULTS_LIMIT = min(max(int(os.getenv("PROBE_SEARCH_RESULTS_LIMIT", "25")), 1), 100)
OPERATOR_REGISTRY_PATH = os.getenv(
    "PROBE_OPERATOR_REGISTRY_PATH",
    "config/operator_registry.json",
).strip()

app = FastAPI(
    title="Tourvisor Flight Probe",
    version=PROBE_VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

_jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
_tasks: dict[str, asyncio.Task[None]] = {}
_state_lock = asyncio.Lock()
_flight_calls_started = 0
_fresh_searches_started = 0


class ProbeStartRequest(BaseModel):
    tour_id: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    currency: str = Field(default="RUB", min_length=3, max_length=8)
    read_timeout_seconds: int = Field(default=180, ge=30, le=300)
    label: str | None = Field(default=None, max_length=80)


class FreshProbeStartRequest(BaseModel):
    departure_city: str = Field(default="Москва", min_length=2, max_length=80)
    country: str = Field(default="Турция", min_length=2, max_length=80)
    date_from: date
    date_to: date | None = None
    nights_from: int = Field(default=7, ge=1, le=30)
    nights_to: int | None = Field(default=None, ge=1, le=30)
    adults: int = Field(default=2, ge=1, le=8)
    children_ages: list[int] = Field(default_factory=list, max_length=3)
    price_to: int | None = Field(default=500_000, ge=1)
    currency: str = Field(default="RUB", min_length=3, max_length=8)
    read_timeout_seconds: int = Field(default=180, ge=30, le=300)
    label: str | None = Field(default=None, max_length=80)


class ProbeStartResponse(BaseModel):
    job_id: str
    status: str
    poll_path: str
    tour_id: str | None = None
    read_timeout_seconds: int
    mode: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _effective_read_timeout(requested: int) -> int:
    return min(max(requested, 30), MAX_READ_TIMEOUT)


def _auth_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {TOURVISOR_JWT}",
    }


def _require_probe_token(authorization: str | None = Header(default=None)) -> None:
    if not PROBE_TOKEN:
        raise HTTPException(status_code=503, detail="PROBE_TOKEN is not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    if not secrets.compare_digest(authorization[7:].strip(), PROBE_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid probe token")


def _endpoint(segment: Any, key: str) -> dict[str, Any]:
    if not isinstance(segment, dict):
        return {}
    value = segment.get(key)
    return value if isinstance(value, dict) else {}


def _port(endpoint: dict[str, Any]) -> str | None:
    port = endpoint.get("port")
    if not isinstance(port, dict):
        return None
    value = port.get("shortName") or port.get("name")
    return str(value) if value else None


def _leg(
    segments: Any,
    date_key: str,
    selected: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(segments, list):
        return None
    valid = [item for item in segments if isinstance(item, dict)]
    if not valid:
        return None
    dep = _endpoint(valid[0], "departure")
    arr = _endpoint(valid[-1], "arrival")
    return {
        "from": _port(dep),
        "to": _port(arr),
        "date": dep.get("date") or selected.get(date_key),
        "departure_time": dep.get("time"),
        "arrival_time": arr.get("time"),
        "segments": len(valid),
    }


def _summarize_flight_payload(payload: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "payload_type": type(payload).__name__,
        "flights_count": 0,
    }
    if not isinstance(payload, dict):
        return summary

    error = payload.get("error")
    if isinstance(error, dict):
        summary["error"] = {
            "code": error.get("code"),
            "reason": str(error.get("reason") or "")[:160],
        }

    info = payload.get("info")
    flags = info.get("flags") if isinstance(info, dict) else None
    if isinstance(flags, dict):
        summary["flags"] = {
            key: flags[key]
            for key in ("noFlight", "noInsurance", "noMeal", "noTransfer")
            if key in flags
        }

    flights = payload.get("flights")
    if not isinstance(flights, list):
        return summary
    options = [item for item in flights if isinstance(item, dict)]
    summary["flights_count"] = len(options)
    if not options:
        return summary

    selected = next(
        (item for item in options if item.get("isDefault") is True),
        options[0],
    )
    price = selected.get("price")
    if isinstance(price, dict):
        summary["price"] = {
            "currency": price.get("currency"),
            "value": price.get("value"),
        }
    forward = _leg(selected.get("forward"), "dateForward", selected)
    backward = _leg(selected.get("backward"), "dateBackward", selected)
    if forward:
        summary["forward"] = forward
    if backward:
        summary["backward"] = backward
    return summary


def _normalize(value: Any) -> str:
    text = str(value or "").strip().lower().replace("ё", "е")
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _find_by_name(items: Any, target: str) -> dict[str, Any] | None:
    if not isinstance(items, list):
        return None
    target_norm = _normalize(target)
    if not target_norm:
        return None

    fuzzy: list[tuple[int, dict[str, Any]]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        names = [
            item.get("name"),
            item.get("russianName"),
            item.get("fullName"),
            item.get("fullRussianName"),
        ]
        normalized = [_normalize(value) for value in names if value]
        if target_norm in normalized:
            return item
        for candidate in normalized:
            if candidate and (target_norm in candidate or candidate in target_norm):
                fuzzy.append((abs(len(candidate) - len(target_norm)), item))
    if fuzzy:
        return sorted(fuzzy, key=lambda pair: pair[0])[0][1]
    return None


def _to_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(str(value).replace(" ", "")))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _nested_name(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("russianName", "fullRussianName", "fullName", "name"):
            if value.get(key):
                return str(value[key])
    if isinstance(value, str):
        return value
    return None


def _load_active_operator_ids() -> list[int]:
    try:
        payload = json.loads(Path(OPERATOR_REGISTRY_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    operators = payload.get("operators") if isinstance(payload, dict) else None
    if not isinstance(operators, list):
        return []

    result: list[int] = []
    for item in operators:
        if not isinstance(item, dict) or item.get("status") != "active_contract":
            continue
        operator_id = _to_int(item.get("tourvisor_id"))
        if operator_id is not None:
            result.append(operator_id)
    return sorted(set(result))


def _select_fresh_tour(
    data: Any,
    *,
    price_to: int | None,
    operator_ids: set[int],
) -> dict[str, Any] | None:
    if not isinstance(data, list):
        return None

    candidates: list[tuple[int, dict[str, Any]]] = []
    for hotel in data:
        if not isinstance(hotel, dict):
            continue
        rating = _to_float(hotel.get("rating"))
        if rating is not None and rating < 4.0:
            continue
        tours = hotel.get("tours")
        if not isinstance(tours, list):
            continue

        for tour in tours:
            if not isinstance(tour, dict) or tour.get("id") is None:
                continue

            operator = tour.get("operator")
            operator_id = (
                _to_int(operator.get("id"))
                if isinstance(operator, dict)
                else None
            )
            if operator_ids and operator_id not in operator_ids:
                continue

            price = _to_int(tour.get("price"))
            if price is None or price <= 0:
                price = _to_int(hotel.get("price"))
            if price is None or price <= 0:
                continue
            if price_to is not None and price > price_to:
                continue

            summary = {
                "tour_id": str(tour["id"]),
                "hotel_id": _to_int(hotel.get("id")),
                "hotel": str(hotel.get("name") or "unknown"),
                "hotel_rating": rating,
                "operator_id": operator_id,
                "operator": _nested_name(operator),
                "search_price": price,
                "currency": str(tour.get("currency") or hotel.get("currency") or "RUB"),
                "date": tour.get("date"),
                "nights": _to_int(tour.get("nights")),
            }
            candidates.append((price, summary))

    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _active_job() -> dict[str, Any] | None:
    return next(
        (job for job in _jobs.values() if job["status"] in {"queued", "running"}),
        None,
    )


def _remember(job: dict[str, Any]) -> None:
    job_id = str(job["job_id"])
    _jobs[job_id] = job
    while len(_jobs) > HISTORY_LIMIT:
        old_id, _ = _jobs.popitem(last=False)
        task = _tasks.pop(old_id, None)
        if task and not task.done():
            task.cancel()


async def _get_json(
    client: httpx.AsyncClient,
    path: str,
    *,
    params: dict[str, Any] | None = None,
) -> Any:
    response = await client.get(
        f"{BASE_URL}{path}",
        params=params or {},
        headers=_auth_headers(),
    )
    response.raise_for_status()
    return response.json()


async def _resolve_fresh_tour(job: dict[str, Any]) -> bool:
    global _fresh_searches_started

    request = job["fresh_request"]
    stage_started = time.perf_counter()
    job["stage"] = "fresh_search"
    operator_ids = _load_active_operator_ids()
    job["active_operator_count"] = len(operator_ids)
    if not operator_ids:
        job.update(
            status="fresh_search_config_error",
            error_type="OperatorRegistryEmpty",
            fresh_search_total_ms=_ms(stage_started),
        )
        return False

    try:
        async with httpx.AsyncClient(timeout=float(PREFLIGHT_TIMEOUT)) as client:
            dictionaries_started = time.perf_counter()
            departures = await _get_json(client, "/search/api/v1/departures")
            departure = _find_by_name(departures, str(request["departure_city"]))
            if not departure:
                job.update(
                    status="fresh_search_input_error",
                    error_type="DepartureNotFound",
                    fresh_search_total_ms=_ms(stage_started),
                )
                return False

            countries = await _get_json(
                client,
                "/search/api/v1/countries",
                params={
                    "departureId": departure.get("id"),
                    "onlyCharter": False,
                    "onlyDirect": False,
                },
            )
            country = _find_by_name(countries, str(request["country"]))
            if not country:
                job.update(
                    status="fresh_search_input_error",
                    error_type="CountryNotFound",
                    fresh_search_total_ms=_ms(stage_started),
                )
                return False
            job["dictionary_ms"] = _ms(dictionaries_started)

            date_from = str(request["date_from"])
            date_to = str(request.get("date_to") or date_from)
            nights_from = int(request["nights_from"])
            nights_to = int(request.get("nights_to") or nights_from)

            search_params: dict[str, Any] = {
                "departureId": departure.get("id"),
                "countryId": country.get("id"),
                "dateFrom": date_from,
                "dateTo": date_to,
                "nightsFrom": nights_from,
                "nightsTo": nights_to,
                "adults": int(request["adults"]),
                "currency": str(request["currency"]),
                "onlyCharter": False,
                "onlyDirect": False,
                "hotelRating": 4,
                "operatorIds": operator_ids,
            }
            children_ages = request.get("children_ages") or []
            if children_ages:
                search_params["childs"] = children_ages
            if request.get("price_to") is not None:
                search_params["priceTo"] = int(request["price_to"])

            async with _state_lock:
                if _fresh_searches_started >= MAX_FRESH_SEARCHES:
                    job.update(
                        status="fresh_search_quota_guard_blocked",
                        fresh_search_total_ms=_ms(stage_started),
                    )
                    return False
                _fresh_searches_started += 1
                job["fresh_search_sequence"] = _fresh_searches_started
                job["fresh_search_call_started"] = True

            search_started = time.perf_counter()
            search_payload = await _get_json(
                client,
                "/search/api/v1/tours/search",
                params=search_params,
            )
            job["search_start_ms"] = _ms(search_started)
            search_id = (
                str(search_payload.get("searchId") or "")
                if isinstance(search_payload, dict)
                else ""
            )
            if not search_id:
                job.update(
                    status="fresh_search_failed",
                    error_type="MissingSearchId",
                    fresh_search_total_ms=_ms(stage_started),
                )
                return False
            job["search_id"] = search_id

            poll_started = time.perf_counter()
            for attempt in range(SEARCH_POLL_ATTEMPTS):
                if SEARCH_POLL_INTERVAL:
                    await asyncio.sleep(SEARCH_POLL_INTERVAL)
                try:
                    status_payload = await _get_json(
                        client,
                        f"/search/api/v1/tours/search/{search_id}/status",
                        params={"operatorStatus": False},
                    )
                except Exception as exc:  # noqa: BLE001 - partial results may still exist
                    logger.warning(
                        "FLIGHT_PROBE_FRESH_STATUS_FAILED job_id=%s search_id=%s "
                        "attempt=%s error_type=%s",
                        job["job_id"],
                        search_id,
                        attempt + 1,
                        type(exc).__name__,
                    )
                    continue

                progress = (
                    _to_int(status_payload.get("progress"))
                    if isinstance(status_payload, dict)
                    else None
                ) or 0
                upstream_status = (
                    str(status_payload.get("status") or "").lower()
                    if isinstance(status_payload, dict)
                    else ""
                )
                job["search_progress"] = progress
                job["search_status"] = upstream_status
                job["search_poll_attempts"] = attempt + 1
                if progress >= 100 or upstream_status in {
                    "done",
                    "complete",
                    "completed",
                    "finished",
                    "finish",
                }:
                    break
            job["search_poll_ms"] = _ms(poll_started)

            results_started = time.perf_counter()
            results = await _get_json(
                client,
                f"/search/api/v1/tours/search/{search_id}",
                params={"limit": SEARCH_RESULTS_LIMIT},
            )
            job["search_results_ms"] = _ms(results_started)
            job["search_result_hotels"] = len(results) if isinstance(results, list) else 0

            selected = _select_fresh_tour(
                results,
                price_to=request.get("price_to"),
                operator_ids=set(operator_ids),
            )
            if not selected:
                job.update(
                    status="fresh_search_no_results",
                    fresh_search_total_ms=_ms(stage_started),
                )
                return False

            job["selected_tour"] = selected
            job["tour_id"] = selected["tour_id"]
            job["fresh_search_total_ms"] = _ms(stage_started)
            logger.info(
                "FLIGHT_PROBE_FRESH_TOUR_SELECTED job_id=%s search_id=%s "
                "tour_id=%s fresh_search_ms=%s operator_id=%s search_price=%s",
                job["job_id"],
                search_id,
                job["tour_id"],
                job["fresh_search_total_ms"],
                selected.get("operator_id"),
                selected.get("search_price"),
            )
            return True
    except Exception as exc:  # noqa: BLE001
        job.update(
            status="fresh_search_failed",
            fresh_search_total_ms=_ms(stage_started),
            error_type=type(exc).__name__,
        )
        logger.warning(
            "FLIGHT_PROBE_FRESH_SEARCH_FAILED job_id=%s error_type=%s elapsed_ms=%s",
            job["job_id"],
            type(exc).__name__,
            job["fresh_search_total_ms"],
        )
        return False


async def _run_probe(job_id: str) -> None:
    global _flight_calls_started

    job = _jobs[job_id]
    job.update(status="running", started_at=_now())

    if job.get("mode") == "fresh_search":
        if not await _resolve_fresh_tour(job):
            job["finished_at"] = _now()
            return

    tour_id = str(job["tour_id"])
    currency = str(job["currency"])
    job["stage"] = "preflight"

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=float(PREFLIGHT_TIMEOUT)) as client:
            response = await client.get(
                f"{BASE_URL}/search/api/v1/tours/{tour_id}",
                params={"currency": currency},
                headers=_auth_headers(),
            )
            job["preflight_http_status"] = response.status_code
            response.raise_for_status()
        job["preflight_ms"] = _ms(started)
        logger.info(
            "FLIGHT_PROBE_PREFLIGHT_SUCCESS job_id=%s tour_id=%s elapsed_ms=%s",
            job_id,
            tour_id,
            job["preflight_ms"],
        )
    except Exception as exc:  # noqa: BLE001
        job.update(
            status="preflight_failed",
            preflight_ms=_ms(started),
            error_type=type(exc).__name__,
            finished_at=_now(),
        )
        logger.warning(
            "FLIGHT_PROBE_PREFLIGHT_FAILED job_id=%s tour_id=%s error_type=%s",
            job_id,
            tour_id,
            type(exc).__name__,
        )
        return

    async with _state_lock:
        if _flight_calls_started >= MAX_FLIGHT_CALLS:
            job.update(status="flight_quota_guard_blocked", finished_at=_now())
            return
        _flight_calls_started += 1
        job["flight_call_started"] = True
        job["flight_call_sequence"] = _flight_calls_started

    read_timeout = int(job["read_timeout_seconds"])
    timeout = httpx.Timeout(
        connect=float(CONNECT_TIMEOUT),
        read=float(read_timeout),
        write=float(CONNECT_TIMEOUT),
        pool=float(CONNECT_TIMEOUT),
    )
    started = time.perf_counter()
    headers_received = False
    body_started: float | None = None
    job["stage"] = "flights"
    logger.info(
        "FLIGHT_PROBE_REQUEST_STARTED job_id=%s tour_id=%s read_timeout_seconds=%s",
        job_id,
        tour_id,
        read_timeout,
    )

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "GET",
                f"{BASE_URL}/search/api/v1/tours/{tour_id}/flights",
                params={"currency": currency},
                headers=_auth_headers(),
            ) as response:
                headers_received = True
                job.update(
                    headers_ms=_ms(started),
                    flight_http_status=response.status_code,
                    response_headers={
                        "content_type": response.headers.get("content-type") or "none",
                        "content_length": response.headers.get("content-length") or "none",
                        "transfer_encoding": response.headers.get("transfer-encoding") or "none",
                    },
                )
                logger.info(
                    "FLIGHT_PROBE_HEADERS job_id=%s tour_id=%s status=%s headers_ms=%s",
                    job_id,
                    tour_id,
                    response.status_code,
                    job["headers_ms"],
                )
                response.raise_for_status()
                body_started = time.perf_counter()
                raw = await response.aread()
                job.update(
                    body_ms=_ms(body_started),
                    response_bytes=len(raw),
                    result=_summarize_flight_payload(json.loads(raw)),
                )
        job.update(status="success", total_ms=_ms(started))
        logger.info(
            "FLIGHT_PROBE_SUCCESS job_id=%s tour_id=%s total_ms=%s",
            job_id,
            tour_id,
            job["total_ms"],
        )
    except httpx.ConnectTimeout:
        job.update(status="connect_timeout", phase="connect", total_ms=_ms(started))
    except httpx.ReadTimeout:
        job.update(
            status="timeout",
            phase="body" if headers_received else "headers",
            total_ms=_ms(started),
        )
        if headers_received and body_started is not None:
            job["body_ms"] = _ms(body_started)
        logger.warning(
            "FLIGHT_PROBE_READ_TIMEOUT job_id=%s tour_id=%s phase=%s total_ms=%s",
            job_id,
            tour_id,
            job["phase"],
            job["total_ms"],
        )
    except httpx.HTTPStatusError as exc:
        job.update(
            status="http_error",
            phase="headers" if headers_received else "request",
            total_ms=_ms(started),
            flight_http_status=exc.response.status_code,
        )
    except Exception as exc:  # noqa: BLE001
        job.update(
            status="error",
            phase="body" if headers_received else "request",
            total_ms=_ms(started),
            error_type=type(exc).__name__,
        )
    finally:
        job["finished_at"] = _now()


def _new_job(
    *,
    mode: str,
    currency: str,
    read_timeout_seconds: int,
    label: str | None,
    tour_id: str | None = None,
    fresh_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "job_id": uuid.uuid4().hex[:16],
        "mode": mode,
        "status": "queued",
        "stage": "queued",
        "created_at": _now(),
        "tour_id": tour_id,
        "currency": currency.upper(),
        "read_timeout_seconds": _effective_read_timeout(read_timeout_seconds),
        "label": label,
        "fresh_search_call_started": False,
        "flight_call_started": False,
        **({"fresh_request": fresh_request} if fresh_request else {}),
    }


async def _schedule_job(
    job: dict[str, Any],
    *,
    require_fresh_slot: bool,
) -> ProbeStartResponse:
    async with _state_lock:
        active = _active_job()
        if active:
            raise HTTPException(
                status_code=409,
                detail=f"Probe job {active['job_id']} is already running",
            )
        if _flight_calls_started >= MAX_FLIGHT_CALLS:
            raise HTTPException(
                status_code=429,
                detail="Per-process flight probe limit reached",
            )
        if require_fresh_slot and _fresh_searches_started >= MAX_FRESH_SEARCHES:
            raise HTTPException(
                status_code=429,
                detail="Per-process fresh-search probe limit reached",
            )

        _remember(job)
        job_id = str(job["job_id"])
        _tasks[job_id] = asyncio.create_task(
            _run_probe(job_id),
            name=f"flight-probe-{job_id}",
        )

    return ProbeStartResponse(
        job_id=str(job["job_id"]),
        status="queued",
        poll_path=f"/probe/{job['job_id']}",
        tour_id=job.get("tour_id"),
        read_timeout_seconds=int(job["read_timeout_seconds"]),
        mode=str(job["mode"]),
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": PROBE_VERSION}


@app.get("/ready", response_model=None)
async def ready() -> dict[str, Any] | JSONResponse:
    is_ready = bool(TOURVISOR_JWT and PROBE_TOKEN)
    active_operator_ids = _load_active_operator_ids()
    payload = {
        "status": "ok" if is_ready else "error",
        "version": PROBE_VERSION,
        "app_module": "probe.main:app",
        "tourvisor_host": urlparse(BASE_URL).netloc,
        "tourvisor_jwt_configured": bool(TOURVISOR_JWT),
        "probe_token_configured": bool(PROBE_TOKEN),
        "connect_timeout_seconds": CONNECT_TIMEOUT,
        "preflight_timeout_seconds": PREFLIGHT_TIMEOUT,
        "max_read_timeout_seconds": MAX_READ_TIMEOUT,
        "max_flight_calls_per_process": MAX_FLIGHT_CALLS,
        "flight_calls_started": _flight_calls_started,
        "max_fresh_searches_per_process": MAX_FRESH_SEARCHES,
        "fresh_searches_started": _fresh_searches_started,
        "active_operator_count": len(active_operator_ids),
        "fresh_search_ready": bool(active_operator_ids),
        "active_job": _active_job() is not None,
    }
    if is_ready:
        return payload
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=payload,
    )


@app.post(
    "/probe/start",
    response_model=ProbeStartResponse,
    dependencies=[Depends(_require_probe_token)],
)
async def start_probe(request: ProbeStartRequest) -> ProbeStartResponse:
    if not TOURVISOR_JWT:
        raise HTTPException(status_code=503, detail="TOURVISOR_JWT is not configured")

    job = _new_job(
        mode="tour_id",
        tour_id=request.tour_id,
        currency=request.currency,
        read_timeout_seconds=request.read_timeout_seconds,
        label=request.label,
    )
    return await _schedule_job(job, require_fresh_slot=False)


@app.post(
    "/probe/fresh-start",
    response_model=ProbeStartResponse,
    dependencies=[Depends(_require_probe_token)],
)
async def start_fresh_probe(request: FreshProbeStartRequest) -> ProbeStartResponse:
    if not TOURVISOR_JWT:
        raise HTTPException(status_code=503, detail="TOURVISOR_JWT is not configured")
    if request.date_to is not None and request.date_to < request.date_from:
        raise HTTPException(status_code=422, detail="date_to must be on or after date_from")
    if request.nights_to is not None and request.nights_to < request.nights_from:
        raise HTTPException(status_code=422, detail="nights_to must be >= nights_from")
    if any(age < 0 or age > 17 for age in request.children_ages):
        raise HTTPException(status_code=422, detail="children_ages must be between 0 and 17")

    fresh_request = {
        "departure_city": request.departure_city,
        "country": request.country,
        "date_from": request.date_from.isoformat(),
        "date_to": request.date_to.isoformat() if request.date_to else None,
        "nights_from": request.nights_from,
        "nights_to": request.nights_to,
        "adults": request.adults,
        "children_ages": request.children_ages,
        "price_to": request.price_to,
        "currency": request.currency.upper(),
    }
    job = _new_job(
        mode="fresh_search",
        currency=request.currency,
        read_timeout_seconds=request.read_timeout_seconds,
        label=request.label,
        fresh_request=fresh_request,
    )
    return await _schedule_job(job, require_fresh_slot=True)


@app.get("/probe/{job_id}", dependencies=[Depends(_require_probe_token)])
async def get_probe(job_id: str) -> dict[str, Any]:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/probes", dependencies=[Depends(_require_probe_token)])
async def list_probes() -> dict[str, Any]:
    return {
        "flight_calls_started": _flight_calls_started,
        "max_flight_calls_per_process": MAX_FLIGHT_CALLS,
        "fresh_searches_started": _fresh_searches_started,
        "max_fresh_searches_per_process": MAX_FRESH_SEARCHES,
        "jobs": list(reversed(_jobs.values())),
    }
