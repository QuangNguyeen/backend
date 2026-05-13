from pathlib import Path
from functools import lru_cache

from pydantic_settings import BaseSettings

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    # App
    APP_NAME: str = "DictaLearn API"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/dictalearn"

    # JWT
    SECRET_KEY: str = "change-me-in-production-use-openssl-rand-hex-32"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://localhost:5174", "http://localhost:3000"]

    # YouTube
    YOUTUBE_API_KEY: str = ""

    # Gemini
    GEMINI_API_KEY: str = ""

    # Google Cloud (Translation API)
    GOOGLE_APPLICATION_CREDENTIALS: str = ""

    # Google Cloud STT / GCS
    GCP_PROJECT_ID: str = ""
    GCS_BUCKET_NAME: str = ""

    # Google OAuth
    GOOGLE_CLIENT_ID: str = ""

    # Celery / Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = ""
    CELERY_RESULT_BACKEND: str = ""

    @property
    def celery_broker(self) -> str:
        return self.CELERY_BROKER_URL or self.REDIS_URL

    @property
    def celery_backend(self) -> str:
        return self.CELERY_RESULT_BACKEND or self.REDIS_URL

    # Sync DB URL for Celery workers (replace asyncpg with psycopg2)
    @property
    def DATABASE_URL_SYNC(self) -> str:
        return self.DATABASE_URL.replace("+asyncpg", "+psycopg2").replace("postgresql+psycopg2", "postgresql+psycopg2")

    model_config = {"env_file": str(_ENV_FILE), "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
