from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.budget import BudgetPolicy, SearchInputError
from app.budget import normalize_budget_rub as _normalize_budget_rub
from app.config import settings
from app.models import TourSearchRequest


MANAGER_REQUIRED_TEXT = (
    "Для этого запроса потребуется помощь менеджера. "
    "Я зафиксировала Ваш запрос — менеджер свяжется с Вами в ближайшее время."
)


class ManagerRoutingRequired(ValueError):
    """A valid request that must be handled by a human without Tourvisor."""

    def __init__(self, reason: str, client_text: str = MANAGER_REQUIRED_TEXT) -> None:
        super().__init__(client_text)
        self.reason = reason
        self.client_text = client_text


def _normalized_words(value: str | None) -> set[str]:
    normalized = unicodedata.normalize("NFKC", str(value or "").lower().replace("ё", "е"))
    return set(re.sub(r"[^a-zа-я0-9]+", " ", normalized).split())


def _validate_blocked_destination(request: TourSearchRequest) -> None:
    destination_words = _normalized_words(request.country) | _normalized_words(
        request.resort
    )
    if destination_words & {
        "китай",
        "китайская",
        "кнр",
        "china",
        "chinese",
        "prc",
        "cn",
        "chn",
        "kitai",
        "kitay",
        "zhongguo",
        "хайнань",
        "hainan",
        "санья",
        "sanya",
        "sania",
    }:
        raise ManagerRoutingRequired("DESTINATION_BLOCKED")


def normalize_budget_rub(value: int | None) -> int:
    """Backward-compatible import path for callers of the 0.4 contract."""
    return _normalize_budget_rub(value)


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


def _business_today() -> date:
    return datetime.now(ZoneInfo(settings.business_timezone)).date()


def validate_and_normalize_search_request(request: TourSearchRequest) -> TourSearchRequest:
    """Return the canonical request that is safe to send to Tourvisor."""
    _validate_blocked_destination(request)

    date_from = _parse_iso_date(request.date_from, "date_from")
    date_to = _parse_iso_date(request.date_to, "date_to")
    departure_span_days = (date_to - date_from).days
    if departure_span_days < 0:
        raise SearchInputError(
            "INVALID_DATES",
            "Конец диапазона вылета не может быть раньше его начала.",
        )
    if date_from < _business_today():
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

    if request.children > 3:
        raise ManagerRoutingRequired("MANAGER_REQUIRED_TOO_MANY_CHILDREN")
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

    budget_policy = BudgetPolicy.from_request(request)
    return request.model_copy(
        update={
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            **budget_policy.request_updates(),
        }
    )


def unverified_preferences(request: TourSearchRequest) -> list[str]:
    """Preserve optional free-text wishes until Tourvisor fields are verified."""
    values = [request.hotel_preferences, request.beach_preferences]
    return [value for value in values if value]
