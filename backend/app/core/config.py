"""
Application configuration using Pydantic Settings.

This module reads environment variables and validates them at startup.
If a required variable is missing, the app will fail fast with a clear error.

Usage:
    from app.core.config import settings
    print(settings.DATABASE_URL)
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # --- Application ---
    APP_ENV: str = "development"
    APP_VERSION: str = "0.1.0"
    LOG_LEVEL: str = "INFO"

    # --- Backend ---
    BACKEND_HOST: str = "0.0.0.0"
    BACKEND_PORT: int = 8000
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]
    CORS_ORIGIN_REGEX: str | None = (
        r"http://(localhost|127\.0\.0\.1|0\.0\.0\.0|192\.168\.\d{1,3}\.\d{1,3}):\d+"
    )

    # --- PostgreSQL ---
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "nlsql"
    POSTGRES_PASSWORD: str = "nlsql_dev_password"
    POSTGRES_DB: str = "nlsql_agent"

    @property
    def DATABASE_URL(self) -> str:
        """Build the async PostgreSQL connection string."""
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def DATABASE_URL_SYNC(self) -> str:
        """Build a sync PostgreSQL connection string (no +asyncpg)."""
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    # --- Redis ---
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379

    @property
    def REDIS_URL(self) -> str:
        """Build the Redis connection string."""
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/0"

    # --- OpenAI / LLM ---
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o"
    OPENAI_API_BASE: str = "https://api.openai.com/v1"
    LLM_TIMEOUT_SECONDS: int = 60
    LLM_TEMPERATURE: float = 0.1

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent.parent / ".env",
        case_sensitive=True,
    )


# Global settings instance — import this throughout the app
settings = Settings()
