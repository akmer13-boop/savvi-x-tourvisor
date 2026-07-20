from app.config import settings
from app.models import TourOption, TourSearchRequest
from app.operator_policy import OperatorPolicy
from app.runtime import operator_policy


def score_tour(tour: TourOption, request: TourSearchRequest) -> int:
    """MVP scoring after mandatory business filters."""
    score = 0

    if request.budget and tour.price:
        if tour.price <= request.budget:
            score += 40
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
        score += max(0, 10 - int(tour.price / 100000))

    return score


def select_best_tours(
    tours: list[TourOption],
    request: TourSearchRequest,
    limit: int = 5,
    *,
    policy: OperatorPolicy | None = None,
) -> list[TourOption]:
    policy = policy or operator_policy

    # Stakeholder rule: only hotels with an explicit rating >= configured threshold.
    min_rating = settings.tourvisor_min_hotel_rating
    tours = [tour for tour in tours if tour.rating is not None and tour.rating >= min_rating]

    if policy.enforced:
        tours = [tour for tour in tours if tour.operator_id in policy.active_ids]

    if request.budget:
        # The budget is a strict upper bound. Unknown prices cannot be offered.
        tours = [tour for tour in tours if tour.price is not None and tour.price <= request.budget]

    if request.hotel_stars:
        tours = [tour for tour in tours if tour.stars is not None and tour.stars >= request.hotel_stars]

    sorted_tours = sorted(tours, key=lambda item: score_tour(item, request), reverse=True)
    return sorted_tours[:limit]
