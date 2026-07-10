from __future__ import annotations

from datetime import date, datetime

from app.models import TourOption, TourSearchRequest


_RU_MONTHS = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}


def _format_date_ru(value: str | None) -> str | None:
    if not value:
        return None
    raw = str(value).strip()
    parsed: date | None = None
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            parsed = datetime.strptime(raw[:10], fmt).date()
            break
        except ValueError:
            continue
    if not parsed:
        return raw
    return f"{parsed.day} {_RU_MONTHS[parsed.month]} {parsed.year} года"


def _adult_word(value: int) -> str:
    if value % 10 == 1 and value % 100 != 11:
        return "взрослый"
    if value % 10 in {2, 3, 4} and value % 100 not in {12, 13, 14}:
        return "взрослых"
    return "взрослых"


def _child_word(value: int) -> str:
    if value % 10 == 1 and value % 100 != 11:
        return "ребёнок"
    if value % 10 in {2, 3, 4} and value % 100 not in {12, 13, 14}:
        return "ребёнка"
    return "детей"


def _night_word(value: int) -> str:
    if value % 10 == 1 and value % 100 != 11:
        return "ночь"
    if value % 10 in {2, 3, 4} and value % 100 not in {12, 13, 14}:
        return "ночи"
    return "ночей"


def _travelers_text(request: TourSearchRequest) -> str:
    parts = [f"{request.adults} {_adult_word(request.adults)}"]
    if request.children:
        child_text = f"{request.children} {_child_word(request.children)}"
        if request.children_ages:
            ages = ", ".join(str(age) for age in request.children_ages)
            child_text += f" ({ages} лет)"
        parts.append(child_text)
    return ", ".join(parts)


def _money(value: int | None, currency: str = "RUB") -> str | None:
    if not value:
        return None
    symbol = "₽" if currency.upper() == "RUB" else currency
    return f"{value:,.0f} {symbol}".replace(",", " ")


def _location(tour: TourOption) -> str | None:
    values: list[str] = []
    if tour.resort:
        values.append(tour.resort)
    if tour.country and tour.country not in values:
        values.append(tour.country)
    return ", ".join(values) or None


def format_tour_card_text(tour: TourOption, request: TourSearchRequest, index: int) -> str:
    title = tour.hotel + (f" {tour.stars}★" if tour.stars else "")
    lines: list[str] = [f"🏨 {index}. {title}"]

    location = _location(tour)
    if location:
        lines.append(f"📍 {location}")

    fly_date = _format_date_ru(tour.fly_date)
    if fly_date:
        lines.append(f"✈️ Вылет: {fly_date}")
    if tour.nights:
        lines.append(f"🌙 Продолжительность: {tour.nights} {_night_word(tour.nights)}")

    lines.append(f"👥 Туристы: {_travelers_text(request)}")

    if tour.meal:
        lines.append(f"🍽️ Питание: {tour.meal}")

    if tour.room:
        room_line = f"🛏️ Номер: {tour.room}"
        extras: list[str] = []
        if tour.room_area:
            extras.append(f"{tour.room_area} м²")
        if tour.room_view_description:
            extras.append(tour.room_view_description)
        if extras:
            room_line += " (" + ", ".join(extras) + ")"
        lines.append(room_line)

    price = _money(tour.price, tour.currency)
    if price:
        lines.append(f"💰 Стоимость: от {price}")
    if tour.link:
        lines.append(f"🔗 Подробнее: {tour.link}")

    # Rating and operator are deliberately not exposed to the tourist.
    return "\n".join(lines)


def _group_images(tour: TourOption, room_limit: int = 2) -> tuple[list[str], list[str]]:
    main_images: list[str] = []
    room_images: list[str] = []

    if tour.tour_picture:
        main_images.append(tour.tour_picture)

    for url in tour.room_images:
        if not url or url in main_images or url in room_images:
            continue
        room_images.append(url)
        if len(room_images) >= room_limit:
            break

    # If Tourvisor search did not return a hotel cover, do not lose images:
    # the first room image remains under the room-photo label.
    return main_images[:1], room_images


def format_tours_for_client(
    tours: list[TourOption],
    request: TourSearchRequest,
    include_image_links: bool = False,
) -> str:
    if not tours:
        return (
            "По заданным параметрам сейчас не удалось найти подходящие варианты. "
            "Я зафиксировала Ваш запрос — менеджер свяжется с Вами в ближайшее время."
        )

    lines: list[str] = ["Нашла предварительные варианты по Вашему запросу:"]

    for index, tour in enumerate(tours, start=1):
        lines.append("")
        lines.append(format_tour_card_text(tour, request, index))
        if include_image_links:
            main_images, room_images = _group_images(tour)
            if main_images:
                lines.append("📸 Главное фото отеля:")
                lines.extend(main_images)
            if room_images:
                lines.append("📸 Фотографии номера:")
                lines.extend(room_images)

    lines.append("")
    lines.append("Цены актуальны на момент поиска. Перед бронированием менеджер проверит наличие, перелёт и финальную стоимость.")
    # No closing question here: Suvvy controls dialogue continuation and must not duplicate it.
    return "\n".join(lines)


def format_tours_with_images_for_client(
    tours: list[TourOption],
    request: TourSearchRequest,
    images_per_tour: int = 2,
) -> str:
    if not tours:
        return format_tours_for_client(tours, request, include_image_links=False)

    lines: list[str] = ["Нашла предварительные варианты по Вашему запросу:"]

    for index, tour in enumerate(tours, start=1):
        lines.append("")
        lines.append(format_tour_card_text(tour, request, index))

        main_images, room_images = _group_images(tour, room_limit=max(images_per_tour, 0))
        if main_images:
            lines.append("📸 Главное фото отеля:")
            lines.extend(main_images)
        if room_images:
            lines.append("📸 Фотографии номера:")
            lines.extend(room_images)

    lines.append("")
    lines.append("Цены актуальны на момент поиска. Перед бронированием менеджер проверит наличие, перелёт и финальную стоимость.")
    return "\n".join(lines)


def format_tours_compact_for_suvvy(
    tours: list[TourOption],
    request: TourSearchRequest,
    room_images_per_tour: int = 1,
) -> str:
    """Compact client text for Suvvy plans capped at 1024 output tokens.

    Keeps the stakeholder-required content and image order, but avoids the
    verbose five-card response that makes the LLM copy more than 1024 tokens.
    """
    if not tours:
        return format_tours_for_client(tours, request, include_image_links=False)

    lines: list[str] = ["Нашла подходящие варианты:"]
    for index, tour in enumerate(tours, start=1):
        title = tour.hotel + (f" {tour.stars}★" if tour.stars else "")
        lines.extend(["", f"🏨 {index}. {title}"])

        location = _location(tour)
        if location:
            lines.append(f"📍 {location}")

        trip_parts: list[str] = []
        fly_date = _format_date_ru(tour.fly_date)
        if fly_date:
            trip_parts.append(fly_date)
        if tour.nights:
            trip_parts.append(f"{tour.nights} {_night_word(tour.nights)}")
        if trip_parts:
            lines.append("✈️ " + " • ".join(trip_parts))

        lines.append(f"👥 {_travelers_text(request)}")
        if tour.meal:
            lines.append(f"🍽️ {tour.meal}")
        if tour.room:
            room_line = f"🛏️ {tour.room}"
            extras: list[str] = []
            if tour.room_area:
                extras.append(f"{tour.room_area} м²")
            if tour.room_view_description:
                extras.append(tour.room_view_description)
            if extras:
                room_line += " (" + ", ".join(extras) + ")"
            lines.append(room_line)

        price = _money(tour.price, tour.currency)
        if price:
            lines.append(f"💰 от {price}")

        main_images, room_images = _group_images(
            tour,
            room_limit=max(room_images_per_tour, 0),
        )
        if main_images:
            lines.append(f"🖼️ Отель: {main_images[0]}")
        for image_index, image_url in enumerate(room_images, start=1):
            label = "Номер" if len(room_images) == 1 else f"Номер {image_index}"
            lines.append(f"🖼️ {label}: {image_url}")

    lines.extend([
        "",
        "Цены актуальны на момент поиска. Наличие, перелёт и итоговую стоимость проверит менеджер.",
    ])
    return "\n".join(lines)
