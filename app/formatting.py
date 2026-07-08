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


def _first_image(tour: TourOption) -> str | None:
    if tour.room_images:
        return tour.room_images[0]
    return tour.tour_picture


def format_tours_for_client(tours: list[TourOption], request: TourSearchRequest) -> str:
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
    travelers = _travelers_text(request)

    for index, tour in enumerate(tours, start=1):
        title_parts = []
        if tour.resort:
            title_parts.append(tour.resort)
        title_parts.append(tour.hotel + (f" {tour.stars}★" if tour.stars else ""))

        lines.append("")
        lines.append(f"{index}. {' — '.join(title_parts)}")

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

        image = _first_image(tour)
        if image:
            image_label = "Фото номера" if tour.room_images else "Фото отеля/тура"
            lines.append(f"• {image_label}: {image}")

        if tour.link:
            lines.append(f"• Ссылка: {tour.link}")

    lines.append("")
    lines.append("Цены актуальны на момент поиска. Перед бронированием менеджер проверит наличие, перелёт и финальную стоимость.")
    lines.append("Хотите, я передам эти варианты менеджеру для проверки и точной подборки?")
    return "\n".join(lines)
