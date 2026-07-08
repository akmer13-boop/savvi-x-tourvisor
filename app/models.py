from typing import Any
from pydantic import BaseModel, Field, field_validator


class TourSearchRequest(BaseModel):
    auth_token: str | None = Field(default=None, description="Токен прослойки для Suvvy, если нельзя передать Authorization header")
    departure_city: str = Field(..., description="Город вылета")
    country: str = Field(..., description="Страна / направление")
    resort: str | None = Field(default=None, description="Курорт, если известен")
    date_from: str | None = Field(default=None, description="Дата начала диапазона, YYYY-MM-DD или текст из Suvvy")
    date_to: str | None = Field(default=None, description="Дата конца диапазона, YYYY-MM-DD или текст из Suvvy")
    nights_from: int | None = Field(default=None, ge=1)
    nights_to: int | None = Field(default=None, ge=1)
    adults: int = Field(default=2, ge=1)
    children: int = Field(default=0, ge=0)
    children_ages: list[int] = Field(default_factory=list)
    budget: int | None = Field(default=None, ge=0, description="Бюджет в рублях")
    meal: str | None = Field(default=None, description="Питание")
    hotel_stars: int | None = Field(default=None, ge=1, le=5)
    hotel_preferences: str | None = None
    beach_preferences: str | None = None
    client_name: str | None = None
    client_phone: str | None = None
    chat_id: str | None = None

    @field_validator("departure_city", "country")
    @classmethod
    def required_text(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("field is required")
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
            "operator": self.operator,
            "room": self.room,
            "room_id": self.room_id,
            "room_images": self.room_images,
            "tour_picture": self.tour_picture,
            "link": self.link,
            "rating": self.rating,
        }


class BotResponse(BaseModel):
    status: str = "ok"
    found: bool
    client_text: str
    tours_count: int = 0
    search_id: str | None = None
    tours: list[dict[str, Any]] = Field(default_factory=list)
