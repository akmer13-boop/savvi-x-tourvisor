from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.models import TourOption


def absolute_url(url: str | None) -> str | None:
    """Normalize Tourvisor protocol-relative URLs for messengers and Suvvy."""
    if not url:
        return None
    value = str(url).strip()
    if not value:
        return None
    if value.startswith("//"):
        return f"https:{value}"
    if value.startswith("http://"):
        return "https://" + value.removeprefix("http://")
    return value


def safe_file_name(value: str | None, default: str = "image") -> str:
    text = (value or default).strip().lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", "_", text, flags=re.IGNORECASE).strip("_")
    return text[:60] or default


def image_file_name(url: str | None, hotel: str | None, index: int, source: str = "image") -> str:
    suffix = ".jpg"
    if url:
        path = Path(url.split("?", 1)[0])
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif"}:
            suffix = path.suffix.lower()
    return f"tour_{index}_{safe_file_name(hotel, 'hotel')}_{source}{suffix}"


def normalize_tour_media(tour: TourOption) -> None:
    tour.room_images = [url for url in (absolute_url(url) for url in tour.room_images) if url]
    tour.tour_picture = absolute_url(tour.tour_picture)
    tour.link = absolute_url(tour.link)


def ordered_tour_images(tour: TourOption, room_limit: int = 2) -> list[tuple[str, str]]:
    """Return hotel cover first, then room images, without duplicates."""
    result: list[tuple[str, str]] = []
    seen: set[str] = set()

    if tour.tour_picture:
        result.append((tour.tour_picture, "hotel"))
        seen.add(tour.tour_picture)

    for url in tour.room_images:
        if not url or url in seen:
            continue
        result.append((url, "room"))
        seen.add(url)
        if sum(1 for _, source in result if source == "room") >= room_limit:
            break

    return result


def first_tour_image(tour: TourOption) -> str | None:
    # Stakeholder rule: prefer the selling/cover image over room photos.
    return tour.tour_picture or (tour.room_images[0] if tour.room_images else None)


def image_assets_from_tours(tours: list[TourOption], limit_per_tour: int = 2) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    for tour_index, tour in enumerate(tours, start=1):
        ordered = ordered_tour_images(tour, room_limit=limit_per_tour)
        for image_index, (url, source) in enumerate(ordered, start=1):
            normalized_url = absolute_url(url)
            if not normalized_url:
                continue
            if source == "hotel":
                caption = f"{tour_index}. {tour.hotel} — главное фото отеля"
            else:
                caption = f"{tour_index}. {tour.hotel}" + (f" — {tour.room}" if tour.room else " — фото номера")
            assets.append(
                {
                    "tour_index": tour_index,
                    "image_index": image_index,
                    "url": normalized_url,
                    "caption": caption,
                    "hotel": tour.hotel,
                    "room": tour.room,
                    "file_name": image_file_name(normalized_url, tour.hotel, tour_index, source),
                    "file_type": "image",
                    "mime_type": _mime_type_from_url(normalized_url),
                    "source": source,
                }
            )
    return assets


def cards_from_tours(tours: list[TourOption]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for index, tour in enumerate(tours, start=1):
        cards.append(
            {
                "index": index,
                "title": f"{tour.hotel}{f' {tour.stars}★' if tour.stars else ''}",
                "subtitle": tour.resort or tour.country,
                "image_url": first_tour_image(tour),
                "price": tour.price,
                "currency": tour.currency,
                "meal": tour.meal,
                "room": tour.room,
                "fly_date": tour.fly_date,
                "nights": tour.nights,
                "link": tour.link,
            }
        )
    return cards


def message_blocks_from_tours(client_text: str, tours: list[TourOption]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [{"type": "text", "text": client_text}]
    for asset in image_assets_from_tours(tours, limit_per_tour=2):
        blocks.append(
            {
                "type": "image",
                "url": asset["url"],
                "caption": asset["caption"],
                "file_name": asset["file_name"],
                "file_type": "image",
            }
        )
    return blocks


def _mime_type_from_url(url: str) -> str:
    lower = url.split("?", 1)[0].lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"
