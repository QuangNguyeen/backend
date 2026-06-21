import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from uuid import uuid4

import redis.asyncio as aioredis

from app.config import get_settings

logger = logging.getLogger(__name__)

VIDEO_CHANNEL = "video_updates"
RECOMMENDATION_USER_CHANNEL_PREFIX = "recommendations:user:"

_publisher: aioredis.Redis | None = None


async def init_publisher() -> None:
    global _publisher
    settings = get_settings()
    _publisher = aioredis.from_url(settings.REDIS_URL, decode_responses=True)


async def close_publisher() -> None:
    global _publisher
    if _publisher:
        await _publisher.aclose()
        _publisher = None


def _event_payload(
    event_type: str,
    *,
    video_id: str | None = None,
    reason: str | None = None,
    data: dict | None = None,
) -> dict:
    return {
        "type": event_type,
        "event_id": str(uuid4()),
        "reason": reason or event_type,
        "video_id": video_id,
        "occurred_at": datetime.now(UTC).isoformat(),
        "data": data or {},
    }


async def _publish(channel: str, payload: dict) -> None:
    if not _publisher:
        logger.warning("Redis publisher not initialized, skipping event")
        return
    try:
        await _publisher.publish(channel, json.dumps(payload))
    except Exception:
        logger.exception("Failed to publish Redis event to %s", channel)


async def publish_video_event(
    event_type: str, video_id: str | None, data: dict | None = None
) -> None:
    await _publish(
        VIDEO_CHANNEL,
        _event_payload(event_type, video_id=video_id, data=data),
    )


async def publish_user_recommendation_event(
    user_id: str,
    reason: str,
    video_id: str | None = None,
) -> None:
    await _publish(
        f"{RECOMMENDATION_USER_CHANNEL_PREFIX}{user_id}",
        _event_payload(
            "recommendations.changed",
            video_id=video_id,
            reason=reason,
        ),
    )


def publish_video_event_sync(
    event_type: str, video_id: str, data: dict | None = None
) -> None:
    settings = get_settings()
    payload = _event_payload(event_type, video_id=video_id, data=data)
    try:
        import redis

        client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        try:
            client.publish(VIDEO_CHANNEL, json.dumps(payload))
        finally:
            client.close()
    except Exception:
        logger.exception("Failed to publish synchronous Redis event")


async def _subscribe_events(channels: list[str]) -> AsyncGenerator[dict, None]:
    settings = get_settings()
    conn = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    pubsub = conn.pubsub()
    await pubsub.subscribe(*channels)
    try:
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg and msg["type"] == "message":
                try:
                    yield json.loads(msg["data"])
                except json.JSONDecodeError:
                    continue
            else:
                await asyncio.sleep(0.1)
    finally:
        await pubsub.unsubscribe(*channels)
        await pubsub.aclose()
        await conn.aclose()


async def subscribe_video_events() -> AsyncGenerator[dict, None]:
    async for event in _subscribe_events([VIDEO_CHANNEL]):
        yield event


async def subscribe_recommendation_events(user_id: str) -> AsyncGenerator[dict, None]:
    channels = [
        VIDEO_CHANNEL,
        f"{RECOMMENDATION_USER_CHANNEL_PREFIX}{user_id}",
    ]
    async for event in _subscribe_events(channels):
        yield {
            "event_id": event.get("event_id") or str(uuid4()),
            "reason": event.get("reason") or event.get("type") or "catalog_changed",
            "video_id": event.get("video_id"),
            "occurred_at": event.get("occurred_at") or datetime.now(UTC).isoformat(),
        }
