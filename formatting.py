from app.models import TourOption, TourSearchRequest


def score_tour(tour: TourOption, request: TourSearchRequest) -> int:
    """Simple MVP scoring. Replace with business rules after first pilot."""
    score = 0

    if request.budget and tour.price:
        if tour.price <= request.budget:
            score += 40
        elif tour.price <= request.budget * 1.1:
            score += 10
        else:
            score -= 40

    if request.meal and tour.meal:
        if request.meal.lower() in tour.meal.lower() or tour.meal.lower() in request.meal.lower():
            score += 20

    if request.hotel_stars and tour.stars:
        if tour.stars >= request.hotel_stars:
            score += 15
        else:
            score -= 15

    if tour.rating:
        if tour.rating >= 4.5:
            score += 10
        elif tour.rating >= 4.0:
            score += 5

    if request.children and tour.meal and "всё" in tour.meal.lower():
        score += 5

    if tour.price:
        # Small tie-breaker: cheaper tours rank slightly better after relevance filters.
        score += max(0, 10 - int(tour.price / 100000))

    return score


def select_best_tours(tours: list[TourOption], request: TourSearchRequest, limit: int = 5) -> list[TourOption]:
    if request.budget:
        # Allow +10% because exact tourist budget is often flexible.
        tours = [tour for tour in tours if not tour.price or tour.price <= int(request.budget * 1.1)]

    if request.hotel_stars:
        tours = [tour for tour in tours if not tour.stars or tour.stars >= request.hotel_stars]

    sorted_tours = sorted(tours, key=lambda item: score_tour(item, request), reverse=True)
    return sorted_tours[:limit]
