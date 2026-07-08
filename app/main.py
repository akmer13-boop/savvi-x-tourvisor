import logging

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.formatting import format_tours_for_client
from app.models import BotResponse, TourSearchRequest
from app.ranking import select_best_tours
from app.tourvisor_client import TourvisorClient, UserInputError

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Suvvy ↔ Tourvisor Bridge",
    version="0.1.0",
    description="MVP service that receives tour parameters from Suvvy and returns 3–5 preliminary Tourvisor options.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def verify_suvvy_token(authorization: str | None = Header(default=None)) -> None:
    if not settings.suvvy_webhook_token:
        return

    expected = f"Bearer {settings.suvvy_webhook_token}"
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization token",
        )



@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "suvvy-tourvisor-bridge", "status": "ok"}


@app.get("/ping")
async def ping() -> dict[str, str]:
    return {"pong": "ok"}

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/suvvy/tour-search", response_model=BotResponse)
async def suvvy_tour_search(
    request: TourSearchRequest,
    _: None = Depends(verify_suvvy_token),
) -> BotResponse:
    try:
        client = TourvisorClient()
        search_id, tours = await client.search_tours(request)
        selected = select_best_tours(tours, request, limit=5)
        client_text = format_tours_for_client(selected, request)

        return BotResponse(
            status="ok",
            found=bool(selected),
            client_text=client_text,
            tours_count=len(selected),
            search_id=search_id,
        )
    except HTTPException:
        raise
    except UserInputError as exc:
        return BotResponse(
            status="ok",
            found=False,
            client_text=str(exc),
            tours_count=0,
            search_id=None,
        )
    except Exception as exc:  # noqa: BLE001 - we return safe text to Suvvy instead of raw stack trace
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
        )
