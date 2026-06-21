import json
import logging

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from app.api.deps import get_current_user
from app.events import subscribe_recommendation_events, subscribe_video_events
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["Events"])


@router.get("/videos")
async def video_events(request: Request):
    async def stream():
        async for event in subscribe_video_events():
            if await request.is_disconnected():
                break
            yield {"event": event["type"], "data": json.dumps(event)}

    return EventSourceResponse(stream())


@router.get("/recommendations")
async def recommendation_events(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    async def stream():
        yield {"comment": "connected", "retry": 3000}
        async for event in subscribe_recommendation_events(current_user.id):
            if await request.is_disconnected():
                break
            yield {
                "event": "recommendations.changed",
                "data": json.dumps(event),
                "retry": 3000,
            }

    return EventSourceResponse(
        stream(),
        ping=15,
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
