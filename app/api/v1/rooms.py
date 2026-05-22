import asyncio
import random
import string
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, WebSocket, Query
from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db, async_session
from app.api.deps import get_current_user
from app.core.security import decode_token
from app.models.user import User
from app.models.video import Video, Transcript
from app.models.room import RoomSession, RoomMember, RoomAnswer
from app.schemas.room import (
    CreateRoomRequest, CreateRoomResponse, JoinRoomResponse,
    RoomSnapshotResponse, RoomSnapshotMember,
)
from app.schemas.dictation import WordDiffItem
from app.services.dictation_service import compute_word_diff
from app.ws.room_manager import manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/rooms", tags=["rooms"])

_exam_tasks: dict[str, asyncio.Task] = {}


def _generate_room_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def _calc_time_bonus(started_at: datetime, submitted_at: datetime) -> float:
    elapsed = (submitted_at - started_at).total_seconds()
    max_bonus = 0.2
    window = 120.0
    return round(max(0.0, max_bonus * (1 - elapsed / window)), 4)


async def _get_room(db: AsyncSession, room_code: str) -> RoomSession:
    result = await db.execute(
        select(RoomSession).where(RoomSession.room_code == room_code)
    )
    room = result.scalar_one_or_none()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    return room


async def _get_member_count(db: AsyncSession, room_id: str) -> int:
    result = await db.execute(
        select(sa_func.count()).select_from(RoomMember).where(RoomMember.room_id == room_id)
    )
    return result.scalar_one()


async def _build_members_list(db: AsyncSession, room_id: str) -> list[dict]:
    result = await db.execute(
        select(RoomMember).where(RoomMember.room_id == room_id).order_by(RoomMember.joined_at)
    )
    members = result.scalars().all()
    return [
        {
            "user_id": m.user_id,
            "display_name": m.display_name,
            "is_ready": m.is_ready,
            "sentences_done": m.sentences_done,
            "total_score": m.total_score,
            "is_finished": m.finished_at is not None,
            "is_connected": m.is_connected,
        }
        for m in members
    ]


async def _build_rankings(db: AsyncSession, room_id: str) -> list[dict]:
    result = await db.execute(
        select(RoomMember)
        .where(RoomMember.room_id == room_id)
        .order_by(RoomMember.total_score.desc())
    )
    members = result.scalars().all()
    rankings = []
    for rank, m in enumerate(members, 1):
        rankings.append({
            "rank": rank,
            "user_id": m.user_id,
            "display_name": m.display_name,
            "total_score": round(m.total_score, 2),
            "sentences_done": m.sentences_done,
            "accuracy_pct": 0.0,
            "is_finished": m.finished_at is not None,
        })
    return rankings


async def _check_all_finished(db: AsyncSession, room_id: str) -> bool:
    result = await db.execute(
        select(sa_func.count()).select_from(RoomMember)
        .where(RoomMember.room_id == room_id, RoomMember.finished_at.is_(None))
    )
    return result.scalar_one() == 0


async def _end_game(room_code: str, reason: str):
    async with async_session() as db:
        room = await _get_room(db, room_code)
        if room.status != "active":
            return
        room.status = "finished"
        room.finished_at = datetime.now(timezone.utc)
        unfinished = await db.execute(
            select(RoomMember).where(
                RoomMember.room_id == room.id,
                RoomMember.finished_at.is_(None),
            )
        )
        for m in unfinished.scalars().all():
            m.finished_at = datetime.now(timezone.utc)
        await db.commit()
        rankings = await _build_rankings(db, room.id)
    await manager.broadcast(room_code, {
        "type": "game_end",
        "payload": {"reason": reason, "final_rankings": rankings},
    })
    _exam_tasks.pop(room_code, None)


async def _schedule_exam_end(room_code: str, duration_minutes: int):
    await asyncio.sleep(duration_minutes * 60)
    await _end_game(room_code, "time_up")


# ── HTTP Endpoints ────────────────────────────────────────────────────────────

@router.post("", response_model=CreateRoomResponse)
async def create_room(
    body: CreateRoomRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Video).where(Video.id == body.video_id))
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    room_code = _generate_room_code()
    room = RoomSession(
        room_code=room_code,
        host_user_id=user.id,
        video_id=body.video_id,
        max_players=body.max_players,
        max_replays=body.max_replays,
        exam_duration_minutes=body.exam_duration_minutes,
    )
    db.add(room)
    await db.flush()

    member = RoomMember(
        room_id=room.id,
        user_id=user.id,
        display_name=user.display_name,
        is_ready=True,
    )
    db.add(member)
    await db.commit()

    return CreateRoomResponse(room_code=room_code, room_id=room.id)


@router.post("/{room_code}/join", response_model=JoinRoomResponse)
async def join_room(
    room_code: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    room = await _get_room(db, room_code)

    if room.status != "waiting":
        raise HTTPException(status_code=403, detail="Game already started")

    member_count = await _get_member_count(db, room.id)
    if member_count >= room.max_players:
        raise HTTPException(status_code=400, detail="Room is full")

    existing = await db.execute(
        select(RoomMember).where(
            RoomMember.room_id == room.id,
            RoomMember.user_id == user.id,
        )
    )
    if not existing.scalar_one_or_none():
        member = RoomMember(
            room_id=room.id,
            user_id=user.id,
            display_name=user.display_name,
        )
        db.add(member)
        await db.commit()
        member_count += 1

        await manager.broadcast(room_code, {
            "type": "member_joined",
            "payload": {
                "user_id": user.id,
                "display_name": user.display_name,
                "member_count": member_count,
            },
        })

    return JoinRoomResponse(
        room_code=room.room_code,
        status=room.status,
        host_user_id=room.host_user_id,
        video_id=room.video_id,
        max_replays=room.max_replays,
        exam_duration_minutes=room.exam_duration_minutes,
        total_sentences=room.total_sentences,
        member_count=member_count,
    )


@router.get("/{room_code}", response_model=RoomSnapshotResponse)
async def get_room(
    room_code: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    room = await _get_room(db, room_code)
    members_data = await _build_members_list(db, room.id)
    return RoomSnapshotResponse(
        room_code=room.room_code,
        status=room.status,
        host_user_id=room.host_user_id,
        video_id=room.video_id,
        max_replays=room.max_replays,
        exam_duration_minutes=room.exam_duration_minutes,
        total_sentences=room.total_sentences,
        members=[RoomSnapshotMember(**m) for m in members_data],
    )


@router.delete("/{room_code}")
async def close_room(
    room_code: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    room = await _get_room(db, room_code)
    if room.host_user_id != user.id:
        raise HTTPException(status_code=403, detail="Only host can close the room")

    task = _exam_tasks.pop(room_code, None)
    if task:
        task.cancel()

    room.status = "finished"
    room.finished_at = datetime.now(timezone.utc)
    await db.commit()

    await manager.broadcast(room_code, {
        "type": "game_end",
        "payload": {"reason": "host_ended", "final_rankings": []},
    })

    return {"status": "closed"}


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

    room = await _get_room(db, room_code)

    member_result = await db.execute(
        select(RoomMember).where(
            RoomMember.room_id == room.id,
            RoomMember.user_id == user_id,
        )
    )
    member = member_result.scalar_one_or_none()
    if not member:
        await ws.close(code=4003)
        return

    await manager.connect(room_code, user_id, ws)
    member.is_connected = True
    await db.commit()

    try:
        members_data = await _build_members_list(db, room.id)
        await manager.send_personal(room_code, user_id, {
            "type": "room_state",
            "payload": {
                "status": room.status,
                "max_players": room.max_players,
                "max_replays": room.max_replays,
                "exam_duration_minutes": room.exam_duration_minutes,
                "host_user_id": room.host_user_id,
                "video_id": room.video_id,
                "total_sentences": room.total_sentences,
                "members": members_data,
                "your_sentences_done": member.sentences_done,
            },
        })

        await manager.broadcast(room_code, {
            "type": "member_status",
            "payload": {"user_id": user_id, "is_connected": True},
        })

        while True:
            data = await ws.receive_json()
            msg_type = data.get("type")

            if msg_type == "ready":
                await _handle_ready(db, room, member, room_code, user_id)

            elif msg_type == "start_game":
                await _handle_start_game(db, room, user_id, room_code)

            elif msg_type == "submit":
                await _handle_submit(db, room, member, user_id, room_code, data.get("payload", {}))

            elif msg_type == "force_end":
                if room.host_user_id == user_id:
                    await _end_game(room_code, "host_ended")

    except Exception:
        pass
    finally:
        await manager.disconnect(room_code, user_id)
        member.is_connected = False
        try:
            await db.commit()
        except Exception:
            pass

        await manager.broadcast(room_code, {
            "type": "member_status",
            "payload": {"user_id": user_id, "is_connected": False},
        })

        if room.host_user_id == user_id and room.status == "waiting":
            await _handle_host_disconnect(db, room, room_code)


async def _handle_ready(
    db: AsyncSession, room: RoomSession, member: RoomMember,
    room_code: str, user_id: str,
):
    member.is_ready = not member.is_ready
    await db.commit()
    await manager.broadcast(room_code, {
        "type": "member_ready",
        "payload": {"user_id": user_id, "is_ready": member.is_ready},
    })


async def _handle_start_game(
    db: AsyncSession, room: RoomSession, user_id: str, room_code: str,
):
    if room.host_user_id != user_id:
        return
    if room.status != "waiting":
        return

    transcript_result = await db.execute(
        select(Transcript)
        .where(Transcript.video_id == room.video_id)
        .order_by(Transcript.index)
    )
    transcripts = transcript_result.scalars().all()
    if not transcripts:
        await manager.send_personal(room_code, user_id, {
            "type": "error",
            "payload": {"message": "No transcripts found for this video"},
        })
        return

    video_result = await db.execute(select(Video).where(Video.id == room.video_id))
    video = video_result.scalar_one()

    now = datetime.now(timezone.utc)
    room.status = "active"
    room.started_at = now
    room.total_sentences = len(transcripts)
    await db.commit()

    exam_ends_at = None
    if room.exam_duration_minutes > 0:
        from datetime import timedelta
        exam_ends_at = (now + timedelta(minutes=room.exam_duration_minutes)).isoformat()
        task = asyncio.create_task(_schedule_exam_end(room_code, room.exam_duration_minutes))
        _exam_tasks[room_code] = task

    sentences = [
        {"index": t.index, "start_time": t.start_time, "end_time": t.end_time}
        for t in transcripts
    ]

    await manager.broadcast(room_code, {
        "type": "game_start",
        "payload": {
            "youtube_id": video.youtube_id,
            "total_sentences": len(transcripts),
            "max_replays": room.max_replays,
            "exam_duration_minutes": room.exam_duration_minutes,
            "exam_ends_at": exam_ends_at,
            "sentences": sentences,
        },
    })


async def _handle_submit(
    db: AsyncSession, room: RoomSession, member: RoomMember,
    user_id: str, room_code: str, payload: dict,
):
    if room.status != "active":
        return

    sentence_index = payload.get("sentence_index")
    user_input = payload.get("user_input", "")
    is_skipped = payload.get("is_skipped", False)
    replay_count = payload.get("replay_count", 0)

    if sentence_index is None:
        return

    if replay_count > room.max_replays:
        await manager.send_personal(room_code, user_id, {
            "type": "error",
            "payload": {"message": "Replay limit exceeded"},
        })
        return

    existing = await db.execute(
        select(RoomAnswer).where(
            RoomAnswer.room_id == room.id,
            RoomAnswer.user_id == user_id,
            RoomAnswer.sentence_index == sentence_index,
        )
    )
    if existing.scalar_one_or_none():
        await manager.send_personal(room_code, user_id, {
            "type": "error",
            "payload": {"message": "Already answered this sentence"},
        })
        return

    transcript_result = await db.execute(
        select(Transcript).where(
            Transcript.video_id == room.video_id,
            Transcript.index == sentence_index,
        )
    )
    transcript = transcript_result.scalar_one_or_none()
    if not transcript:
        return

    now = datetime.now(timezone.utc)

    if is_skipped:
        accuracy = 0.0
        time_bonus = 0.0
        word_diffs = []
    else:
        word_diffs_list, accuracy = compute_word_diff(user_input, transcript.text)
        word_diffs = [d.model_dump() for d in word_diffs_list]
        time_bonus = _calc_time_bonus(room.started_at, now)

    final_score = round(accuracy + time_bonus, 4)

    answer = RoomAnswer(
        room_id=room.id,
        user_id=user_id,
        sentence_index=sentence_index,
        user_input=user_input,
        is_skipped=is_skipped,
        accuracy_score=accuracy,
        time_bonus=time_bonus,
        final_score=final_score,
        replay_count=replay_count,
    )
    db.add(answer)

    member.total_score += final_score
    member.sentences_done += 1

    if member.sentences_done >= room.total_sentences:
        member.finished_at = now

    await db.commit()

    await manager.send_personal(room_code, user_id, {
        "type": "submit_result",
        "payload": {
            "sentence_index": sentence_index,
            "correct_text": transcript.text,
            "accuracy_score": accuracy,
            "time_bonus": time_bonus,
            "final_score": final_score,
            "is_skipped": is_skipped,
            "word_diffs": word_diffs,
        },
    })

    rankings = await _build_rankings(db, room.id)
    await manager.broadcast(room_code, {
        "type": "leaderboard_update",
        "payload": {"rankings": rankings},
    })

    if await _check_all_finished(db, room.id):
        task = _exam_tasks.pop(room_code, None)
        if task:
            task.cancel()
        await _end_game(room_code, "all_finished")


async def _handle_host_disconnect(
    db: AsyncSession, room: RoomSession, room_code: str,
):
    result = await db.execute(
        select(RoomMember)
        .where(RoomMember.room_id == room.id, RoomMember.user_id != room.host_user_id)
        .order_by(RoomMember.joined_at)
        .limit(1)
    )
    new_host = result.scalar_one_or_none()
    if new_host:
        room.host_user_id = new_host.user_id
        await db.commit()
        await manager.broadcast(room_code, {
            "type": "host_changed",
            "payload": {"new_host_user_id": new_host.user_id, "display_name": new_host.display_name},
        })
