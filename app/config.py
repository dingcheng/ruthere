from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    app_name: str = "RUThere"
    base_url: str = "http://localhost:8000"
    secret_key: str = "change-me-to-a-random-64-char-string"

    # Vault encryption (base64-encoded 32-byte key)
    vault_key: str = "change-me-generate-a-real-key"

    # Database
    database_url: str = "sqlite+aiosqlite:///./ruthere.db"

    # ntfy.sh
    ntfy_base_url: str = "https://ntfy.sh"

    # Resend (email)
    resend_api_key: str = ""
    email_from: str = "heartbeat@yourdomain.com"

    # Heartbeat defaults
    default_heartbeat_interval_hours: int = 24
    default_missed_threshold: int = 3
    default_response_window_hours: int = 4

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
