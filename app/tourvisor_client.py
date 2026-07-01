import uuid
from typing import Any

import httpx

from app.config import settings
from app.models import TourOption, TourSearchRequest


class TourvisorClient:
    """
    Adapter for Tourvisor DDAPI.

    Public Tourvisor pages confirm the availability of DDAPI/search API, but the exact
    search method, parameter names and dictionaries are usually issued with access.
    Therefore this MVP has MOCK mode plus a single place where real mapping is added.
    """

    async def search_tours(self, request: TourSearchRequest) -> tuple[str, list[TourOption]]:
        search_id = str(uuid.uuid4())
        if settings.mock_tourvisor:
            return search_id, self._mock_tours(request)

        if not settings.tourvisor_search_url or not settings.tourvisor_api_key:
            raise RuntimeError("Tourvisor API is not configured")

        payload = self._build_tourvisor_payload(request)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            # Change after receiving exact Tourvisor auth requirements.
            "Authorization": f"Bearer {settings.tourvisor_api_key}",
        }

        async with httpx.AsyncClient(timeout=settings.tourvisor_timeout_seconds) as client:
            response = await client.post(settings.tourvisor_search_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        return search_id, self._parse_tourvisor_response(data)

    def _build_tourvisor_payload(self, request: TourSearchRequest) -> dict[str, Any]:
        """
        Map Suvvy request fields to Tourvisor API fields.
        Replace names/IDs after receiving official Tourvisor DDAPI docs.
        """
        return {
            "departure": request.departure_city,
            "country": request.country,
            "resort": request.resort,
            "date_from": request.date_from,
            "date_to": request.date_to,
            "nights_from": request.nights_from,
            "nights_to": request.nights_to,
            "adults": request.adults,
            "children": request.children,
            "children_ages": request.children_ages,
            "budget": request.budget,
            "meal": request.meal,
            "stars": request.hotel_stars,
        }

    def _parse_tourvisor_response(self, data: dict[str, Any]) -> list[TourOption]:
        """
        Flexible parser for first integration tests.
        Adjust this to Tourvisor's real response schema after docs/access are issued.
        """
        candidates: Any = data
        for key in ("tours", "items", "results", "data"):
            if isinstance(candidates, dict) and key in candidates:
                candidates = candidates[key]
                break

        if not isinstance(candidates, list):
            return []

        tours: list[TourOption] = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            tours.append(
                TourOption(
                    country=str(item.get("country") or item.get("countryName") or ""),
                    resort=item.get("resort") or item.get("region") or item.get("resortName"),
                    hotel=str(item.get("hotel") or item.get("hotelName") or "Отель"),
                    stars=_to_int(item.get("stars") or item.get("hotelStars")),
                    meal=item.get("meal") or item.get("mealName"),
                    departure_city=item.get("departure") or item.get("departureCity"),
                    fly_date=item.get("flydate") or item.get("flyDate") or item.get("date"),
                    nights=_to_int(item.get("nights")),
                    adults=_to_int(item.get("adults")),
                    children=_to_int(item.get("children")),
                    price=_to_int(item.get("price") or item.get("totalPrice")),
                    currency=item.get("currency") or "RUB",
                    operator=item.get("operator") or item.get("operatorName"),
                    room=item.get("room") or item.get("roomName"),
                    link=item.get("link") or item.get("url") or item.get("operatorlink"),
                    rating=_to_float(item.get("rating")),
                    raw=item,
                )
            )
        return tours

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
