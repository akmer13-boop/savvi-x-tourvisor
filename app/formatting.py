from app.media import first_tour_image
from app.models import TourOption, TourSearchRequest


def _travelers_text(request: TourSearchRequest) -> str:
    base = f"{request.adults} взр."
    if request.children:
        ages = ", ".join(str(age) for age in request.children_ages)
        if ages:
            return f"{base} + {request.children} реб. ({ages} лет)"
        return f"{base} + {request.children} реб."
    return base


def _money(value: int | None, currency: str = "RUB") -> str | None:
    if not value:
        return None
    symbol = "₽" if currency.upper() == "RUB" else currency
    return f"{value:,.0f} {symbol}".replace(",", " ")


def format_tour_card_text(tour: TourOption, request: TourSearchRequest, index: int) -> str:
    travelers = _travelers_text(request)
    title_parts = []
    if tour.resort:
        title_parts.append(tour.resort)
    title_parts.append(tour.hotel + (f" {tour.stars}★" if tour.stars else ""))

    lines: list[str] = [f"{index}. {' — '.join(title_parts)}"]

    details: list[str] = []
    if tour.fly_date:
        details.append(f"вылет {tour.fly_date}")
    if tour.nights:
        details.append(f"{tour.nights} ночей")
    details.append(travelers)
    if details:
        lines.append("• " + ", ".join(details))

    if tour.meal:
        lines.append(f"• Питание: {tour.meal}")
    if tour.room:
        room_line = f"• Номер: {tour.room}"
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
        lines.append(f"• Цена: от {price}")
    if tour.operator:
        lines.append(f"• Туроператор: {tour.operator}")
    if tour.rating:
        lines.append(f"• Рейтинг отеля: {tour.rating}")
    if tour.link:
        lines.append(f"• Подробнее: {tour.link}")

    return "\n".join(lines)


def format_tours_for_client(
    tours: list[TourOption],
    request: TourSearchRequest,
    include_image_links: bool = False,
) -> str:
    """Human-readable text for Suvvy.

    By default image URLs are not embedded into text because many messengers show them as
    raw links. Images are returned separately in `images`, `cards` and `messages`.
    """
    if not tours:
        return (
            "По указанным параметрам сейчас не нашёл подходящих вариантов.\n\n"
            "Что можно расширить для повторного поиска:\n"
            "• даты вылета ±2–3 дня;\n"
            "• количество ночей;\n"
            "• соседние курорты;\n"
            "• бюджет или категорию отеля.\n\n"
            "Хотите, я попробую поискать альтернативы?"
        )

    lines: list[str] = ["Нашёл предварительные варианты под ваш запрос:"]

    for index, tour in enumerate(tours, start=1):
        lines.append("")
        lines.append(format_tour_card_text(tour, request, index))
        if include_image_links:
            image = first_tour_image(tour)
            if image:
                image_label = "Фото номера" if tour.room_images else "Фото отеля/тура"
                lines.append(f"• {image_label}: {image}")

    lines.append("")
    lines.append("Цены актуальны на момент поиска. Перед бронированием менеджер проверит наличие, перелёт и финальную стоимость.")
    lines.append("Хотите, я передам эти варианты менеджеру для проверки и точной подборки?")
    return "\n".join(lines)


def _tour_images(tour: TourOption, max_images: int = 2) -> list[str]:
    images: list[str] = []
    for url in tour.room_images:
        if url and url not in images:
            images.append(url)
        if len(images) >= max_images:
            return images
    if tour.tour_picture and tour.tour_picture not in images:
        images.append(tour.tour_picture)
    return images[:max_images]


def format_tours_with_images_for_client(
    tours: list[TourOption],
    request: TourSearchRequest,
    images_per_tour: int = 2,
) -> str:
    """Human-readable structured text with direct image URLs.

    Suvvy structured answers can render images when the final bot response contains
    direct image links. Keep this payload compact: no JSON arrays, only text + direct
    URLs grouped under each hotel.
    """
    if not tours:
        return format_tours_for_client(tours, request, include_image_links=False)

    lines: list[str] = ["Нашёл предварительные варианты под ваш запрос:"]

    for index, tour in enumerate(tours, start=1):
        lines.append("")
        lines.append(format_tour_card_text(tour, request, index))

        images = _tour_images(tour, max_images=images_per_tour)
        if images:
            label = "Фото номера:" if tour.room_images else "Фото отеля:"
            lines.append(label)
            for url in images:
                lines.append(url)

    lines.append("")
    lines.append("Цены актуальны на момент поиска. Перед бронированием менеджер проверит наличие, перелёт и финальную стоимость.")
    lines.append("Хотите, я передам эти варианты менеджеру для проверки и точной подборки?")
    return "\n".join(lines)
