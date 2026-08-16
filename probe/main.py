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

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(levelname)s:%(name)s:%(message)s",
)
logger = logging.getLogger(__name__)

PROBE_VERSION = "0.1.0"
BASE_URL = os.getenv("TOURVISOR_API_BASE_URL", "https://api.tourvisor.ru").rstrip("/")
TOURVISOR_JWT = os.getenv("TOURVISOR_JWT", "").strip()
PROBE_TOKEN = os.getenv("PROBE_TOKEN", "").strip()
CONNECT_TIMEOUT = max(int(os.getenv("PROBE_CONNECT_TIMEOUT_SECONDS", "10")), 1)
PREFLIGHT_TIMEOUT = max(int(os.getenv("PROBE_PREFLIGHT_TIMEOUT_SECONDS", "20")), 1)
MAX_READ_TIMEOUT = max(int(os.getenv("PROBE_MAX_READ_TIMEOUT_SECONDS", "300")), 30)
MAX_BILLABLE = max(int(os.getenv("PROBE_MAX_BILLABLE_JOBS_PER_PROCESS", "5")), 1)
HISTORY_LIMIT = max(int(os.getenv("PROBE_JOB_HISTORY_LIMIT", "20")), 5)

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
_billable_started = 0


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


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _effective_read_timeout(requested: int) -> int:
    return min(max(requested, 30), MAX_READ_TIMEOUT)


def _auth_headers() -> dict[str, str]:
    return {"Accept": "application/json", "Authorization": f"Bearer {TOURVISOR_JWT}"}


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


async def _run_probe(job_id: str) -> None:
    global _billable_started

    job = _jobs[job_id]
    job.update(status="running", started_at=_now())
    tour_id = str(job["tour_id"])
    currency = str(job["currency"])

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
        if _billable_started >= MAX_BILLABLE:
            job.update(status="quota_guard_blocked", finished_at=_now())
            return
        _billable_started += 1
        job["billable_sequence"] = _billable_started

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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": PROBE_VERSION}


@app.get("/ready", response_model=None)
async def ready() -> dict[str, Any] | JSONResponse:
    is_ready = bool(TOURVISOR_JWT and PROBE_TOKEN)
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
        "max_billable_jobs_per_process": MAX_BILLABLE,
        "billable_jobs_started": _billable_started,
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

    async with _state_lock:
        active = _active_job()
        if active:
            raise HTTPException(
                status_code=409,
                detail=f"Probe job {active['job_id']} is already running",
            )
        if _billable_started >= MAX_BILLABLE:
            raise HTTPException(
                status_code=429,
                detail="Per-process billable probe limit reached",
            )
        job_id = uuid.uuid4().hex[:16]
        timeout = _effective_read_timeout(request.read_timeout_seconds)
        job = {
            "job_id": job_id,
            "status": "queued",
            "created_at": _now(),
            "tour_id": request.tour_id,
            "currency": request.currency.upper(),
            "read_timeout_seconds": timeout,
            "label": request.label,
            "billable": True,
        }
        _remember(job)
        _tasks[job_id] = asyncio.create_task(
            _run_probe(job_id),
            name=f"flight-probe-{job_id}",
        )

    return ProbeStartResponse(
        job_id=job_id,
        status="queued",
        poll_path=f"/probe/{job_id}",
        tour_id=request.tour_id,
        read_timeout_seconds=timeout,
    )


@app.get("/probe/{job_id}", dependencies=[Depends(_require_probe_token)])
async def get_probe(job_id: str) -> dict[str, Any]:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/probes", dependencies=[Depends(_require_probe_token)])
async def list_probes() -> dict[str, Any]:
    return {
        "billable_jobs_started": _billable_started,
        "max_billable_jobs_per_process": MAX_BILLABLE,
        "jobs": list(reversed(_jobs.values())),
    }
