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
    tourvisor_poll_interval_seconds: float = 3.0
    tourvisor_results_limit: int = 25

    # Backward-compatible names from the first MVP build.
    tourvisor_api_key: str = ""
    tourvisor_search_url: str = ""

    mock_tourvisor: bool = True
    tourvisor_public_search_url: str = ""
    log_level: str = "INFO"

    @property
    def effective_tourvisor_jwt(self) -> str:
        return self.tourvisor_jwt or self.tourvisor_api_key


settings = Settings()
