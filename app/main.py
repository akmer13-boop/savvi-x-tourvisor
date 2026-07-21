import asyncio
import hmac
import logging
import math
import re
import time
import uuid
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import settings
from app.formatting import (
    format_tours_compact_for_suvvy,
    format_tours_for_client,
    format_tours_with_images_for_client,
)
from app.media import (
    cards_from_tours,
    image_assets_from_tours,
    message_blocks_from_tours,
    normalize_tour_media,
)
from app.models import BotResponse, ShortBotResponse, TourSearchRequest
from app.observability import (
    get_request_id,
    reset_request_id,
    safe_chat_id,
    set_request_id,
)
from app.operator_policy import OperatorPolicyConfigurationError
from app.ranking import select_best_tours
from app.runtime import operator_policy, search_guard
from app.search_guard import (
    ClaimAction,
    SearchGuard,
    SearchDispatchLimitReached,
    SearchGuardError,
)
from app.tourvisor_client import TourvisorClient
from app.validation import (
    ManagerRoutingRequired,
    SearchInputError,
    unverified_preferences,
    validate_and_normalize_search_request,
)

logging.basicConfig(level=settings.log_level)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def _prune_search_guard_periodically(guard: SearchGuard) -> None:
    """Remove expired replay payloads and 72-hour state while the app is live."""
    while True:
        await asyncio.sleep(settings.search_guard_prune_interval_seconds)
        try:
            await guard.aprune_expired()
        except SearchGuardError as exc:
            logger.error(
                "SEARCH_GUARD_PRUNE_FAILED error_type=%s",
                type(exc).__name__,
            )


@asynccontextmanager
async def app_lifespan(_: FastAPI):
    cleanup_task: asyncio.Task[None] | None = None
    if search_guard is not None:
        cleanup_task = asyncio.create_task(
            _prune_search_guard_periodically(search_guard),
            name="search-guard-prune",
        )
    try:
        yield
    finally:
        if cleanup_task is not None:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task


app = FastAPI(
    title="Suvvy ↔ Tourvisor Bridge",
    version=settings.service_version,
    description=(
        "Service that receives tour parameters from Suvvy, searches Tourvisor, "
        "and returns a safe compact response."
    ),
    docs_url="/docs" if settings.expose_api_docs else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.expose_api_docs else None,
    lifespan=app_lifespan,
)


def _new_request_id(request: Request) -> str:
    incoming = (request.headers.get("x-request-id") or "").strip()
    if re.fullmatch(
        r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
        incoming,
    ):
        return incoming
    return uuid.uuid4().hex


@app.middleware("http")
async def request_context_and_logging(request: Request, call_next):
    request_id = _new_request_id(request)
    request.state.request_id = request_id
    token = set_request_id(request_id)
    started = time.perf_counter()
    logger.info(
        "INCOMING request_id=%s method=%s path=%s",
        request_id,
        request.method,
        request.url.path,
    )
    try:
        if request.method == "POST" and request.url.path in PROTECTED_SEARCH_PATHS:
            body_token = None
            if settings.suvvy_allow_body_token:
                try:
                    payload = await request.json()
                except (ValueError, UnicodeDecodeError):
                    payload = None
                if isinstance(payload, dict) and isinstance(payload.get("auth_token"), str):
                    body_token = payload["auth_token"]
            try:
                verify_suvvy_token(
                    request.headers.get("authorization"),
                    body_token,
                )
            except HTTPException as exc:
                response = _http_error_response(request_id, exc)
                response.headers["X-Request-ID"] = request_id
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                logger.info(
                    "OUTGOING request_id=%s status=%s elapsed_ms=%s",
                    request_id,
                    response.status_code,
                    elapsed_ms,
                )
                return response
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "OUTGOING request_id=%s status=%s elapsed_ms=%s",
            request_id,
            response.status_code,
            elapsed_ms,
        )
        return response
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.error(
            "FAILED request_id=%s elapsed_ms=%s error_type=%s",
            request_id,
            elapsed_ms,
            type(exc).__name__,
        )
        raise
    finally:
        reset_request_id(token)


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", get_request_id())
    forbidden_policy = any(
        "operatorids" in str(error.get("msg", "")).lower()
        for error in exc.errors()
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "error" if forbidden_policy else "needs_clarification",
            "found": False,
            "reason": "FORBIDDEN_OPERATOR_FILTER" if forbidden_policy else "INVALID_REQUEST",
            "request_id": request_id,
            "client_text": (
                "Параметры туроператоров задаются сервером."
                if forbidden_policy
                else "Уточните параметры поездки."
            ),
            "tours_count": 0,
            "search_id": None,
            "whitelist_version": operator_policy.version,
            "whitelist_hash": operator_policy.short_hash,
            "unverified_preferences": [],
            "reused": False,
        },
    )


IMAGE_DELIVERY_NOTE = (
    "Сначала возвращается главное фото тура/отеля из результатов поиска, "
    "затем фотографии номера. В тарифе Suvvy без структурированных ответов "
    "прямые URL отображаются как ссылки."
)

NEUTRAL_FALLBACK_TEXT = (
    "Сейчас не удалось получить подборку. "
    "Я зафиксировала Ваш запрос — менеджер свяжется с Вами в ближайшее время."
)

PROTECTED_SEARCH_PATHS = {
    "/",
    "/tour-search",
    "/suvvy",
    "/api/suvvy/tour-search",
    "/tour-search-full",
    "/api/suvvy/tour-search-full",
}


def _error_content(
    request_id: str,
    *,
    reason: str,
    client_text: str = NEUTRAL_FALLBACK_TEXT,
) -> dict[str, object]:
    return {
        "status": "error",
        "found": False,
        "reason": reason,
        "request_id": request_id,
        "client_text": client_text,
        "tours_count": 0,
        "search_id": None,
        "whitelist_version": operator_policy.version,
        "whitelist_hash": operator_policy.short_hash,
        "unverified_preferences": [],
        "reused": False,
    }


def _http_error_response(request_id: str, exc: HTTPException) -> JSONResponse:
    reason = {
        status.HTTP_401_UNAUTHORIZED: "UNAUTHORIZED",
        status.HTTP_403_FORBIDDEN: "FORBIDDEN",
        status.HTTP_503_SERVICE_UNAVAILABLE: "CONFIGURATION_ERROR",
    }.get(exc.status_code, "HTTP_ERROR")
    client_text = NEUTRAL_FALLBACK_TEXT
    if isinstance(exc.detail, dict):
        reason = str(exc.detail.get("reason") or reason)
        client_text = str(exc.detail.get("client_text") or client_text)
    return JSONResponse(
        status_code=exc.status_code,
        content=_error_content(
            request_id,
            reason=reason,
            client_text=client_text,
        ),
        headers=exc.headers,
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    request_id = getattr(request.state, "request_id", get_request_id())
    return _http_error_response(request_id, exc)


def verify_suvvy_token(authorization: str | None, body_token: str | None = None) -> None:
    """Validate Suvvy without ever logging or returning credential values."""
    expected = settings.suvvy_webhook_token
    if not expected:
        if settings.mock_tourvisor:
            return
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook authentication is not configured",
        )

    provided_header = ""
    if authorization:
        scheme, separator, credential = authorization.partition(" ")
        if separator and scheme.lower() == "bearer":
            provided_header = credential.strip()
    if provided_header:
        accepted_tokens = (
            expected,
            settings.suvvy_previous_webhook_token,
        )
        if any(
            token and hmac.compare_digest(provided_header, token)
            for token in accepted_tokens
        ):
            return

    if (
        settings.suvvy_allow_body_token
        and body_token
        and hmac.compare_digest(body_token.strip(), expected)
    ):
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authorization token",
    )


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "service": "suvvy-tourvisor-bridge",
        "status": "ok",
        "version": settings.service_version,
    }


@app.get("/ping")
async def ping() -> dict[str, str]:
    return {"pong": "ok"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready", response_model=None)
async def ready() -> dict[str, str | int | bool] | JSONResponse:
    guard_ready = search_guard is not None
    if search_guard is not None:
        try:
            await search_guard.acheck_ready()
        except SearchGuardError as exc:
            logger.error(
                "READINESS_FAILED component=search_guard error_type=%s",
                type(exc).__name__,
            )
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": "error",
                    "reason": "SEARCH_GUARD_UNAVAILABLE",
                    "version": settings.service_version,
                    "api_contract_version": settings.api_contract_version,
                    "whitelist_version": operator_policy.version,
                    "whitelist_hash": operator_policy.short_hash,
                    "allowed_operator_count": operator_policy.active_count,
                    "search_guard_enabled": True,
                    "search_guard_ready": False,
                    "search_guard_persistence_verified": (
                        settings.search_guard_persistence_verified
                    ),
                },
            )
    return {
        "status": "ok",
        "version": settings.service_version,
        "api_contract_version": settings.api_contract_version,
        "tourvisor_api_contract_version": settings.tourvisor_api_contract_version,
        "tourvisor_price_from_enabled": settings.tourvisor_price_from_enabled,
        "git_commit": settings.git_commit_sha,
        "whitelist_version": operator_policy.version,
        "whitelist_hash": operator_policy.short_hash,
        "allowed_operator_count": operator_policy.active_count,
        "search_guard_enabled": settings.search_guard_enabled,
        "search_guard_ready": guard_ready,
        "search_guard_persistence_verified": settings.search_guard_persistence_verified,
    }


if settings.enable_debug_endpoints:

    @app.api_route(
        "/suvvy-debug",
        methods=["GET", "POST", "HEAD"],
        name="suvvy_debug",
        include_in_schema=False,
    )
    async def suvvy_debug_endpoint(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> Response:
        verify_suvvy_token(authorization)
        if request.method == "HEAD":
            return Response(status_code=200)
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "request_id": get_request_id(),
                "method": request.method,
                "path": request.url.path,
            },
        )


def _controlled_bot_response(
    request: TourSearchRequest,
    *,
    response_status: str,
    reason: str,
    client_text: str,
) -> BotResponse:
    return BotResponse(
        status=response_status,
        found=False,
        reason=reason,
        request_id=get_request_id(),
        client_text=client_text,
        tours_count=0,
        search_id=None,
        whitelist_version=operator_policy.version,
        whitelist_hash=operator_policy.short_hash,
        unverified_preferences=unverified_preferences(request),
        image_delivery_note=None,
    )


async def run_tour_search(
    request: TourSearchRequest,
    *,
    tour_limit: int = 5,
    compact_for_suvvy: bool = False,
    room_images_per_tour: int = 2,
) -> BotResponse:
    request_id = get_request_id()
    guard_attempt_id: str | None = None
    guard_dispatched = False

    async def abandon_guard_claim() -> None:
        if search_guard is None or guard_attempt_id is None or guard_dispatched:
            return
        try:
            await search_guard.aabandon_claim(guard_attempt_id)
        except SearchGuardError as exc:
            logger.error(
                "SEARCH_GUARD_FINALIZE_FAILED request_id=%s action=abandon error_type=%s",
                request_id,
                type(exc).__name__,
            )

    async def record_guard_failure(reason: str) -> None:
        if search_guard is None or guard_attempt_id is None:
            return
        try:
            if guard_dispatched:
                await search_guard.amark_failed(guard_attempt_id, reason)
            else:
                await search_guard.amark_preflight_failed(guard_attempt_id, reason)
        except SearchGuardError as exc:
            logger.error(
                "SEARCH_GUARD_FINALIZE_FAILED request_id=%s action=fail error_type=%s",
                request_id,
                type(exc).__name__,
            )

    try:
        request = validate_and_normalize_search_request(request)

        if search_guard is not None:
            if not request.chat_id:
                return _controlled_bot_response(
                    request,
                    response_status="error",
                    reason="CHAT_ID_REQUIRED",
                    client_text=NEUTRAL_FALLBACK_TEXT,
                )

            delivery_key = {
                "compact_for_suvvy": compact_for_suvvy,
                "tour_limit": max(tour_limit, 1),
                "room_images_per_tour": room_images_per_tour,
                "image_mode": request.image_mode,
                "hotel_preferences": request.hotel_preferences,
                "beach_preferences": request.beach_preferences,
                "whitelist_version": operator_policy.version,
                "whitelist_hash": operator_policy.sha256,
                "api_contract_version": settings.api_contract_version,
            }
            claim = await search_guard.aclaim(
                request.chat_id,
                request,
                refresh_requested=request.refresh_requested,
                delivery_key=delivery_key,
            )

            if claim.action is ClaimAction.IN_FLIGHT:
                if request.refresh_requested or not claim.delivery_matches:
                    return _controlled_bot_response(
                        request,
                        response_status="error",
                        reason="SEARCH_IN_PROGRESS",
                        client_text="Подборка уже формируется. Повторный поиск не запущен.",
                    )

                deadline = time.monotonic() + min(
                    max(float(settings.tourvisor_timeout_seconds), 5.0),
                    30.0,
                )
                while claim.action is ClaimAction.IN_FLIGHT and time.monotonic() < deadline:
                    await asyncio.sleep(0.25)
                    claim = await search_guard.aclaim(
                        request.chat_id,
                        request,
                        refresh_requested=False,
                        delivery_key=delivery_key,
                    )

            logger.info(
                "SEARCH_GUARD_DECISION request_id=%s chat_ref=%s fingerprint=%s "
                "action=%s dispatch_count=%s",
                request_id,
                claim.chat_key[:12],
                claim.search_fingerprint[:12],
                claim.action.value,
                claim.dispatch_count,
            )

            if claim.action is ClaimAction.REPLAY:
                replayed = BotResponse.model_validate(claim.replay_payload)
                return replayed.model_copy(
                    update={
                        "request_id": request_id,
                        "reused": True,
                        "unverified_preferences": unverified_preferences(request),
                    }
                )
            if claim.action is ClaimAction.IN_FLIGHT:
                return _controlled_bot_response(
                    request,
                    response_status="error",
                    reason="SEARCH_IN_PROGRESS",
                    client_text="Подборка уже формируется. Повторный поиск не запущен.",
                )
            if claim.action is ClaimAction.DUPLICATE:
                prior_failed = claim.prior_state is not None and claim.prior_state.value == "failed"
                return _controlled_bot_response(
                    request,
                    response_status="error",
                    reason="PREVIOUS_SEARCH_FAILED" if prior_failed else "DUPLICATE_SEARCH",
                    client_text=(
                        NEUTRAL_FALLBACK_TEXT
                        if prior_failed
                        else (
                            "Подборка по этим параметрам уже выполнялась. "
                            "Для проверки актуальных цен нужно явное подтверждение обновления."
                        )
                    ),
                )
            if claim.action is ClaimAction.LIMIT_REACHED:
                return _controlled_bot_response(
                    request,
                    response_status="error",
                    reason="SEARCH_LIMIT_REACHED",
                    client_text=NEUTRAL_FALLBACK_TEXT,
                )
            if claim.action is not ClaimAction.CLAIMED or claim.attempt_id is None:
                raise SearchGuardError("unexpected search guard decision")
            guard_attempt_id = claim.attempt_id

        logger.info(
            "SEARCH_VALIDATED request_id=%s chat_ref=%s budget_type=%s refresh=%s "
            "policy_version=%s policy_hash=%s allowed_count=%s",
            request_id,
            safe_chat_id(request.chat_id),
            request.budget_type or "legacy_max",
            request.refresh_requested,
            operator_policy.version,
            operator_policy.short_hash,
            operator_policy.active_count,
        )

        client = TourvisorClient(policy=operator_policy)

        async def before_dispatch() -> None:
            nonlocal guard_dispatched
            if search_guard is None or guard_attempt_id is None:
                return
            permit = await search_guard.amark_dispatched(guard_attempt_id)
            guard_dispatched = True
            logger.info(
                "SEARCH_GUARD_DISPATCH request_id=%s dispatch_number=%s remaining=%s",
                request_id,
                permit.dispatch_number,
                permit.remaining_dispatches,
            )

        search_id, tours = await client.search_tours(
            request,
            before_dispatch=before_dispatch if search_guard is not None else None,
        )
        selected = select_best_tours(
            tours,
            request,
            limit=max(tour_limit, 1),
            policy=operator_policy,
        )
        selected = await client.enrich_tours_with_hotel_details(selected)
        selected = await client.enrich_tours_with_room_details(selected)
        for tour in selected:
            normalize_tour_media(tour)

        if request.image_mode == "none":
            client_text = format_tours_for_client(
                selected,
                request,
                include_image_links=False,
            )
        elif compact_for_suvvy and request.image_mode in {"structured", "links_in_text"}:
            client_text = format_tours_compact_for_suvvy(
                selected,
                request,
                room_images_per_tour=room_images_per_tour,
            )
        elif request.image_mode in {"structured", "links_in_text"}:
            client_text = format_tours_with_images_for_client(
                selected,
                request,
                images_per_tour=room_images_per_tour,
            )
        else:
            client_text = format_tours_for_client(
                selected,
                request,
                include_image_links=False,
            )

        pending_preferences = unverified_preferences(request)
        if selected and pending_preferences:
            client_text += (
                "\n\nДополнительные пожелания по отелю и пляжу переданы "
                "менеджеру для обязательной проверки."
            )

        url_count = len(re.findall(r"https?://\S+", client_text))
        approx_output_tokens = math.ceil(len(client_text) / 2.5)
        logger.info(
            "SUVVY_RESPONSE_METRICS request_id=%s compact=%s tours=%s chars=%s "
            "urls=%s approx_output_tokens=%s",
            request_id,
            compact_for_suvvy,
            len(selected),
            len(client_text),
            url_count,
            approx_output_tokens,
        )

        images = (
            []
            if request.image_mode == "none"
            else image_assets_from_tours(
                selected,
                limit_per_tour=room_images_per_tour,
            )
        )
        cards = cards_from_tours(selected)
        messages = (
            []
            if request.image_mode == "none"
            else message_blocks_from_tours(client_text, selected)
        )

        response = BotResponse(
            status="ok",
            found=bool(selected),
            reason="FOUND" if selected else "NO_MATCHES",
            request_id=request_id,
            client_text=client_text,
            tours_count=len(selected),
            search_id=search_id,
            whitelist_version=operator_policy.version,
            whitelist_hash=operator_policy.short_hash,
            unverified_preferences=pending_preferences,
            tours=[tour.public_dict() for tour in selected],
            cards=cards,
            images=images,
            messages=messages,
            image_delivery_note=IMAGE_DELIVERY_NOTE if images else None,
        )
        if search_guard is not None and guard_attempt_id is not None:
            await search_guard.amark_succeeded(
                guard_attempt_id,
                response.model_dump(mode="json"),
            )
        return response
    except ManagerRoutingRequired as exc:
        await abandon_guard_claim()
        return _controlled_bot_response(
            request,
            response_status="error",
            reason=exc.reason,
            client_text=exc.client_text,
        )
    except SearchInputError as exc:
        await abandon_guard_claim()
        return _controlled_bot_response(
            request,
            response_status="needs_clarification",
            reason=exc.reason,
            client_text=exc.client_text,
        )
    except SearchDispatchLimitReached:
        await abandon_guard_claim()
        return _controlled_bot_response(
            request,
            response_status="error",
            reason="SEARCH_LIMIT_REACHED",
            client_text=NEUTRAL_FALLBACK_TEXT,
        )
    except OperatorPolicyConfigurationError as exc:
        await record_guard_failure("CONFIGURATION_ERROR")
        logger.error("CONFIGURATION_ERROR request_id=%s type=%s", request_id, type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "error",
                "found": False,
                "reason": "CONFIGURATION_ERROR",
                "request_id": request_id,
            },
        ) from exc
    except SearchGuardError as exc:
        logger.error(
            "SEARCH_GUARD_ERROR request_id=%s error_type=%s",
            request_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "error",
                "found": False,
                "reason": "SEARCH_GUARD_UNAVAILABLE",
                "request_id": request_id,
                "client_text": NEUTRAL_FALLBACK_TEXT,
            },
        ) from exc
    except httpx.TimeoutException as exc:
        await record_guard_failure("UPSTREAM_TIMEOUT")
        logger.warning(
            "TOUR_SEARCH_ERROR request_id=%s reason=UPSTREAM_TIMEOUT error_type=%s",
            request_id,
            type(exc).__name__,
        )
        return _controlled_bot_response(
            request,
            response_status="error",
            reason="UPSTREAM_TIMEOUT",
            client_text=NEUTRAL_FALLBACK_TEXT,
        )
    except Exception as exc:
        await record_guard_failure("UPSTREAM_ERROR")
        logger.error(
            "TOUR_SEARCH_ERROR request_id=%s reason=UPSTREAM_ERROR error_type=%s",
            request_id,
            type(exc).__name__,
        )
        return _controlled_bot_response(
            request,
            response_status="error",
            reason="UPSTREAM_ERROR",
            client_text=NEUTRAL_FALLBACK_TEXT,
        )


async def authenticated_tour_search(
    request: TourSearchRequest,
    authorization: str | None,
) -> BotResponse:
    verify_suvvy_token(authorization, request.auth_token)
    return await run_tour_search(request)


def to_short_response(response: BotResponse) -> ShortBotResponse:
    return ShortBotResponse(
        status=response.status,
        found=response.found,
        reason=response.reason,
        request_id=response.request_id,
        client_text=response.client_text,
        tours_count=response.tours_count,
        search_id=response.search_id,
        whitelist_version=response.whitelist_version,
        whitelist_hash=response.whitelist_hash,
        unverified_preferences=response.unverified_preferences,
        reused=response.reused,
    )


async def authenticated_tour_search_short(
    request: TourSearchRequest,
    authorization: str | None,
) -> ShortBotResponse:
    verify_suvvy_token(authorization, request.auth_token)
    response = await run_tour_search(
        request,
        tour_limit=settings.suvvy_tours_limit,
        compact_for_suvvy=settings.suvvy_compact_output,
        room_images_per_tour=settings.suvvy_room_images_per_tour,
    )
    return to_short_response(response)


@app.post("/tour-search", response_model=ShortBotResponse)
async def suvvy_tour_search_short(
    request: TourSearchRequest,
    authorization: str | None = Header(default=None),
) -> ShortBotResponse:
    return await authenticated_tour_search_short(request, authorization)


@app.post("/", response_model=ShortBotResponse, include_in_schema=False)
async def suvvy_tour_search_root(
    request: TourSearchRequest,
    authorization: str | None = Header(default=None),
) -> ShortBotResponse:
    return await authenticated_tour_search_short(request, authorization)


@app.post("/suvvy", response_model=ShortBotResponse, include_in_schema=False)
async def suvvy_tour_search_suvvy_alias(
    request: TourSearchRequest,
    authorization: str | None = Header(default=None),
) -> ShortBotResponse:
    return await authenticated_tour_search_short(request, authorization)


@app.post("/api/suvvy/tour-search", response_model=ShortBotResponse, include_in_schema=False)
async def suvvy_tour_search(
    request: TourSearchRequest,
    authorization: str | None = Header(default=None),
) -> ShortBotResponse:
    return await authenticated_tour_search_short(request, authorization)


@app.post("/tour-search-full", response_model=BotResponse, include_in_schema=False)
async def suvvy_tour_search_full(
    request: TourSearchRequest,
    authorization: str | None = Header(default=None),
) -> BotResponse:
    return await authenticated_tour_search(request, authorization)


@app.post("/api/suvvy/tour-search-full", response_model=BotResponse, include_in_schema=False)
async def suvvy_tour_search_full_api(
    request: TourSearchRequest,
    authorization: str | None = Header(default=None),
) -> BotResponse:
    return await authenticated_tour_search(request, authorization)
