import asyncio
import logging
import re
import unicodedata
import uuid
from typing import Any

import httpx

from app.config import settings
from app.media import absolute_url, normalize_tour_media
from app.models import TourOption, TourSearchRequest

logger = logging.getLogger(__name__)


class UserInputError(ValueError):
    """The request is missing data required for Tourvisor search."""


class TourvisorClient:
    """Client for Tourvisor Search API.

    Flow according to Tourvisor docs:
    1. Resolve names to dictionary IDs: departures, countries, regions, meals.
    2. Start async tour search: GET /search/api/v1/tours/search -> searchId.
    3. Poll status briefly.
    4. Read current results: GET /search/api/v1/tours/search/{searchId}.
    """

    def __init__(self) -> None:
        self.base_url = settings.tourvisor_api_base_url.rstrip("/")
        self.jwt = settings.effective_tourvisor_jwt
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.jwt}",
        }

    async def search_tours(self, request: TourSearchRequest) -> tuple[str, list[TourOption]]:
        if settings.mock_tourvisor:
            return str(uuid.uuid4()), self._mock_tours(request)

        self._validate_config()
        self._validate_request(request)

        async with httpx.AsyncClient(timeout=settings.tourvisor_timeout_seconds) as client:
            departure = await self._resolve_departure(client, request.departure_city)
            country = await self._resolve_country(client, request.country, departure["id"])
            region_id = None
            if request.resort:
                region = await self._resolve_region(client, request.resort, country["id"])
                region_id = region["id"] if region else None

            meal_id = None
            if request.meal:
                meal = await self._resolve_meal(client, request.meal)
                meal_id = meal["id"] if meal else None

            search_params = self._build_search_params(
                request=request,
                departure_id=departure["id"],
                country_id=country["id"],
                region_id=region_id,
                meal_id=meal_id,
            )

            search_response = await self._get(client, "/search/api/v1/tours/search", params=search_params)
            search_id = str(search_response.get("searchId") or "")
            if not search_id:
                raise RuntimeError(f"Tourvisor did not return searchId: {search_response}")

            await self._wait_for_results(client, search_id)
            results = await self._get(
                client,
                f"/search/api/v1/tours/search/{search_id}",
                params={"limit": settings.tourvisor_results_limit},
            )

        tours = self._parse_search_results(results, request)
        return search_id, tours

    async def enrich_tours_with_room_details(self, tours: list[TourOption]) -> list[TourOption]:
        """Attach room details/images by roomId.

        Tourvisor docs: GET /search/api/v1/rooms accepts room IDs from search results
        and returns room descriptions plus images. This API section may be paid separately;
        if it is unavailable, we keep tours without images instead of breaking search.
        """
        if settings.mock_tourvisor or not settings.tourvisor_enable_room_images:
            return tours

        room_ids = sorted({tour.room_id for tour in tours if tour.room_id})
        if not room_ids:
            return tours

        try:
            async with httpx.AsyncClient(timeout=settings.tourvisor_timeout_seconds) as client:
                rooms = await self._get(client, "/search/api/v1/rooms", params={"ids": room_ids[:30]})
        except Exception:
            logger.exception("Unable to enrich tours with room details/images")
            return tours

        if not isinstance(rooms, list):
            return tours

        room_by_id = {_to_int(room.get("id")): room for room in rooms if isinstance(room, dict)}
        image_limit = max(settings.tourvisor_room_images_limit, 0)
        for tour in tours:
            room = room_by_id.get(tour.room_id)
            if not room:
                continue
            images = room.get("images") or []
            if isinstance(images, list):
                tour.room_images = [url for url in (absolute_url(str(url)) for url in images if url) if url][:image_limit]
            tour.room_description = room.get("description") or room.get("comment")
            tour.room_area = _to_int(room.get("area"))
            tour.room_sleeping_places = room.get("sleepingPlaces")
            tour.room_view_description = room.get("viewDescription")
            if not tour.room and room.get("name"):
                tour.room = str(room.get("name"))
        return tours

    def _validate_config(self) -> None:
        if not self.jwt:
            raise RuntimeError("TOURVISOR_JWT is not configured")
        if not self.base_url:
            raise RuntimeError("TOURVISOR_API_BASE_URL is not configured")

    def _validate_request(self, request: TourSearchRequest) -> None:
        missing: list[str] = []
        if not request.date_from:
            missing.append("дату начала вылета")
        if not request.date_to:
            missing.append("дату окончания диапазона")
        if not request.nights_from:
            missing.append("количество ночей от")
        if not request.nights_to:
            missing.append("количество ночей до")
        if request.children and len(request.children_ages) < request.children:
            missing.append("возраст каждого ребёнка")

        if missing:
            raise UserInputError("Для поиска нужно уточнить: " + ", ".join(missing) + ".")

        if request.nights_from and request.nights_to and request.nights_to - request.nights_from > 10:
            raise UserInputError("Диапазон ночей в Tourvisor должен быть не больше 10. Уточните более узкий диапазон.")

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        response = await client.get(url, params=self._clean_params(params or {}), headers=self.headers)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:1000]
            logger.error("Tourvisor API error %s for %s: %s", exc.response.status_code, path, body)
            raise RuntimeError(f"Tourvisor API error {exc.response.status_code}") from exc
        return response.json()

    @staticmethod
    def _clean_params(params: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in params.items() if value not in (None, "", [])}

    async def _resolve_departure(self, client: httpx.AsyncClient, name: str) -> dict[str, Any]:
        items = await self._get(client, "/search/api/v1/departures")
        found = _find_by_name(items, name)
        if not found:
            raise UserInputError(f"Не нашёл город вылета «{name}» в справочнике Tourvisor. Уточните город вылета.")
        return found

    async def _resolve_country(self, client: httpx.AsyncClient, name: str, departure_id: int) -> dict[str, Any]:
        items = await self._get(
            client,
            "/search/api/v1/countries",
            params={"departureId": departure_id, "onlyCharter": False, "onlyDirect": False},
        )
        found = _find_by_name(items, name)
        if not found:
            raise UserInputError(f"Не нашёл направление «{name}» для выбранного города вылета. Уточните страну.")
        return found

    async def _resolve_region(self, client: httpx.AsyncClient, name: str, country_id: int) -> dict[str, Any] | None:
        items = await self._get(client, "/search/api/v1/regions", params={"countryId": country_id})
        found = _find_by_name(items, name)
        if not found:
            logger.info("Region not found in Tourvisor dictionary: %s", name)
        return found

    async def _resolve_meal(self, client: httpx.AsyncClient, name: str) -> dict[str, Any] | None:
        items = await self._get(client, "/search/api/v1/meals")
        found = _find_meal(items, name)
        if not found:
            logger.info("Meal not found in Tourvisor dictionary: %s", name)
        return found

    def _build_search_params(
        self,
        request: TourSearchRequest,
        departure_id: int,
        country_id: int,
        region_id: int | None,
        meal_id: int | None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "departureId": departure_id,
            "countryId": country_id,
            "dateFrom": request.date_from,
            "dateTo": request.date_to,
            "nightsFrom": request.nights_from,
            "nightsTo": request.nights_to,
            "adults": request.adults,
            "currency": settings.tourvisor_currency,
            "onlyCharter": False,
            "onlyDirect": False,
            "priceTo": request.budget,
            "hotelCategory": request.hotel_stars,
            "meal": meal_id,
        }

        if request.children_ages:
            params["childs"] = request.children_ages[:3]
        if region_id:
            params["regionIds"] = [region_id]
        return params

    async def _wait_for_results(self, client: httpx.AsyncClient, search_id: str) -> None:
        attempts = max(settings.tourvisor_poll_attempts, 1)
        interval = max(settings.tourvisor_poll_interval_seconds, 0)
        for attempt in range(attempts):
            if interval:
                await asyncio.sleep(interval)
            try:
                status_data = await self._get(
                    client,
                    f"/search/api/v1/tours/search/{search_id}/status",
                    params={"operatorStatus": False},
                )
            except Exception:  # noqa: BLE001 - results endpoint may still have partial data
                logger.exception("Unable to read Tourvisor search status")
                continue

            progress = int(status_data.get("progress") or 0)
            status = str(status_data.get("status") or "").lower()
            logger.info("Tourvisor search %s status=%s progress=%s attempt=%s", search_id, status, progress, attempt + 1)
            if progress >= 100 or status in {"done", "complete", "completed", "finished", "finish"}:
                break

    def _parse_search_results(self, data: Any, request: TourSearchRequest) -> list[TourOption]:
        if not isinstance(data, list):
            logger.warning("Unexpected Tourvisor results format: %s", type(data).__name__)
            return []

        tours: list[TourOption] = []
        seen_hotels: set[int] = set()

        for hotel in data:
            if not isinstance(hotel, dict):
                continue
            hotel_tours = hotel.get("tours") or []
            if not isinstance(hotel_tours, list):
                continue

            # For chatbot output we usually need one best/cheapest tour per hotel.
            hotel_tours = sorted(hotel_tours, key=lambda item: _to_int(item.get("price")) or 10**12)
            for tour in hotel_tours[:1]:
                if not isinstance(tour, dict):
                    continue
                hotel_id = _to_int(hotel.get("id"))
                if hotel_id and hotel_id in seen_hotels:
                    continue
                if hotel_id:
                    seen_hotels.add(hotel_id)
                option = self._parse_tour_option(hotel, tour, request)
                normalize_tour_media(option)
                tours.append(option)

        return tours

    def _parse_tour_option(self, hotel: dict[str, Any], tour: dict[str, Any], request: TourSearchRequest) -> TourOption:
        country = _nested_name(hotel.get("country")) or request.country
        region = _nested_name(hotel.get("region")) or request.resort
        sub_region = _nested_name(hotel.get("subRegion"))
        meal = tour.get("meal") or {}
        operator = tour.get("operator") or {}

        return TourOption(
            country=country,
            resort=sub_region or region,
            hotel=str(hotel.get("name") or "Отель"),
            stars=_to_int(hotel.get("category")),
            meal=_meal_text(meal),
            departure_city=request.departure_city,
            fly_date=tour.get("date"),
            nights=_to_int(tour.get("nights")),
            adults=_to_int(tour.get("adults")),
            children=_to_int(tour.get("childs")),
            price=_to_int(tour.get("price") or hotel.get("price")),
            currency=str(tour.get("currency") or hotel.get("currency") or settings.tourvisor_currency),
            operator=_operator_text(operator),
            room=tour.get("roomType") or tour.get("name") or tour.get("placement"),
            link=absolute_url(hotel.get("hotelDescriptionLink") or settings.tourvisor_public_search_url or None),
            rating=_to_float(hotel.get("rating")),
            hotel_id=_to_int(hotel.get("id")),
            tour_id=str(tour.get("id")) if tour.get("id") is not None else None,
            room_id=_to_int(tour.get("roomId")),
            tour_picture=absolute_url(tour.get("picture") or hotel.get("picture")),
            raw={"hotel": hotel, "tour": tour},
        )

    def _mock_tours(self, request: TourSearchRequest) -> list[TourOption]:
        country = request.country
        departure = request.departure_city
        budget = request.budget or 250000
        nights = request.nights_from or request.nights_to or 7
        adults = request.adults
        children = request.children

        return [
            TourOption(
                country=country,
                resort=request.resort or "Сиде",
                hotel="Family Resort",
                stars=max(request.hotel_stars or 5, 4),
                meal=request.meal or "Всё включено",
                departure_city=departure,
                fly_date=request.date_from or "ближайшая подходящая дата",
                nights=nights,
                adults=adults,
                children=children,
                price=int(budget * 0.95),
                operator="Demo Operator",
                room="Standard Room",
                rating=4.6,
                link=settings.tourvisor_public_search_url or None,
            ),
            TourOption(
                country=country,
                resort=request.resort or "Аланья",
                hotel="Sea View Hotel",
                stars=max(request.hotel_stars or 4, 4),
                meal=request.meal or "Всё включено",
                departure_city=departure,
                fly_date=request.date_from or "ближайшая подходящая дата",
                nights=nights + 1,
                adults=adults,
                children=children,
                price=int(budget * 0.88),
                operator="Demo Operator",
                room="Promo Room",
                rating=4.3,
                link=settings.tourvisor_public_search_url or None,
            ),
            TourOption(
                country=country,
                resort=request.resort or "Белек",
                hotel="Premium Beach Resort",
                stars=5,
                meal=request.meal or "Ультра всё включено",
                departure_city=departure,
                fly_date=request.date_to or request.date_from or "ближайшая подходящая дата",
                nights=nights,
                adults=adults,
                children=children,
                price=int(budget * 1.05),
                operator="Demo Operator",
                room="Family Room",
                rating=4.8,
                link=settings.tourvisor_public_search_url or None,
            ),
        ]


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

    candidates: list[tuple[int, dict[str, Any]]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        names = [item.get("name"), item.get("russianName"), item.get("fullName"), item.get("fullRussianName")]
        norms = [_normalize(name) for name in names if name]
        if target_norm in norms:
            return item
        for norm in norms:
            if not norm:
                continue
            if target_norm in norm or norm in target_norm:
                candidates.append((abs(len(norm) - len(target_norm)), item))

    if candidates:
        return sorted(candidates, key=lambda pair: pair[0])[0][1]
    return None


def _find_meal(items: Any, target: str) -> dict[str, Any] | None:
    target_norm = _normalize(target)
    synonym_map = {
        "all inclusive": "все включено",
        "ultra all inclusive": "ультра все включено",
        "ai": "все включено",
        "uai": "ультра все включено",
        "завтрак": "завтрак",
        "завтраки": "завтрак",
        "полупансион": "полупансион",
        "пансион": "пансион",
    }
    target_norm = _normalize(synonym_map.get(target_norm, target_norm))
    return _find_by_name(items, target_norm)


def _nested_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("name") or value.get("russianName") or value.get("fullRussianName")
    if isinstance(value, str):
        return value
    return None


def _meal_text(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("fullRussianName") or value.get("russianName") or value.get("fullName") or value.get("name")
    if isinstance(value, str):
        return value
    return None


def _operator_text(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("russianName") or value.get("fullName") or value.get("name")
    if isinstance(value, str):
        return value
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
