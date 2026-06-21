import logging

from fastapi import APIRouter, Depends, Query, WebSocket
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.security import decode_token
from app.database import get_db
from app.models.user import User
from app.repositories.room_repository import RoomRepository
from app.schemas.room import (
    CreateRoomRequest,
    CreateRoomResponse,
    JoinRoomResponse,
    RoomSnapshotResponse,
)
from app.services.room_service import RoomService, end_game
from app.ws.room_manager import manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/rooms", tags=["rooms"])


def get_room_service(db: AsyncSession = Depends(get_db)) -> RoomService:
    return RoomService(RoomRepository(db))


# ── HTTP Endpoints ────────────────────────────────────────────────────────────


@router.post("", response_model=CreateRoomResponse)
async def create_room(
    body: CreateRoomRequest,
    user: User = Depends(get_current_user),
    service: RoomService = Depends(get_room_service),
):
    return await service.create_room(user, body)


@router.post("/{room_code}/join", response_model=JoinRoomResponse)
async def join_room(
    room_code: str,
    user: User = Depends(get_current_user),
    service: RoomService = Depends(get_room_service),
):
    return await service.join_room(user, room_code)


@router.get("/{room_code}", response_model=RoomSnapshotResponse)
async def get_room(
    room_code: str,
    user: User = Depends(get_current_user),
    service: RoomService = Depends(get_room_service),
):
    return await service.get_room_snapshot(room_code)


@router.delete("/{room_code}")
async def close_room(
    room_code: str,
    user: User = Depends(get_current_user),
    service: RoomService = Depends(get_room_service),
):
    return await service.close_room(user, room_code)


# ── WebSocket Endpoint ────────────────────────────────────────────────────────


@router.websocket("/ws/{room_code}")
async def room_ws(
    room_code: str,
    ws: WebSocket,
    token: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        await ws.close(code=4001)
        return

    user_id = payload["sub"]
    service = RoomService(RoomRepository(db))

    room = await service.get_room_or_404(room_code)

    member = await service.repo.get_member(room.id, user_id)
    if not member:
        await ws.close(code=4003)
        return

    await manager.connect(room_code, user_id, ws)
    await service.mark_connected(member)

    try:
        await service.announce_connect(room, member, room_code, user_id)

        while True:
            data = await ws.receive_json()
            msg_type = data.get("type")

            if msg_type == "ready":
                await service.handle_ready(room, member, room_code, user_id)

            elif msg_type == "start_game":
                await service.handle_start_game(room, user_id, room_code)

            elif msg_type == "submit":
                await service.handle_submit(
                    room, member, user_id, room_code, data.get("payload", {})
                )

            elif msg_type in ("early_exit", "leave"):
                await service.handle_early_exit(room, member, room_code, user_id)

            elif msg_type == "force_end":
                if room.host_user_id == user_id:
                    await end_game(room_code, "host_ended")

    except Exception:
        pass
    finally:
        await manager.disconnect(room_code, user_id)
        await service.announce_disconnect(member, room_code, user_id)

        if room.host_user_id == user_id and room.status == "waiting":
            await service.handle_host_disconnect(room, room_code)