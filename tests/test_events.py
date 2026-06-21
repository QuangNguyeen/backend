"""Tests for best-effort Redis recommendation events."""

import pytest

from app import events


@pytest.mark.asyncio
async def test_publish_user_event_is_best_effort(monkeypatch):
    class FailingPublisher:
        async def publish(self, channel, payload):
            raise RuntimeError("redis unavailable")

    monkeypatch.setattr(events, "_publisher", FailingPublisher())

    await events.publish_user_recommendation_event(
        "user-1",
        "attempt_completed",
        "video-1",
    )


@pytest.mark.asyncio
async def test_recommendation_subscription_uses_user_and_catalog_channels(monkeypatch):
    subscribed_channels = []

    async def fake_subscribe(channels):
        subscribed_channels.extend(channels)
        yield {
            "type": "video.updated",
            "event_id": "event-1",
            "video_id": "video-1",
            "occurred_at": "2026-06-07T00:00:00+00:00",
        }
        yield {
            "type": "recommendations.changed",
            "event_id": "event-2",
            "reason": "attempt_completed",
            "video_id": "video-2",
            "occurred_at": "2026-06-07T00:01:00+00:00",
        }

    monkeypatch.setattr(events, "_subscribe_events", fake_subscribe)

    received = []
    async for event in events.subscribe_recommendation_events("user-1"):
        received.append(event)

    assert subscribed_channels == [
        events.VIDEO_CHANNEL,
        f"{events.RECOMMENDATION_USER_CHANNEL_PREFIX}user-1",
    ]
    assert received == [
        {
            "event_id": "event-1",
            "reason": "video.updated",
            "video_id": "video-1",
            "occurred_at": "2026-06-07T00:00:00+00:00",
        },
        {
            "event_id": "event-2",
            "reason": "attempt_completed",
            "video_id": "video-2",
            "occurred_at": "2026-06-07T00:01:00+00:00",
        },
    ]
