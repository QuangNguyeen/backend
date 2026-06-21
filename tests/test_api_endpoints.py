"""Endpoint-level tests: full router → service stack with overridden services.

Verifies routing, response_model serialization, and domain-exception → HTTP
status mapping end to end (through the global exception handlers), no DB.
"""

from datetime import UTC, datetime

import pytest

from app.core.exceptions import ForbiddenError, NotFoundError
from app.schemas.user import UserPreferences, UserProfileResponse, UserStatsBlock

# ── Happy path + serialization (users) ──────────────────────────────────────


def _profile() -> UserProfileResponse:
    return UserProfileResponse(
        id="user-1",
        email="user@example.com",
        display_name="Test User",
        is_admin=False,
        preferred_language="en",
        preferences=UserPreferences(),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        stats=UserStatsBlock(
            total_attempts=3,
            average_score=82.5,
            total_vocabulary=12,
            current_streak=1,
            longest_streak=4,
        ),
    )


@pytest.mark.asyncio
async def test_get_my_profile_returns_serialized_payload(client, app, auth_as):
    from app.api.v1.users import get_user_service

    auth_as()

    class StubUserService:
        async def get_profile(self, user):
            return _profile()

    app.dependency_overrides[get_user_service] = lambda: StubUserService()

    resp = await client.get("/api/v1/users/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "user@example.com"
    assert body["stats"]["average_score"] == 82.5
    assert body["preferences"] == {"audio_speed": 1.0, "theme": "system"}
    # Correlation id header is attached by middleware.
    assert resp.headers.get("X-Request-ID")


# ── Domain-exception → HTTP status mapping ───────────────────────────────────


@pytest.mark.asyncio
async def test_video_not_found_maps_to_404(client, app):
    from app.api.v1.videos import get_video_service

    class StubVideoService:
        async def get_video(self, user, video_id):
            raise NotFoundError("Video not found")

    app.dependency_overrides[get_video_service] = lambda: StubVideoService()

    resp = await client.get("/api/v1/videos/does-not-exist")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Video not found"}


@pytest.mark.asyncio
async def test_video_delete_forbidden_maps_to_403(client, app, auth_as):
    from app.api.v1.videos import get_video_service

    auth_as()

    class StubVideoService:
        async def delete_video(self, user, video_id):
            raise ForbiddenError("You can only delete videos you created")

    app.dependency_overrides[get_video_service] = lambda: StubVideoService()

    resp = await client.delete("/api/v1/videos/v1")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "You can only delete videos you created"


# ── Request validation (422) ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_invalid_email_returns_422(client):
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "not-an-email",
            "password": "secret123",
            "display_name": "X",
            "preferred_language": "en",
        },
    )
    assert resp.status_code == 422
