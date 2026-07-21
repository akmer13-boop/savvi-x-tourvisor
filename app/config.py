from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_environment: str = "development"
    service_version: str = "0.5.0"
    api_contract_version: str = "2026-07-21.2"
    git_commit_sha: str = "unknown"

    # Suvvy webhook authentication. Body-token support is transitional only.
    suvvy_webhook_token: str = ""
    suvvy_allow_body_token: bool = True
    suvvy_previous_webhook_token: str = ""

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
    business_timezone: str = "Europe/Moscow"

    # Per-chat idempotency and search limits. The persistent Amvera mount is
    # /data; the guard remains disabled during the backward-compatible bot
    # migration and is then enabled explicitly.
    search_guard_enabled: bool = False
    search_guard_db_path: str = "/data/search_guard.sqlite3"
    search_guard_hmac_secret: str = ""
    search_guard_namespace: str = "suvvy-tourvisor"
    search_guard_ttl_seconds: int = 72 * 60 * 60
    search_guard_max_searches: int = 2
    search_result_replay_ttl_seconds: int = 45
    search_guard_prune_interval_seconds: int = 15
    search_guard_persistence_verified: bool = False

    # This stays deliberately unverified until an approved Tourvisor contract
    # check confirms priceFrom plus result pagination/sorting semantics.
    tourvisor_api_contract_version: str = "unverified"
    tourvisor_price_from_enabled: bool = False

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
        try:
            ZoneInfo(self.business_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("BUSINESS_TIMEZONE must be a valid IANA timezone") from exc
        if self.tourvisor_price_from_enabled and self.tourvisor_api_contract_version.strip().lower() in {
            "",
            "unknown",
            "unverified",
        }:
            raise ValueError(
                "TOURVISOR_API_CONTRACT_VERSION must be verified before enabling priceFrom"
            )
        if self.production_mode:
            if self.mock_tourvisor:
                raise ValueError("MOCK_TOURVISOR must be false in production")
            if self.enable_debug_endpoints or self.expose_api_docs:
                raise ValueError("Debug endpoints and API docs must be disabled in production")
            if self.git_commit_sha.strip().lower() in {"", "unknown"}:
                raise ValueError("GIT_COMMIT_SHA must identify the deployed commit in production")
        if self.search_guard_enabled:
            if not self.search_guard_hmac_secret:
                raise ValueError("SEARCH_GUARD_HMAC_SECRET is required when search guard is enabled")
            if self.search_guard_ttl_seconds != 72 * 60 * 60:
                raise ValueError("SEARCH_GUARD_TTL_SECONDS must be exactly 259200")
            if self.search_guard_max_searches != 2:
                raise ValueError("SEARCH_GUARD_MAX_SEARCHES must be exactly 2")
            if not 0 < self.search_result_replay_ttl_seconds <= 60:
                raise ValueError(
                    "SEARCH_RESULT_REPLAY_TTL_SECONDS must be between 1 and 60"
                )
            if not 0 < self.search_guard_prune_interval_seconds <= 60:
                raise ValueError(
                    "SEARCH_GUARD_PRUNE_INTERVAL_SECONDS must be between 1 and 60"
                )
            if (
                self.search_result_replay_ttl_seconds
                + self.search_guard_prune_interval_seconds
                > 60
            ):
                raise ValueError(
                    "Replay TTL plus prune interval must not exceed 60 seconds"
                )
            if not self.search_guard_persistence_verified:
                raise ValueError(
                    "SEARCH_GUARD_PERSISTENCE_VERIFIED must be true after a restart persistence check"
                )
        if self.mock_tourvisor:
            return
        if not self.effective_tourvisor_jwt:
            raise ValueError("TOURVISOR_JWT is required when MOCK_TOURVISOR=false")
        if not self.suvvy_webhook_token:
            raise ValueError("SUVVY_WEBHOOK_TOKEN is required when MOCK_TOURVISOR=false")
        if active_operator_count <= 0:
            raise ValueError("At least one active_contract operator is required when MOCK_TOURVISOR=false")


settings = Settings()
