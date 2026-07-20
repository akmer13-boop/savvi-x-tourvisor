from __future__ import annotations

from datetime import date

from app.config import settings
from app.models import TourSearchRequest


class SearchInputError(ValueError):
    """A client-visible validation problem that must not call Tourvisor."""

    def __init__(self, reason: str, client_text: str) -> None:
        super().__init__(client_text)
        self.reason = reason
        self.client_text = client_text


def normalize_budget_rub(value: int | None) -> int:
    """Normalize the total package budget and reject ambiguous ruble values."""
    if value is None or value <= 0:
        raise SearchInputError(
            "INVALID_BUDGET",
            "Уточните общий бюджет путёвки в рублях.",
        )
    if 50 <= value <= 999:
        return value * 1_000
    if value >= 50_000:
        return value
    raise SearchInputError(
        "INVALID_BUDGET",
        "Уточните общий бюджет путёвки в рублях, например 300 000 или 500 000.",
    )


def _parse_iso_date(value: str | None, field_name: str) -> date:
    if not value:
        raise SearchInputError(
            "INVALID_DATES",
            "Уточните даты возможного вылета.",
        )
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SearchInputError(
            "INVALID_DATES",
            f"Поле {field_name} должно содержать дату в формате YYYY-MM-DD.",
        ) from exc


def validate_and_normalize_search_request(request: TourSearchRequest) -> TourSearchRequest:
    """Return the canonical request that is safe to send to Tourvisor."""
    date_from = _parse_iso_date(request.date_from, "date_from")
    date_to = _parse_iso_date(request.date_to, "date_to")
    departure_span_days = (date_to - date_from).days
    if departure_span_days < 0:
        raise SearchInputError(
            "INVALID_DATES",
            "Конец диапазона вылета не может быть раньше его начала.",
        )
    if date_from < date.today():
        raise SearchInputError(
            "INVALID_DATES",
            "Дата начала вылета не может быть в прошлом.",
        )
    if departure_span_days + 1 > settings.max_departure_window_days:
        raise SearchInputError(
            "INVALID_DATES",
            "Уточните дату или диапазон вылета продолжительностью не более "
            f"{settings.max_departure_window_days} календарных дней.",
        )

    if request.nights_from is None or request.nights_to is None:
        raise SearchInputError(
            "INVALID_NIGHTS",
            "Уточните продолжительность поездки.",
        )
    if request.nights_from > request.nights_to:
        raise SearchInputError(
            "INVALID_NIGHTS",
            "Минимальное количество ночей не может превышать максимальное.",
        )
    if request.nights_to - request.nights_from > settings.max_nights_range:
        raise SearchInputError(
            "INVALID_NIGHTS",
            "Диапазон продолжительности должен быть не шире "
            f"{settings.max_nights_range} ночей.",
        )

    if request.children == 0 and request.children_ages:
        raise SearchInputError(
            "INVALID_TRAVELLERS",
            "Уточните количество детей.",
        )
    if request.children > 0 and len(request.children_ages) != request.children:
        raise SearchInputError(
            "CHILD_AGES_REQUIRED",
            "Укажите, пожалуйста, возраст каждого ребёнка.",
        )
    if any(age < 0 or age > 17 for age in request.children_ages):
        raise SearchInputError(
            "INVALID_TRAVELLERS",
            "Возраст каждого ребёнка должен быть указан полным числом лет от 0 до 17.",
        )

    budget = normalize_budget_rub(request.budget)
    return request.model_copy(
        update={
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "budget": budget,
        }
    )


def unverified_preferences(request: TourSearchRequest) -> list[str]:
    """Preserve optional free-text wishes until Tourvisor fields are verified."""
    values = [request.hotel_preferences, request.beach_preferences]
    return [value for value in values if value]
