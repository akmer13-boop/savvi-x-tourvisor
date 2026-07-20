import hmac
import logging
import math
import re
import time
import uuid

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
from app.runtime import operator_policy
from app.tourvisor_client import TourvisorClient
from app.validation import (
    SearchInputError,
    unverified_preferences,
    validate_and_normalize_search_request,
)

logging.basicConfig(level=settings.log_level)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

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
)


def _new_request_id(request: Request) -> str:
    incoming = (request.headers.get("x-request-id") or "").strip()
    if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", incoming):
        return incoming
    return uuid.uuid4().hex[:12]


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
    except Exception:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.exception("FAILED request_id=%s elapsed_ms=%s", request_id, elapsed_ms)
        raise
    finally:
        reset_request_id(token)


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    del exc
    request_id = getattr(request.state, "request_id", get_request_id())
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "status": "needs_clarification",
            "found": False,
            "reason": "INVALID_REQUEST",
            "request_id": request_id,
            "client_text": "Уточните параметры поездки.",
            "tours_count": 0,
            "search_id": None,
            "whitelist_version": operator_policy.version,
            "whitelist_hash": operator_policy.short_hash,
            "unverified_preferences": [],
        },
    )


IMAGE_DELIVERY_NOTE = (
    "Сначала возвращается главное фото тура/отеля из результатов поиска, "
    "затем фотографии номера. В тарифе Suvvy без структурированных ответов "
    "прямые URL отображаются как ссылки."
)


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
    if authorization and authorization.startswith("Bearer "):
        provided_header = authorization.removeprefix("Bearer ").strip()
    if provided_header and hmac.compare_digest(provided_header, expected):
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


@app.get("/ready")
async def ready() -> dict[str, str | int]:
    return {
        "status": "ok",
        "version": settings.service_version,
        "git_commit": settings.git_commit_sha,
        "whitelist_version": operator_policy.version,
        "whitelist_hash": operator_policy.short_hash,
        "allowed_operator_count": operator_policy.active_count,
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


async def run_tour_search(
    request: TourSearchRequest,
    *,
    tour_limit: int = 5,
    compact_for_suvvy: bool = False,
    room_images_per_tour: int = 2,
) -> BotResponse:
    request_id = get_request_id()
    try:
        request = validate_and_normalize_search_request(request)
        logger.info(
            "SEARCH_VALIDATED request_id=%s chat_id=%s budget=%s nights_from=%s nights_to=%s "
            "policy_version=%s policy_hash=%s allowed_count=%s",
            request_id,
            safe_chat_id(request.chat_id),
            request.budget,
            request.nights_from,
            request.nights_to,
            operator_policy.version,
            operator_policy.short_hash,
            operator_policy.active_count,
        )

        client = TourvisorClient(policy=operator_policy)
        search_id, tours = await client.search_tours(request)
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

        return BotResponse(
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
    except SearchInputError as exc:
        return BotResponse(
            status="needs_clarification",
            found=False,
            reason=exc.reason,
            request_id=request_id,
            client_text=exc.client_text,
            tours_count=0,
            search_id=None,
            whitelist_version=operator_policy.version,
            whitelist_hash=operator_policy.short_hash,
            unverified_preferences=unverified_preferences(request),
            image_delivery_note=None,
        )
    except OperatorPolicyConfigurationError as exc:
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
    except httpx.TimeoutException:
        logger.exception("Tourvisor timeout request_id=%s", request_id)
        return BotResponse(
            status="error",
            found=False,
            reason="UPSTREAM_TIMEOUT",
            request_id=request_id,
            client_text=(
                "Сейчас Tourvisor отвечает дольше обычного. "
                "Я зафиксировала Ваш запрос — менеджер свяжется с Вами в ближайшее время."
            ),
            tours_count=0,
            search_id=None,
            whitelist_version=operator_policy.version,
            whitelist_hash=operator_policy.short_hash,
            unverified_preferences=unverified_preferences(request),
            image_delivery_note=None,
        )
    except Exception:
        logger.exception("Tour search failed request_id=%s", request_id)
        return BotResponse(
            status="error",
            found=False,
            reason="UPSTREAM_ERROR",
            request_id=request_id,
            client_text=(
                "Сейчас не удалось получить автоматическую подборку. "
                "Я зафиксировала Ваш запрос — менеджер свяжется с Вами в ближайшее время."
            ),
            tours_count=0,
            search_id=None,
            whitelist_version=operator_policy.version,
            whitelist_hash=operator_policy.short_hash,
            unverified_preferences=unverified_preferences(request),
            image_delivery_note=None,
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
