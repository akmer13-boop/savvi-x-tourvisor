from app.models import TourOption, TourSearchRequest


def _travelers_text(request: TourSearchRequest) -> str:
    base = f"{request.adults} взр."
    if request.children:
        ages = ", ".join(str(age) for age in request.children_ages)
        if ages:
            return f"{base} + {request.children} реб. ({ages} лет)"
        return f"{base} + {request.children} реб."
    return base


def format_tours_for_client(tours: list[TourOption], request: TourSearchRequest) -> str:
    if not tours:
        return (
            "По указанным параметрам сейчас не нашёл подходящих вариантов. "
            "Можно расширить даты, изменить количество ночей, рассмотреть соседние курорты "
            "или немного скорректировать бюджет. Хотите, я попробую поискать альтернативы?"
        )

    lines: list[str] = ["Нашёл несколько предварительных вариантов под ваш запрос:"]
    travelers = _travelers_text(request)

    for index, tour in enumerate(tours, start=1):
        title_parts = [tour.country]
        if tour.resort:
            title_parts.append(tour.resort)
        hotel_line = tour.hotel
        if tour.stars:
            hotel_line += f" {tour.stars}★"

        lines.append("")
        lines.append(f"{index}. {', '.join(title_parts)} — {hotel_line}")
        if tour.fly_date:
            departure = tour.departure_city or request.departure_city
            lines.append(f"Вылет из {departure}: {tour.fly_date}")
        if tour.nights:
            lines.append(f"{tour.nights} ночей, {travelers}")
        if tour.meal:
            lines.append(f"Питание: {tour.meal}")
        if tour.room:
            lines.append(f"Номер: {tour.room}")
        if tour.price:
            lines.append(f"Цена: от {tour.price:,.0f} ₽".replace(",", " "))
        if tour.operator:
            lines.append(f"Туроператор: {tour.operator}")
        if tour.link:
            lines.append(f"Ссылка: {tour.link}")

    lines.append("")
    lines.append("Цены актуальны на момент поиска. Перед бронированием менеджер проверит наличие, перелёт и финальную стоимость.")
    lines.append("Хотите, я передам эти варианты менеджеру для проверки и точной подборки?")
    return "\n".join(lines)
