import logging
import time
import uuid

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.formatting import format_tours_for_client
from app.media import cards_from_tours, image_assets_from_tours, message_blocks_from_tours, normalize_tour_media
from app.models import BotResponse, ShortBotResponse, TourSearchRequest
from app.ranking import select_best_tours
from app.tourvisor_client import TourvisorClient, UserInputError

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Suvvy ↔ Tourvisor Bridge",
    version="0.2.2",
    description=(
        "Service that receives tour parameters from Suvvy, searches Tourvisor, "
        "and returns clean text plus structured cards/images for user-friendly delivery."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)




@app.middleware("http")
async def log_every_incoming_request(request: Request, call_next):
    """Log requests at entry point, before route handling.

    This is intentionally noisy while debugging Suvvy webhooks: if the request
    reaches FastAPI at all, Amvera logs will show INCOMING immediately, even if
    the client disconnects, the body is invalid, or downstream Tourvisor is slow.
    """
    request_id = uuid.uuid4().hex[:8]
    started = time.perf_counter()
    logger.info(
        "INCOMING request_id=%s method=%s path=%s query=%s user_agent=%s",
        request_id,
        request.method,
        request.url.path,
        request.url.query,
        request.headers.get("user-agent", ""),
    )
    try:
        response = await call_next(request)
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


IMAGE_DELIVERY_NOTE = (
    "Webhook-действие Suvvy получает JSON-результат для бота. "
    "Ссылки на фото не вставлены в client_text, чтобы клиент не видел сырые URL. "
    "Для вывода фото как картинок используйте массив images/messages и настройку канала/действия, "
    "которое умеет отправлять image attachments."
)


def verify_suvvy_token(authorization: str | None, body_token: str | None = None) -> None:
    """Validate request from Suvvy.

    Preferred: Authorization header = Bearer <SUVVY_WEBHOOK_TOKEN>.
    Fallback for Swagger/Suvvy UI issues: auth_token field in JSON body = <SUVVY_WEBHOOK_TOKEN>.
    """
    if not settings.suvvy_webhook_token:
        return

    expected_header = f"Bearer {settings.suvvy_webhook_token}"
    if authorization == expected_header:
        return

    if body_token and body_token.strip() == settings.suvvy_webhook_token:
        return

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authorization token",
    )


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "suvvy-tourvisor-bridge", "status": "ok", "version": "0.2.2"}


@app.get("/ping")
async def ping() -> dict[str, str]:
    return {"pong": "ok"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}




@app.api_route("/suvvy-debug", methods=["GET", "POST", "HEAD", "OPTIONS"])
@app.api_route("/debug", methods=["GET", "POST", "HEAD", "OPTIONS"])
@app.api_route("/tour-search-fast", methods=["GET", "POST", "HEAD", "OPTIONS"])
async def suvvy_debug_endpoint(request: Request) -> Response:
    """Ultra-fast webhook diagnostics endpoint for Suvvy.

    It does not validate tokens and does not call Tourvisor. Use it only to prove
    whether a Suvvy webhook reaches the Amvera FastAPI container.
    """
    if request.method == "HEAD":
        return Response(status_code=200)

    body_preview = ""
    try:
        raw = await request.body()
        body_preview = raw[:500].decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        body_preview = "<unable to read body>"

    logger.info(
        "SUVVY_DEBUG_HIT method=%s path=%s body_preview=%s",
        request.method,
        request.url.path,
        body_preview,
    )
    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "found": True,
            "client_text": "Диагностика успешна: Suvvy дошёл до Amvera. Вебхук работает, можно возвращать боевой URL поиска тура.",
            "source": "amvera_fastapi_debug",
            "method": request.method,
            "path": request.url.path,
        },
    )


async def run_tour_search(request: TourSearchRequest) -> BotResponse:
    try:
        client = TourvisorClient()
        search_id, tours = await client.search_tours(request)
        selected = select_best_tours(tours, request, limit=5)
        selected = await client.enrich_tours_with_room_details(selected)
        for tour in selected:
            normalize_tour_media(tour)

        include_image_links = request.image_mode == "links_in_text"
        client_text = format_tours_for_client(selected, request, include_image_links=include_image_links)
        images = [] if request.image_mode == "none" else image_assets_from_tours(selected, limit_per_tour=1)
        cards = cards_from_tours(selected)
        messages = [] if request.image_mode == "none" else message_blocks_from_tours(client_text, selected)

        return BotResponse(
            status="ok",
            found=bool(selected),
            client_text=client_text,
            tours_count=len(selected),
            search_id=search_id,
            tours=[tour.public_dict() for tour in selected],
            cards=cards,
            images=images,
            messages=messages,
            image_delivery_note=IMAGE_DELIVERY_NOTE if images else None,
        )
    except UserInputError as exc:
        return BotResponse(
            status="ok",
            found=False,
            client_text=str(exc),
            tours_count=0,
            search_id=None,
            image_delivery_note=None,
        )
    except Exception:  # noqa: BLE001 - we return safe text to Suvvy instead of raw stack trace
        logger.exception("Tour search failed")
        return BotResponse(
            status="error",
            found=False,
            client_text=(
                "Не удалось выполнить поиск тура из-за технической ошибки. "
                "Я передам запрос менеджеру, чтобы он проверил варианты вручную."
            ),
            tours_count=0,
            search_id=None,
            image_delivery_note=None,
        )


async def authenticated_tour_search(
    request: TourSearchRequest,
    authorization: str | None,
) -> BotResponse:
    verify_suvvy_token(authorization, request.auth_token)
    return await run_tour_search(request)


def to_short_response(response: BotResponse) -> ShortBotResponse:
    """Return only fields that Suvvy needs.

    Suvvy webhook actions have a maximum response length. The full response
    contains tours/cards/images/messages and can exceed that limit before
    JSONPath extraction happens. Keep /tour-search tiny and put the large
    payload on /tour-search-full for Swagger/debug/future image delivery.
    """
    return ShortBotResponse(
        status=response.status,
        found=response.found,
        client_text=response.client_text,
        tours_count=response.tours_count,
        search_id=response.search_id,
    )


async def authenticated_tour_search_short(
    request: TourSearchRequest,
    authorization: str | None,
) -> ShortBotResponse:
    full_response = await authenticated_tour_search(request, authorization)
    return to_short_response(full_response)


@app.post("/", response_model=ShortBotResponse)
async def suvvy_tour_search_root(
    request: TourSearchRequest,
    authorization: str | None = Header(default=None),
) -> ShortBotResponse:
    """Root webhook alias for platforms/proxies that fail on nested paths."""
    logger.info("Received Suvvy tour-search webhook on root alias /")
    return await authenticated_tour_search_short(request, authorization)


@app.post("/tour-search", response_model=ShortBotResponse)
async def suvvy_tour_search_short(
    request: TourSearchRequest,
    authorization: str | None = Header(default=None),
) -> ShortBotResponse:
    """Short webhook alias."""
    logger.info("Received Suvvy tour-search webhook on /tour-search")
    return await authenticated_tour_search_short(request, authorization)


@app.post("/suvvy", response_model=ShortBotResponse)
async def suvvy_tour_search_suvvy_alias(
    request: TourSearchRequest,
    authorization: str | None = Header(default=None),
) -> ShortBotResponse:
    """Simple webhook alias for Suvvy UI."""
    logger.info("Received Suvvy tour-search webhook on /suvvy")
    return await authenticated_tour_search_short(request, authorization)


@app.post("/api/suvvy/tour-search", response_model=ShortBotResponse)
async def suvvy_tour_search(
    request: TourSearchRequest,
    authorization: str | None = Header(default=None),
) -> ShortBotResponse:
    logger.info("Received Suvvy tour-search webhook on /api/suvvy/tour-search")
    return await authenticated_tour_search_short(request, authorization)


@app.post("/tour-search-full", response_model=BotResponse)
async def suvvy_tour_search_full(
    request: TourSearchRequest,
    authorization: str | None = Header(default=None),
) -> BotResponse:
    """Full payload endpoint for Swagger/debug/future image mapping. Do not use in Suvvy text webhook."""
    logger.info("Received FULL Suvvy tour-search webhook on /tour-search-full")
    return await authenticated_tour_search(request, authorization)


@app.post("/api/suvvy/tour-search-full", response_model=BotResponse)
async def suvvy_tour_search_full_api(
    request: TourSearchRequest,
    authorization: str | None = Header(default=None),
) -> BotResponse:
    """Full payload endpoint for Swagger/debug/future image mapping. Do not use in Suvvy text webhook."""
    logger.info("Received FULL Suvvy tour-search webhook on /api/suvvy/tour-search-full")
    return await authenticated_tour_search(request, authorization)


@app.get("/api/suvvy/tour-search")
async def suvvy_tour_search_diagnostic() -> dict[str, str]:
    return {
        "status": "ok",
        "message": "Use POST with JSON body for tour search.",
        "recommended_suvvy_url": "/ or /tour-search if nested path is blocked by proxy",
    }
