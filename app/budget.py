from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


BudgetType = Literal["max", "min", "approx", "range", "unknown"]


class SearchInputError(ValueError):
    """A client-visible validation problem that must not call Tourvisor."""

    def __init__(self, reason: str, client_text: str) -> None:
        super().__init__(client_text)
        self.reason = reason
        self.client_text = client_text


def _invalid_budget(client_text: str) -> SearchInputError:
    return SearchInputError("INVALID_BUDGET", client_text)


def normalize_budget_rub(value: int | None) -> int:
    """Normalize a client-entered package price without re-scaling ruble values."""
    if value is None or isinstance(value, bool) or value <= 0:
        raise _invalid_budget("Уточните общий бюджет путёвки в рублях.")
    if 50 <= value <= 999:
        return value * 1_000
    if value >= 50_000:
        return value
    raise _invalid_budget(
        "Уточните общий бюджет путёвки в рублях, например 300 000 или 500 000."
    )


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    """Canonical price bounds used by validation, Tourvisor and post-filtering.

    ``budget_to`` is the anchor for ``approx`` requests. The effective
    ``price_from``/``price_to`` values are derived here and are never written
    back into that request field, which makes repeated validation idempotent.
    """

    budget_type: BudgetType
    price_from: int | None
    price_to: int | None
    anchor: int | None = None
    normalized_budget: int | None = None
    normalized_budget_from: int | None = None
    normalized_budget_to: int | None = None

    @classmethod
    def from_request(cls, request: Any) -> BudgetPolicy:
        mode = getattr(request, "budget_type", None)
        legacy_raw = getattr(request, "budget", None)
        from_raw = getattr(request, "budget_from", None)
        to_raw = getattr(request, "budget_to", None)

        legacy = normalize_budget_rub(legacy_raw) if legacy_raw is not None else None
        budget_from = normalize_budget_rub(from_raw) if from_raw is not None else None
        budget_to = normalize_budget_rub(to_raw) if to_raw is not None else None

        if mode is None:
            if legacy is None:
                if budget_from is not None or budget_to is not None:
                    raise _invalid_budget(
                        "Уточните тип бюджета: максимум, минимум, примерно или диапазон."
                    )
                raise _invalid_budget("Уточните общий бюджет путёвки в рублях.")
            if budget_from is not None or budget_to is not None:
                raise _invalid_budget(
                    "Передайте либо прежнее поле budget, либо новый бюджетный контракт."
                )
            return cls(
                budget_type="max",
                price_from=None,
                price_to=legacy,
                normalized_budget=legacy,
            )

        if legacy is not None:
            if mode == "max" and budget_from is None and budget_to == legacy:
                return cls(
                    budget_type="max",
                    price_from=None,
                    price_to=budget_to,
                    normalized_budget=legacy,
                    normalized_budget_to=budget_to,
                )
            raise _invalid_budget(
                "Передайте либо прежнее поле budget, либо новый бюджетный контракт."
            )

        if mode == "unknown":
            if budget_from is not None or budget_to is not None:
                raise _invalid_budget(
                    "Для поиска без ограничения не передавайте ценовые границы."
                )
            return cls(budget_type="unknown", price_from=None, price_to=None)

        if mode == "max":
            if budget_to is None or budget_from is not None:
                raise _invalid_budget(
                    "Для бюджета «до» укажите только верхнюю границу budget_to."
                )
            return cls(
                budget_type="max",
                price_from=None,
                price_to=budget_to,
                normalized_budget_to=budget_to,
            )

        if mode == "min":
            if budget_from is None or budget_to is not None:
                raise _invalid_budget(
                    "Для бюджета «от» укажите только нижнюю границу budget_from."
                )
            return cls(
                budget_type="min",
                price_from=budget_from,
                price_to=None,
                normalized_budget_from=budget_from,
            )

        if mode == "approx":
            if budget_to is None or budget_from is not None:
                raise _invalid_budget(
                    "Для примерного бюджета укажите сумму-ориентир в budget_to."
                )
            return cls(
                budget_type="approx",
                price_from=(budget_to * 90 + 99) // 100,
                price_to=(budget_to * 110) // 100,
                anchor=budget_to,
                normalized_budget_to=budget_to,
            )

        if mode == "range":
            if budget_from is None or budget_to is None:
                raise _invalid_budget(
                    "Для диапазона укажите обе границы: budget_from и budget_to."
                )
            if budget_from > budget_to:
                raise _invalid_budget(
                    "Нижняя граница бюджета не может превышать верхнюю."
                )
            return cls(
                budget_type="range",
                price_from=budget_from,
                price_to=budget_to,
                normalized_budget_from=budget_from,
                normalized_budget_to=budget_to,
            )

        raise _invalid_budget("Уточните тип бюджета.")

    def request_updates(self) -> dict[str, int]:
        """Return only normalized client fields, never derived approx bounds."""
        updates: dict[str, int] = {}
        if self.normalized_budget is not None:
            updates["budget"] = self.normalized_budget
        if self.normalized_budget_from is not None:
            updates["budget_from"] = self.normalized_budget_from
        if self.normalized_budget_to is not None:
            updates["budget_to"] = self.normalized_budget_to
        return updates

    def allows(self, price: int | None) -> bool:
        """Apply strict inclusive bounds; an option without a price is unusable."""
        if price is None or price <= 0:
            return False
        if self.price_from is not None and price < self.price_from:
            return False
        return self.price_to is None or price <= self.price_to

    def priority_bucket(self, price: int | None) -> int:
        """For ``max``, prefer the final 100k below the strict ceiling."""
        if not self.allows(price) or self.budget_type != "max" or self.price_to is None:
            return 0
        corridor_from = max(0, self.price_to - 100_000)
        return int(price >= corridor_from)

    def hotel_choice_key(self, price: int | None) -> tuple[int, int]:
        """Prefer the max-budget corridor, then the cheaper allowed room."""
        if price is None:
            return (-1, 0)
        return (self.priority_bucket(price), -price)
