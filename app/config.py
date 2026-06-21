import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

_INSECURE_SECRET_DEFAULT = "change-me-in-production-use-openssl-rand-hex-32"


class Settings(BaseSettings):
    # App
    APP_NAME: str = "DictaLearn API"
    # Deployment environment: "development" | "staging" | "production".
    ENVIRONMENT: str = "development"
    # DEBUG defaults OFF; enable explicitly via env for local development.
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    API_V1_PREFIX: str = "/api/v1"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/dictalearn"

    # JWT
    SECRET_KEY: str = _INSECURE_SECRET_DEFAULT
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # CORS — NoDecode disables pydantic-settings' automatic JSON decoding so the
    # validator below receives the raw env string and can accept both a JSON list
    # and a comma-separated string.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = [
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:3000",
        "https://dictalearn-react.nvquang176.workers.dev",
    ]

    # YouTube proxy — route youtube-transcript-api + yt-dlp through a (residential)
    # proxy to bypass datacenter-IP blocks ("RequestBlocked" / "not a bot").
    # Empty = disabled. Format: http://user:pass@host:port
    YT_PROXY_URL: str = ""

    # Gemini
    GEMINI_API_KEY: str = ""

    # AssemblyAI
    ASSEMBLYAI_API_KEY: str = ""

    # Google Cloud (Translation API)
    GOOGLE_APPLICATION_CREDENTIALS: str = ""

    # Google OAuth
    GOOGLE_CLIENT_ID: str = ""

    # Celery / Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = ""
    CELERY_RESULT_BACKEND: str = ""

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.strip().lower() in ("production", "prod")

    @property
    def celery_broker(self) -> str:
        return self.CELERY_BROKER_URL or self.REDIS_URL

    @property
    def celery_backend(self) -> str:
        return self.CELERY_RESULT_BACKEND or self.REDIS_URL

    @property
    def DATABASE_URL_SYNC(self) -> str:
        """Sync driver URL for Celery workers (asyncpg → psycopg2)."""
        return self.DATABASE_URL.replace("+asyncpg", "+psycopg2")

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _parse_cors_origins(cls, value):
        """Accept a comma-separated string from .env as well as a JSON list."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return json.loads(stripped)
            return [origin.strip() for origin in stripped.split(",") if origin.strip()]
        return value

    @model_validator(mode="after")
    def _validate_production_secrets(self):
        """Fail fast in production rather than booting with an insecure secret."""
        if self.is_production and self.SECRET_KEY == _INSECURE_SECRET_DEFAULT:
            raise ValueError(
                "SECRET_KEY must be set to a strong value in production "
                "(generate with: openssl rand -hex 32)."
            )
        return self

    model_config = {"env_file": str(_ENV_FILE), "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
