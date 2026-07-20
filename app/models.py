from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class TourSearchRequest(BaseModel):
    auth_token: str | None = Field(default=None, description="Токен прослойки для Suvvy, если нельзя передать Authorization header")
    departure_city: str = Field(..., description="Город вылета")
    country: str = Field(..., description="Страна / направление")
    resort: str | None = Field(default=None, description="Курорт, если известен")
    date_from: str | None = Field(default=None, description="Начало окна вылета, строго YYYY-MM-DD")
    date_to: str | None = Field(default=None, description="Конец окна вылета, строго YYYY-MM-DD")
    nights_from: int | None = Field(default=None, ge=1)
    nights_to: int | None = Field(default=None, ge=1)
    adults: int = Field(default=2, ge=1)
    children: int = Field(default=0, ge=0, le=3)
    children_ages: list[int] = Field(default_factory=list)
    budget: int | None = Field(default=None, ge=0, description="Бюджет в рублях")
    meal: str | None = Field(default=None, description="Питание")
    hotel_stars: int | None = Field(default=None, ge=1, le=5)
    hotel_preferences: str | None = None
    beach_preferences: str | None = None
    client_name: str | None = None
    client_phone: str | None = None
    chat_id: str | None = Field(default=None, description="ID диалога в Suvvy/канале, если доступен")
    source: str | None = Field(default=None, description="Источник диалога/лида, если доступен")
    image_mode: Literal["structured", "links_in_text", "none"] = Field(
        default="structured",
        description="Как возвращать фото: structured — отдельно в JSON; links_in_text — ссылками в тексте; none — без фото",
    )

    @field_validator("departure_city", "country")
    @classmethod
    def required_text(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("field is required")
        return value

    @field_validator(
        "auth_token",
        "resort",
        "date_from",
        "date_to",
        "meal",
        "hotel_preferences",
        "beach_preferences",
        "client_name",
        "client_phone",
        "chat_id",
        "source",
        mode="before",
    )
    @classmethod
    def strip_optional_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("children_ages", mode="before")
    @classmethod
    def parse_children_ages(cls, value: Any) -> list[int]:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return [int(v) for v in value if str(v).strip()]
        if isinstance(value, str):
            return [int(part.strip()) for part in value.replace(";", ",").split(",") if part.strip().isdigit()]
        return []

    @field_validator("budget", mode="before")
    @classmethod
    def parse_budget(cls, value: Any) -> Any:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            raise ValueError("budget must be an integer")
        if isinstance(value, str):
            cleaned = value.replace(" ", "").replace("\u00a0", "").strip()
            if not cleaned.isdigit():
                raise ValueError("budget must be an integer")
            return int(cleaned)
        return value


class TourOption(BaseModel):
    country: str
    resort: str | None = None
    hotel: str
    stars: int | None = None
    meal: str | None = None
    departure_city: str | None = None
    fly_date: str | None = None
    nights: int | None = None
    adults: int | None = None
    children: int | None = None
    price: int | None = None
    currency: str = "RUB"
    operator: str | None = None
    operator_id: int | None = None
    room: str | None = None
    link: str | None = None
    rating: float | None = None
    hotel_id: int | None = None
    tour_id: str | None = None
    room_id: int | None = None
    tour_picture: str | None = None
    room_images: list[str] = Field(default_factory=list)
    room_description: str | None = None
    room_area: int | None = None
    room_sleeping_places: str | None = None
    room_view_description: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return {
            "country": self.country,
            "resort": self.resort,
            "hotel": self.hotel,
            "stars": self.stars,
            "meal": self.meal,
            "departure_city": self.departure_city,
            "fly_date": self.fly_date,
            "nights": self.nights,
            "adults": self.adults,
            "children": self.children,
            "price": self.price,
            "currency": self.currency,
            "room": self.room,
            "room_id": self.room_id,
            "room_images": self.room_images,
            "tour_picture": self.tour_picture,
            "link": self.link,
        }


class BotResponse(BaseModel):
    status: Literal["ok", "needs_clarification", "error"] = "ok"
    found: bool
    reason: str = "FOUND"
    request_id: str
    client_text: str
    tours_count: int = 0
    search_id: str | None = None
    whitelist_version: str
    whitelist_hash: str
    unverified_preferences: list[str] = Field(default_factory=list)
    tours: list[dict[str, Any]] = Field(default_factory=list)
    cards: list[dict[str, Any]] = Field(default_factory=list, description="Карточки туров для маппинга в Suvvy")
    images: list[dict[str, Any]] = Field(default_factory=list, description="Фото отдельным массивом; URL уже полные https")
    messages: list[dict[str, Any]] = Field(default_factory=list, description="Упорядоченные блоки text/image для интеграции")
    image_delivery_note: str | None = Field(default=None, description="Подсказка, как корректно выводить изображения в Suvvy")


class ShortBotResponse(BaseModel):
    status: Literal["ok", "needs_clarification", "error"] = "ok"
    found: bool
    reason: str = "FOUND"
    request_id: str
    client_text: str
    tours_count: int = 0
    search_id: str | None = None
    whitelist_version: str
    whitelist_hash: str
    unverified_preferences: list[str] = Field(default_factory=list)
