import logging

from fastapi import FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.formatting import format_tours_for_client
from app.media import cards_from_tours, image_assets_from_tours, message_blocks_from_tours, normalize_tour_media
from app.models import BotResponse, TourSearchRequest
from app.ranking import select_best_tours
from app.tourvisor_client import TourvisorClient, UserInputError

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Suvvy ↔ Tourvisor Bridge",
    version="0.2.1",
    description=(
        "Service that receives tour parameters from Suvvy, searches Tourvisor, "
        "and returns clean text plus structured cards/images for user-friendly delivery."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


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
    return {"service": "suvvy-tourvisor-bridge", "status": "ok", "version": "0.2.1"}


@app.get("/ping")
async def ping() -> dict[str, str]:
    return {"pong": "ok"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


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


@app.post("/", response_model=BotResponse)
async def suvvy_tour_search_root(
    request: TourSearchRequest,
    authorization: str | None = Header(default=None),
) -> BotResponse:
    """Root webhook alias for platforms/proxies that fail on nested paths."""
    logger.info("Received Suvvy tour-search webhook on root alias /")
    return await authenticated_tour_search(request, authorization)


@app.post("/tour-search", response_model=BotResponse)
async def suvvy_tour_search_short(
    request: TourSearchRequest,
    authorization: str | None = Header(default=None),
) -> BotResponse:
    """Short webhook alias."""
    logger.info("Received Suvvy tour-search webhook on /tour-search")
    return await authenticated_tour_search(request, authorization)


@app.post("/suvvy", response_model=BotResponse)
async def suvvy_tour_search_suvvy_alias(
    request: TourSearchRequest,
    authorization: str | None = Header(default=None),
) -> BotResponse:
    """Simple webhook alias for Suvvy UI."""
    logger.info("Received Suvvy tour-search webhook on /suvvy")
    return await authenticated_tour_search(request, authorization)


@app.post("/api/suvvy/tour-search", response_model=BotResponse)
async def suvvy_tour_search(
    request: TourSearchRequest,
    authorization: str | None = Header(default=None),
) -> BotResponse:
    logger.info("Received Suvvy tour-search webhook on /api/suvvy/tour-search")
    return await authenticated_tour_search(request, authorization)


@app.get("/api/suvvy/tour-search")
async def suvvy_tour_search_diagnostic() -> dict[str, str]:
    return {
        "status": "ok",
        "message": "Use POST with JSON body for tour search.",
        "recommended_suvvy_url": "/ or /tour-search if nested path is blocked by proxy",
    }
