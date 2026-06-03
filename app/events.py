import asyncio
import json
import logging
from typing import AsyncGenerator

import redis.asyncio as aioredis

from app.config import get_settings

logger = logging.getLogger(__name__)

CHANNEL = "video_updates"

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


async def publish_video_event(event_type: str, video_id: str, data: dict | None = None) -> None:
    if not _publisher:
        logger.warning("Redis publisher not initialized, skipping event")
        return
    payload = json.dumps({"type": event_type, "video_id": video_id, "data": data or {}})
    await _publisher.publish(CHANNEL, payload)


async def subscribe_video_events() -> AsyncGenerator[dict, None]:
    settings = get_settings()
    conn = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    pubsub = conn.pubsub()
    await pubsub.subscribe(CHANNEL)
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
        await pubsub.unsubscribe(CHANNEL)
        await pubsub.aclose()
        await conn.aclose()