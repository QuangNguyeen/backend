"""Protected endpoints must reject unauthenticated requests with 401."""

import pytest

PROTECTED_GET_ENDPOINTS = [
    "/api/v1/auth/me",
    "/api/v1/users/me",
    "/api/v1/dashboard/full",
    "/api/v1/dashboard/history",
    "/api/v1/dashboard/weak-words",
    "/api/v1/videos/recommendations",
    "/api/v1/events/recommendations",
    "/api/v1/vocabulary",
    "/api/v1/vocabulary/due",
    "/api/v1/admin/stats",
    "/api/v1/admin/users",
    "/api/v1/admin/analytics/content-health",
]


@pytest.mark.parametrize("path", PROTECTED_GET_ENDPOINTS)
@pytest.mark.asyncio
async def test_protected_endpoint_requires_auth(client, path):
    resp = await client.get(path)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_public_video_list_does_not_require_auth(client, app):
    # The catalogue listing is intentionally public — should not 401.
    from app.api.v1.videos import get_video_service

    class StubVideoService:
        async def list_videos(self, *a, **k):
            return {"items": [], "total": 0, "page": 1, "total_pages": 1}

    app.dependency_overrides[get_video_service] = lambda: StubVideoService()
    resp = await client.get("/api/v1/videos")
    assert resp.status_code == 200
