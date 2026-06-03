import asyncio
import logging

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from app.events import subscribe_video_events

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["Events"])


@router.get("/videos")
async def video_events(request: Request):
    async def stream():
        async for event in subscribe_video_events():
            if await request.is_disconnected():
                break
            yield {"event": event["type"], "data": event}

    return EventSourceResponse(stream())