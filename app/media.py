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
    text = (value or default).strip().lower()
    text = text.replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", "_", text, flags=re.IGNORECASE).strip("_")
    return text[:60] or default


def image_file_name(url: str | None, hotel: str | None, index: int) -> str:
    suffix = ".jpg"
    if url:
        path = Path(url.split("?", 1)[0])
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif"}:
            suffix = path.suffix.lower()
    return f"tour_{index}_{safe_file_name(hotel, 'hotel')}{suffix}"


def normalize_tour_media(tour: TourOption) -> None:
    tour.room_images = [url for url in (absolute_url(url) for url in tour.room_images) if url]
    tour.tour_picture = absolute_url(tour.tour_picture)
    tour.link = absolute_url(tour.link)


def first_tour_image(tour: TourOption) -> str | None:
    if tour.room_images:
        return tour.room_images[0]
    return tour.tour_picture


def image_assets_from_tours(tours: list[TourOption], limit_per_tour: int = 1) -> list[dict[str, Any]]:
    """Return direct image URLs as structured assets for Suvvy variables/cards.

    This does not assume that a Suvvy webhook response can render attachments by itself.
    The assets are returned separately so Suvvy can map them if the channel/action supports images.
    """
    assets: list[dict[str, Any]] = []
    for tour_index, tour in enumerate(tours, start=1):
        images = tour.room_images[:limit_per_tour] if tour.room_images else ([tour.tour_picture] if tour.tour_picture else [])
        for image_index, url in enumerate(images, start=1):
            normalized_url = absolute_url(url)
            if not normalized_url:
                continue
            caption_parts = [f"{tour_index}. {tour.hotel}"]
            if tour.room:
                caption_parts.append(tour.room)
            caption = " — ".join(caption_parts)
            assets.append(
                {
                    "tour_index": tour_index,
                    "image_index": image_index,
                    "url": normalized_url,
                    "caption": caption,
                    "hotel": tour.hotel,
                    "room": tour.room,
                    "file_name": image_file_name(normalized_url, tour.hotel, tour_index),
                    "file_type": "image",
                    "mime_type": _mime_type_from_url(normalized_url),
                    "source": "room" if tour.room_images else "hotel_or_tour",
                }
            )
    return assets


def cards_from_tours(tours: list[TourOption]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for index, tour in enumerate(tours, start=1):
        image_url = first_tour_image(tour)
        cards.append(
            {
                "index": index,
                "title": f"{tour.hotel}{f' {tour.stars}★' if tour.stars else ''}",
                "subtitle": tour.resort or tour.country,
                "image_url": image_url,
                "price": tour.price,
                "currency": tour.currency,
                "meal": tour.meal,
                "room": tour.room,
                "fly_date": tour.fly_date,
                "nights": tour.nights,
                "operator": tour.operator,
                "rating": tour.rating,
                "link": tour.link,
            }
        )
    return cards


def message_blocks_from_tours(client_text: str, tours: list[TourOption]) -> list[dict[str, Any]]:
    """Channel-agnostic ordered blocks: text + images + short captions.

    Suvvy webhook docs describe returning results/variables to the bot, not a universal
    attachment response format. These blocks are therefore an integration-friendly
    structure, not a guarantee that Suvvy will render images automatically.
    """
    blocks: list[dict[str, Any]] = [{"type": "text", "text": client_text}]
    for asset in image_assets_from_tours(tours, limit_per_tour=1):
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
