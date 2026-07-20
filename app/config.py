from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_environment: str = "development"
    service_version: str = "0.4.0"
    git_commit_sha: str = "unknown"

    # Suvvy webhook authentication. Body-token support is transitional only.
    suvvy_webhook_token: str = ""
    suvvy_allow_body_token: bool = True

    # Tourvisor Search API
    tourvisor_api_base_url: str = "https://api.tourvisor.ru"
    tourvisor_jwt: str = ""
    tourvisor_currency: str = "RUB"
    tourvisor_timeout_seconds: int = 20
    tourvisor_poll_attempts: int = 4
    tourvisor_poll_interval_seconds: float = 2.0
    tourvisor_results_limit: int = 25

    # Business filters and fail-closed operator policy.
    operator_registry_path: str = "config/operator_registry.json"
    tourvisor_min_hotel_rating: float = 4.0
    max_departure_window_days: int = 7
    max_nights_range: int = 10

    # Media
    tourvisor_enable_hotel_images: bool = True
    tourvisor_hotel_images_limit: int = 1
    tourvisor_enable_room_images: bool = True
    tourvisor_room_images_limit: int = 2
    tourvisor_main_image_limit: int = 1

    # Suvvy Free/Start plan output optimisation (1024 max output tokens)
    suvvy_tours_limit: int = 3
    suvvy_room_images_per_tour: int = 1
    suvvy_compact_output: bool = True

    # Production hardening.
    enable_debug_endpoints: bool = False
    expose_api_docs: bool = False

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
    def operator_policy_required(self) -> bool:
        """Every real Tourvisor request must use a non-empty active-contract list."""
        return not self.mock_tourvisor

    @property
    def production_mode(self) -> bool:
        return self.app_environment.strip().lower() == "production"

    def validate_runtime_configuration(self, active_operator_count: int) -> None:
        """Fail before serving traffic when a real integration is unsafe."""
        if self.mock_tourvisor:
            return
        if not self.effective_tourvisor_jwt:
            raise ValueError("TOURVISOR_JWT is required when MOCK_TOURVISOR=false")
        if not self.suvvy_webhook_token:
            raise ValueError("SUVVY_WEBHOOK_TOKEN is required when MOCK_TOURVISOR=false")
        if active_operator_count <= 0:
            raise ValueError("At least one active_contract operator is required when MOCK_TOURVISOR=false")


settings = Settings()
