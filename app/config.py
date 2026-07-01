from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    suvvy_webhook_token: str = ""
    tourvisor_api_key: str = ""
    tourvisor_search_url: str = ""
    tourvisor_timeout_seconds: int = 15
    mock_tourvisor: bool = True
    tourvisor_public_search_url: str = ""
    log_level: str = "INFO"


settings = Settings()
