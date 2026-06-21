"""Tests for Phase 0 foundations: config, health, error handling, correlation id."""

import pytest
from starlette.testclient import TestClient

from app.config import Settings
from app.main import app


@pytest.fixture
def client():
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ── Config ────────────────────────────────────────────────────────────────


def test_cors_origins_accepts_comma_separated_string():
    settings = Settings(CORS_ORIGINS="http://a.com, http://b.com")
    assert settings.CORS_ORIGINS == ["http://a.com", "http://b.com"]


def test_cors_origins_accepts_json_list():
    settings = Settings(CORS_ORIGINS='["http://a.com","http://b.com"]')
    assert settings.CORS_ORIGINS == ["http://a.com", "http://b.com"]


def test_production_rejects_insecure_secret_key():
    with pytest.raises(ValueError):
        Settings(
            ENVIRONMENT="production",
            SECRET_KEY="change-me-in-production-use-openssl-rand-hex-32",
        )


def test_production_accepts_strong_secret_key():
    settings = Settings(ENVIRONMENT="production", SECRET_KEY="a" * 64)
    assert settings.is_production is True


def test_sync_database_url_swaps_driver():
    settings = Settings(
        DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/db",
        SECRET_KEY="x" * 64,
    )
    assert settings.DATABASE_URL_SYNC == "postgresql+psycopg2://u:p@localhost:5432/db"


# ── Health & correlation id ─────────────────────────────────────────────────


def test_health_liveness(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_response_carries_request_id_header(client):
    resp = client.get("/health")
    assert resp.headers.get("X-Request-ID")


def test_inbound_request_id_is_echoed(client):
    resp = client.get("/health", headers={"X-Request-ID": "trace-abc"})
    assert resp.headers.get("X-Request-ID") == "trace-abc"


# ── Error handling ──────────────────────────────────────────────────────────


def test_unhandled_exception_is_sanitized(client):
    @app.get("/_test_boom")
    def _boom():
        raise RuntimeError("super-secret-internal-detail")

    resp = client.get("/_test_boom", headers={"X-Request-ID": "trace-err"})
    assert resp.status_code == 500
    body = resp.json()
    # Generic client-facing message; never the raw traceback.
    assert body["detail"] == "Internal server error"
    # Correlation id survives the error path.
    assert resp.headers.get("X-Request-ID") == "trace-err"