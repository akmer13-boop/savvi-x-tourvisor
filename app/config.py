from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    suvvy_webhook_token: str = ""

    # Tourvisor Search API
    tourvisor_api_base_url: str = "https://api.tourvisor.ru"
    tourvisor_jwt: str = ""
    tourvisor_currency: str = "RUB"
    tourvisor_timeout_seconds: int = 20
    tourvisor_poll_attempts: int = 4
    tourvisor_poll_interval_seconds: float = 2.0
    tourvisor_results_limit: int = 25

    # Business filters
    tourvisor_min_hotel_rating: float = 4.0
    tourvisor_operator_whitelist_enabled: bool = False
    tourvisor_allowed_operator_ids: str = ""

    # Media
    tourvisor_enable_hotel_images: bool = True
    tourvisor_hotel_images_limit: int = 1
    tourvisor_enable_room_images: bool = True
    tourvisor_room_images_limit: int = 2
    tourvisor_main_image_limit: int = 1

    # Backward-compatible names from the first MVP build.
    tourvisor_api_key: str = ""
    tourvisor_search_url: str = ""

    mock_tourvisor: bool = True
    tourvisor_public_search_url: str = ""
    log_level: str = "INFO"

    @property
    def effective_tourvisor_jwt(self) -> str:
        return self.tourvisor_jwt or self.tourvisor_api_key

    @property
    def allowed_operator_ids(self) -> set[int]:
        result: set[int] = set()
        for raw in self.tourvisor_allowed_operator_ids.replace(";", ",").split(","):
            value = raw.strip()
            if not value:
                continue
            try:
                result.add(int(value))
            except ValueError:
                continue
        return result

    @property
    def operator_whitelist_active(self) -> bool:
        # Fail-open while the stakeholder mapping is not ready: an enabled flag
        # without IDs must not remove all tours from production.
        return self.tourvisor_operator_whitelist_enabled and bool(self.allowed_operator_ids)


settings = Settings()
