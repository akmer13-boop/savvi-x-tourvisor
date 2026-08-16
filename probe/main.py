from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(levelname)s:%(name)s:%(message)s",
)

PROBE_VERSION = "0.1.0"
TOURVISOR_API_BASE_URL = os.getenv(
    "TOURVISOR_API_BASE_URL", "https://api.tourvisor.ru"
).rstrip("/")
TOURVISOR_JWT = (
    os.getenv("TOURVISOR_JWT") or os.getenv("TOURVISOR_API_KEY") or ""
).strip()
PROBE_TOKEN = os.getenv("PROBE_TOKEN", "").strip()
PROBE_CONNECT_TIMEOUT_SECONDS = max(
    int(os.getenv("PROBE_CONNECT_TIMEOUT_SECONDS", "10")), 1
)
PROBE_PREFLIGHT_TIMEOUT_SECONDS = max(
    int(os.getenv("PROBE_PREFLIGHT_TIMEOUT_SECONDS", "20")), 1
)
PROBE_MAX_READ_TIMEOUT_SECONDS = max(
    int(os.getenv("PROBE_MAX_READ_TIMEOUT_SECONDS", "300")), 30
)
PROBE_MAX_BILLABLE_JOBS_PER_PROCESS = max(
    int(os.getenv("PROBE_MAX_BILLABLE_JOBS_PER_PROCESS", "5")), 1
)
PROBE_JOB_HISTORY_LIMIT = max(int(os.getenv("PROBE_JOB_HISTORY_LIMIT", "20")), 5)

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
_billable_jobs_started = 0


class ProbeStartRequest(BaseModel):
    tour_id: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    currency: str = Field(default="RUB", min_length=3, max_length=8)
    read_timeout_seconds: int = Field(default=180, ge=30, le=300)
    label: str | None = Field(default=None, max_length=80)


class ProbeStartResponse(BaseModel):
    job_id: str
    status: str
    poll_path: str
    tour_id: str
    read_timeout_seconds: int


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _effective_read_timeout(requested: int) -> int:
    return min(max(requested, 30), PROBE_MAX_READ_TIMEOUT_SECONDS)


def _tourvisor_host() -> str:
    return urlparse(TOURVISOR_API_BASE_URL).netloc or "unknown"


def _auth_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {TOURVISOR_JWT}",
    }


def _require_probe_token(authorization: str | None = Header(default=None)) -> None:
    if not PROBE_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PROBE_TOKEN is not configured",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
        )
    supplied = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(supplied, PROBE_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid probe token",
        )


def _safe_response_headers(response: httpx.Response) -> dict[str, str]:
    return {
        "content_type": response.headers.get("content-type") or "none",
        "content_length": response.headers.get("content-length") or "none",
        "transfer_encoding": response.headers.get("transfer-encoding") or "none",
    }


def _port_name(endpoint: Any) -> str | None:
    if not isinstance(endpoint, dict):
        return None
    port = endpoint.get("port")
    if not isinstance(port, dict):
        return None
    value = port.get("shortName") or port.get("name")
    return str(value) if value else None


def _segment_endpoint(segment: Any, key: str) -> dict[str, Any]:
    if not isinstance(segment, dict):
        return {}
    value = segment.get(key)
    return value if isinstance(value, dict) else {}


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
    if isinstance(info, dict):
        flags = info.get("flags")
        if isinstance(flags, dict):
            summary["flags"] = {
                key: flags.get(key)
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
    forward = (
        selected.get("forward")
        if isinstance(selected.get("forward"), list)
        else []
    )
    backward = (
        selected.get("backward")
        if isinstance(selected.get("backward"), list)
        else []
    )
    forward = [item for item in forward if isinstance(item, dict)]
    backward = [item for item in backward if isinstance(item, dict)]

    price = selected.get("price")
    if isinstance(price, dict):
        summary["price"] = {
            "currency": price.get("currency"),
            "value": price.get("value"),
        }

    if forward:
        dep = _segment_endpoint(forward[0], "departure")
        arr = _segment_endpoint(forward[-1], "arrival")
        summary["forward"] = {
            "from": _port_name(dep),
            "to": _port_name(arr),
            "date": dep.get("date") or selected.get("dateForward"),
            "departure_time": dep.get("time"),
            "arrival_time": arr.get("time"),
            "segments": len(forward),
        }

    if backward:
        dep = _segment_endpoint(backward[0], "departure")
        arr = _segment_endpoint(backward[-1], "arrival")
        summary["backward"] = {
            "from": _port_name(dep),
            "to": _port_name(arr),
            "date": dep.get("date") or selected.get("dateBackward"),
            "departure_time": dep.get("time"),
            "arrival_time": arr.get("time"),
            "segments": len(backward),
        }

    return summary


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in job.items() if key != "_internal"}


def _remember_job(job_id: str, job: dict[str, Any]) -> None:
    _jobs[job_id] = job
    _jobs.move_to_end(job_id)
    while len(_jobs) > PROBE_JOB_HISTORY_LIMIT:
        old_job_id, _ = _jobs.popitem(last=False)
        old_task = _tasks.pop(old_job_id, None)
        if old_task is not None and not old_task.done():
            old_task.cancel()


def _active_job() -> dict[str, Any] | None:
    return next(
        (
            job
            for job in _jobs.values()
            if job.get("status") in {"queued", "running"}
        ),
        None,
    )


async def _run_probe(job_id: str) -> None:
    global _billable_jobs_started

    job = _jobs[job_id]
    job["status"] = "running"
    job["started_at"] = _now_iso()
    tour_id = str(job["tour_id"])
    currency = str(job["currency"])
    read_timeout_seconds = int(job["read_timeout_seconds"])

    preflight_started = time.perf_counter()
    try:
        timeout = httpx.Timeout(float(PROBE_PREFLIGHT_TIMEOUT_SECONDS))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{TOURVISOR_API_BASE_URL}/search/api/v1/tours/{tour_id}",
                params={"currency": currency},
                headers=_auth_headers(),
            )
            job["preflight_http_status"] = response.status_code
            response.raise_for_status()
            await response.aread()
        job["preflight_ms"] = _elapsed_ms(preflight_started)
        logger.info(
            "FLIGHT_PROBE_PREFLIGHT_SUCCESS job_id=%s tour_id=%s elapsed_ms=%s",
            job_id,
            tour_id,
            job["preflight_ms"],
        )
    except Exception as exc:  # noqa: BLE001 - probe must record, not crash
        job["preflight_ms"] = _elapsed_ms(preflight_started)
        job["status"] = "preflight_failed"
        job["error_type"] = type(exc).__name__
        job["finished_at"] = _now_iso()
        logger.warning(
            "FLIGHT_PROBE_PREFLIGHT_FAILED job_id=%s tour_id=%s error_type=%s elapsed_ms=%s",
            job_id,
            tour_id,
            type(exc).__name__,
            job["preflight_ms"],
        )
        return

    async with _state_lock:
        if _billable_jobs_started >= PROBE_MAX_BILLABLE_JOBS_PER_PROCESS:
            job["status"] = "quota_guard_blocked"
            job["finished_at"] = _now_iso()
            return
        _billable_jobs_started += 1
        job["billable_sequence"] = _billable_jobs_started

    connect_timeout = float(PROBE_CONNECT_TIMEOUT_SECONDS)
    timeout = httpx.Timeout(
        connect=connect_timeout,
        read=float(read_timeout_seconds),
        write=connect_timeout,
        pool=connect_timeout,
    )
    request_started = time.perf_counter()
    headers_received = False
    body_started: float | None = None

    logger.info(
        "FLIGHT_PROBE_REQUEST_STARTED job_id=%s tour_id=%s read_timeout_seconds=%s",
        job_id,
        tour_id,
        read_timeout_seconds,
    )

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "GET",
                f"{TOURVISOR_API_BASE_URL}/search/api/v1/tours/{tour_id}/flights",
                params={"currency": currency},
                headers=_auth_headers(),
            ) as response:
                headers_received = True
                job["headers_ms"] = _elapsed_ms(request_started)
                job["flight_http_status"] = response.status_code
                job["response_headers"] = _safe_response_headers(response)
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
                job["body_ms"] = _elapsed_ms(body_started)
                job["response_bytes"] = len(raw)
                payload = json.loads(raw)
                job["result"] = _summarize_flight_payload(payload)

        job["total_ms"] = _elapsed_ms(request_started)
        job["status"] = "success"
        logger.info(
            "FLIGHT_PROBE_SUCCESS job_id=%s tour_id=%s headers_ms=%s body_ms=%s total_ms=%s flights_count=%s",
            job_id,
            tour_id,
            job.get("headers_ms"),
            job.get("body_ms"),
            job["total_ms"],
            job.get("result", {}).get("flights_count"),
        )
    except httpx.ConnectTimeout:
        job["total_ms"] = _elapsed_ms(request_started)
        job["status"] = "connect_timeout"
        job["phase"] = "connect"
        logger.warning(
            "FLIGHT_PROBE_CONNECT_TIMEOUT job_id=%s tour_id=%s total_ms=%s",
            job_id,
            tour_id,
            job["total_ms"],
        )
    except httpx.ReadTimeout:
        job["total_ms"] = _elapsed_ms(request_started)
        job["status"] = "timeout"
        job["phase"] = "body" if headers_received else "headers"
        if headers_received and body_started is not None:
            job["body_ms"] = _elapsed_ms(body_started)
        logger.warning(
            "FLIGHT_PROBE_READ_TIMEOUT job_id=%s tour_id=%s phase=%s total_ms=%s",
            job_id,
            tour_id,
            job["phase"],
            job["total_ms"],
        )
    except httpx.HTTPStatusError as exc:
        job["total_ms"] = _elapsed_ms(request_started)
        job["status"] = "http_error"
        job["phase"] = "headers" if headers_received else "request"
        job["flight_http_status"] = exc.response.status_code
        logger.warning(
            "FLIGHT_PROBE_HTTP_ERROR job_id=%s tour_id=%s status=%s total_ms=%s",
            job_id,
            tour_id,
            exc.response.status_code,
            job["total_ms"],
        )
    except Exception as exc:  # noqa: BLE001 - probe must record, not crash
        job["total_ms"] = _elapsed_ms(request_started)
        job["status"] = "error"
        job["phase"] = "body" if headers_received else "request"
        job["error_type"] = type(exc).__name__
        logger.warning(
            "FLIGHT_PROBE_FAILED job_id=%s tour_id=%s error_type=%s total_ms=%s",
            job_id,
            tour_id,
            type(exc).__name__,
            job["total_ms"],
        )
    finally:
        job["finished_at"] = _now_iso()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": PROBE_VERSION}


@app.get("/ready", response_model=None)
async def ready() -> dict[str, Any] | JSONResponse:
    ready_state = bool(TOURVISOR_JWT and PROBE_TOKEN)
    active = _active_job()
    payload = {
        "status": "ok" if ready_state else "error",
        "version": PROBE_VERSION,
        "app_module": "probe.main:app",
        "tourvisor_host": _tourvisor_host(),
        "tourvisor_jwt_configured": bool(TOURVISOR_JWT),
        "probe_token_configured": bool(PROBE_TOKEN),
        "connect_timeout_seconds": PROBE_CONNECT_TIMEOUT_SECONDS,
        "preflight_timeout_seconds": PROBE_PREFLIGHT_TIMEOUT_SECONDS,
        "max_read_timeout_seconds": PROBE_MAX_READ_TIMEOUT_SECONDS,
        "max_billable_jobs_per_process": PROBE_MAX_BILLABLE_JOBS_PER_PROCESS,
        "billable_jobs_started": _billable_jobs_started,
        "active_job": _public_job(active) if active else None,
    }
    if ready_state:
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
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="TOURVISOR_JWT is not configured",
        )

    effective_timeout = _effective_read_timeout(request.read_timeout_seconds)
    async with _state_lock:
        active = _active_job()
        if active is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Probe job {active['job_id']} is already running",
            )
        if _billable_jobs_started >= PROBE_MAX_BILLABLE_JOBS_PER_PROCESS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    "Per-process billable probe limit reached; restart only if "
                    "another probe is explicitly approved"
                ),
            )

        job_id = uuid.uuid4().hex[:16]
        job = {
            "job_id": job_id,
            "status": "queued",
            "created_at": _now_iso(),
            "tour_id": request.tour_id,
            "currency": request.currency.upper(),
            "read_timeout_seconds": effective_timeout,
            "label": request.label,
            "billable": True,
        }
        _remember_job(job_id, job)
        task = asyncio.create_task(
            _run_probe(job_id),
            name=f"flight-probe-{job_id}",
        )
        _tasks[job_id] = task

    return ProbeStartResponse(
        job_id=job_id,
        status="queued",
        poll_path=f"/probe/{job_id}",
        tour_id=request.tour_id,
        read_timeout_seconds=effective_timeout,
    )


@app.get(
    "/probe/{job_id}",
    dependencies=[Depends(_require_probe_token)],
)
async def get_probe(job_id: str) -> dict[str, Any]:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found",
        )
    return _public_job(job)


@app.get(
    "/probes",
    dependencies=[Depends(_require_probe_token)],
)
async def list_probes() -> dict[str, Any]:
    return {
        "billable_jobs_started": _billable_jobs_started,
        "max_billable_jobs_per_process": PROBE_MAX_BILLABLE_JOBS_PER_PROCESS,
        "jobs": [_public_job(job) for job in reversed(_jobs.values())],
    }
