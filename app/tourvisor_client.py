import asyncio
import logging
import re
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.budget import BudgetPolicy
from app.config import settings
from app.media import absolute_url, normalize_tour_media
from app.models import TourOption, TourSearchRequest
from app.observability import get_request_id
from app.operator_policy import OperatorPolicy, OperatorPolicyConfigurationError
from app.runtime import operator_policy
from app.validation import SearchInputError, validate_and_normalize_search_request

logger = logging.getLogger(__name__)


class UserInputError(SearchInputError):
    """The request is missing data required for Tourvisor search."""

    def __init__(self, client_text: str, reason: str = "NEEDS_CLARIFICATION") -> None:
        super().__init__(reason, client_text)


class TourvisorContractConfigurationError(OperatorPolicyConfigurationError):
    """A requested search mode is not enabled for the verified API contract."""


class TourvisorClient:
    """Client for Tourvisor Search API.

    Flow according to Tourvisor docs:
    1. Resolve names to dictionary IDs: departures, countries, regions, meals.
    2. Start async tour search: GET /search/api/v1/tours/search -> searchId.
    3. Poll status briefly.
    4. Read current results: GET /search/api/v1/tours/search/{searchId}.
    """

    def __init__(self, policy: OperatorPolicy | None = None) -> None:
        self.base_url = settings.tourvisor_api_base_url.rstrip("/")
        self.jwt = settings.effective_tourvisor_jwt
        self.operator_policy = policy or operator_policy
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.jwt}",
        }

    async def search_tours(
        self,
        request: TourSearchRequest,
        *,
        before_dispatch: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[str, list[TourOption]]:
        request = validate_and_normalize_search_request(request)
        if settings.mock_tourvisor:
            if before_dispatch is not None:
                await before_dispatch()
            return str(uuid.uuid4()), self._mock_tours(request)

        self._validate_config()
        self._validate_budget_contract(request)

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

            logger.info(
                "TOURVISOR_SEARCH_CONTRACT request_id=%s contract_version=%s "
                "has_price_from=%s has_price_to=%s operator_count=%s",
                get_request_id(),
                settings.tourvisor_api_contract_version,
                "priceFrom" in search_params,
                "priceTo" in search_params,
                len(search_params.get("operatorIds") or []),
            )
            if before_dispatch is not None:
                await before_dispatch()
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

    async def enrich_tours_with_hotel_details(self, tours: list[TourOption]) -> list[TourOption]:
        """Attach the official Tourvisor hotel cover image.

        Tourvisor docs: GET /search/api/v1/hotels/{hotelId} returns the hotel
        description and an ordered ``images`` array. This method belongs to the
        separately paid Hotel Descriptions API. The first image is treated as
        the selling/cover photo. If access is unavailable, the search remains
        successful and the response falls back to room photos.
        """
        if settings.mock_tourvisor or not settings.tourvisor_enable_hotel_images:
            return tours

        hotel_ids = sorted({tour.hotel_id for tour in tours if tour.hotel_id})
        if not hotel_ids:
            return tours

        async def fetch_one(client: httpx.AsyncClient, hotel_id: int) -> tuple[int, Any | None]:
            path = f"/search/api/v1/hotels/{hotel_id}"
            url = f"{self.base_url}{path}"
            try:
                response = await client.get(url, headers=self.headers)
                if response.status_code in {401, 402, 403}:
                    logger.warning(
                        "Hotel Descriptions API is unavailable for hotel_id=%s: HTTP %s. "
                        "Enable the paid Tourvisor Hotel Descriptions API to receive selling photos.",
                        hotel_id,
                        response.status_code,
                    )
                    return hotel_id, None
                response.raise_for_status()
                return hotel_id, response.json()
            except Exception:  # noqa: BLE001 - media enrichment must never break search
                logger.exception("Unable to fetch Tourvisor hotel description for hotel_id=%s", hotel_id)
                return hotel_id, None

        async with httpx.AsyncClient(timeout=settings.tourvisor_timeout_seconds) as client:
            payloads = await asyncio.gather(*(fetch_one(client, hotel_id) for hotel_id in hotel_ids))

        payload_by_id = dict(payloads)
        image_limit = max(settings.tourvisor_hotel_images_limit, 1)
        for tour in tours:
            if not tour.hotel_id:
                continue
            images = _extract_hotel_images(payload_by_id.get(tour.hotel_id))
            if images:
                # The first image in the official description is the main/cover photo.
                tour.tour_picture = images[:image_limit][0]
        return tours

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
        if not self.operator_policy.enforced or not self.operator_policy.active_ids:
            raise OperatorPolicyConfigurationError(
                "A non-empty active_contract operator policy is required"
            )

    @staticmethod
    def _validate_budget_contract(request: TourSearchRequest) -> None:
        budget_policy = BudgetPolicy.from_request(request)
        contract_version = settings.tourvisor_api_contract_version.strip().lower()
        contract_verified = contract_version not in {"", "unknown", "unverified"}
        if (
            budget_policy.budget_type in {"min", "approx", "range"}
            and (not settings.tourvisor_price_from_enabled or not contract_verified)
        ):
            raise TourvisorContractConfigurationError(
                "The requested budget mode is disabled until the Tourvisor price contract is verified"
            )

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        response = await client.get(url, params=self._clean_params(params or {}), headers=self.headers)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Tourvisor API error request_id=%s status=%s path=%s",
                get_request_id(),
                exc.response.status_code,
                path,
            )
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

    async def _resolve_region(self, client: httpx.AsyncClient, name: str, country_id: int) -> dict[str, Any]:
        items = await self._get(client, "/search/api/v1/regions", params={"countryId": country_id})
        found = _find_by_name(items, name)
        if not found:
            logger.info("Tourvisor region dictionary lookup returned no match")
            raise UserInputError(
                "Не удалось однозначно определить курорт. Уточните название курорта.",
                reason="REGION_NOT_FOUND",
            )
        return found

    async def _resolve_meal(self, client: httpx.AsyncClient, name: str) -> dict[str, Any]:
        items = await self._get(client, "/search/api/v1/meals")
        found = _find_meal(items, name)
        if not found:
            logger.info("Tourvisor meal dictionary lookup returned no match")
            raise UserInputError(
                "Не удалось однозначно определить тип питания. Уточните питание.",
                reason="MEAL_NOT_FOUND",
            )
        return found

    def _build_search_params(
        self,
        request: TourSearchRequest,
        departure_id: int,
        country_id: int,
        region_id: int | None,
        meal_id: int | None,
    ) -> dict[str, Any]:
        budget_policy = BudgetPolicy.from_request(request)
        self._validate_budget_contract(request)

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
            "priceFrom": budget_policy.price_from,
            "priceTo": budget_policy.price_to,
            "hotelCategory": request.hotel_stars,
            "hotelRating": 4,
            "meal": meal_id,
        }

        if self.operator_policy.enforced:
            if not self.operator_policy.active_ids:
                raise OperatorPolicyConfigurationError(
                    "The active_contract operator list is empty"
                )
            # httpx serializes list values as repeated query parameters. A
            # contract test locks this down until the live Tourvisor contract
            # can be verified with an explicitly approved request.
            params["operatorIds"] = sorted(self.operator_policy.active_ids)

        if request.children_ages:
            params["childs"] = request.children_ages
        if region_id:
            params["regionIds"] = [region_id]
        return self._clean_params(params)

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
            except Exception as exc:  # noqa: BLE001 - partial results may still exist
                logger.warning(
                    "Unable to read Tourvisor search status request_id=%s error_type=%s",
                    get_request_id(),
                    type(exc).__name__,
                )
                continue

            progress = int(status_data.get("progress") or 0)
            status = str(status_data.get("status") or "").lower()
            logger.info(
                "TOURVISOR_STATUS request_id=%s search_id=%s status=%s progress=%s attempt=%s",
                get_request_id(),
                search_id,
                status,
                progress,
                attempt + 1,
            )
            if progress >= 100 or status in {"done", "complete", "completed", "finished", "finish"}:
                break

    def _parse_search_results(self, data: Any, request: TourSearchRequest) -> list[TourOption]:
        if not isinstance(data, list):
            logger.warning("Unexpected Tourvisor results format: %s", type(data).__name__)
            return []

        tours: list[TourOption] = []
        seen_hotels: set[int] = set()
        dropped_disallowed = 0
        dropped_budget = 0
        budget_policy = BudgetPolicy.from_request(request)

        for hotel in data:
            if not isinstance(hotel, dict):
                continue
            hotel_tours = hotel.get("tours") or []
            if not isinstance(hotel_tours, list):
                continue

            # Filter every room by mandatory policy before choosing one option
            # per hotel. Otherwise an inadmissible cheap room could hide an
            # eligible room from min/range/approx searches.
            hotel_rating = _to_float(hotel.get("rating"))
            if hotel_rating is None or hotel_rating < settings.tourvisor_min_hotel_rating:
                continue

            allowed_hotel_tours: list[tuple[dict[str, Any], int]] = []
            for tour in hotel_tours:
                if not isinstance(tour, dict):
                    continue
                operator = tour.get("operator") or {}
                operator_id = _to_int(operator.get("id")) if isinstance(operator, dict) else None
                if self.operator_policy.enforced and operator_id not in self.operator_policy.active_ids:
                    dropped_disallowed += 1
                    continue

                price = _effective_tour_price(hotel, tour)
                if not budget_policy.allows(price):
                    dropped_budget += 1
                    continue
                allowed_hotel_tours.append((tour, price))

            if not allowed_hotel_tours:
                continue

            hotel_id = _to_int(hotel.get("id"))
            if hotel_id and hotel_id in seen_hotels:
                continue
            if hotel_id:
                seen_hotels.add(hotel_id)

            selected_tour, _ = max(
                allowed_hotel_tours,
                key=lambda item: budget_policy.hotel_choice_key(item[1]),
            )
            option = self._parse_tour_option(hotel, selected_tour, request)
            normalize_tour_media(option)
            tours.append(option)

        logger.info(
            "OPERATOR_FILTER request_id=%s policy_version=%s policy_hash=%s "
            "allowed_count=%s dropped_disallowed=%s dropped_budget=%s accepted=%s",
            get_request_id(),
            self.operator_policy.version,
            self.operator_policy.short_hash,
            self.operator_policy.active_count,
            dropped_disallowed,
            dropped_budget,
            len(tours),
        )
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
            price=_effective_tour_price(hotel, tour),
            currency=str(tour.get("currency") or hotel.get("currency") or settings.tourvisor_currency),
            operator=_operator_text(operator),
            operator_id=_to_int(operator.get("id")) if isinstance(operator, dict) else None,
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
        budget_policy = BudgetPolicy.from_request(request)
        if budget_policy.budget_type == "min":
            floor = budget_policy.price_from or 250_000
            prices = (floor, int(floor * 1.1), int(floor * 1.2))
        elif budget_policy.budget_type in {"approx", "range"}:
            floor = budget_policy.price_from or 0
            ceiling = budget_policy.price_to or floor
            prices = (floor, (floor + ceiling) // 2, ceiling)
        elif budget_policy.budget_type == "unknown":
            prices = (220_000, 250_000, 280_000)
        else:
            ceiling = budget_policy.price_to or 250_000
            prices = (int(ceiling * 0.95), int(ceiling * 0.88), int(ceiling * 1.05))
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
                price=prices[0],
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
                price=prices[1],
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
                price=prices[2],
                operator="Demo Operator",
                room="Family Room",
                rating=4.8,
                link=settings.tourvisor_public_search_url or None,
            ),
        ]


def _extract_hotel_images(payload: Any) -> list[str]:
    """Read hotel images from both documented and defensive response shapes."""
    item: Any = payload
    if isinstance(payload, list):
        item = payload[0] if payload else None
    if not isinstance(item, dict):
        return []

    raw_images = item.get("images") or []
    if not isinstance(raw_images, list):
        return []

    result: list[str] = []
    seen: set[str] = set()
    for image in raw_images:
        value: Any = image
        if isinstance(image, dict):
            value = image.get("url") or image.get("image") or image.get("src")
        url = absolute_url(str(value)) if value else None
        if url and url not in seen:
            result.append(url)
            seen.add(url)
    return result


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


def _effective_tour_price(hotel: dict[str, Any], tour: dict[str, Any]) -> int | None:
    price = _to_int(tour.get("price"))
    if price is not None and price > 0:
        return price
    hotel_price = _to_int(hotel.get("price"))
    return hotel_price if hotel_price is not None and hotel_price > 0 else None


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
