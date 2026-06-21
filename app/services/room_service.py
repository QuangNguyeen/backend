"""Business logic for multiplayer rooms: lobby, realtime game, and scoring.

HTTP endpoints and WebSocket message handlers both delegate here. The service
coordinates the repository, the connection ``manager`` (broadcasts), and the
per-room exam-timer background tasks.
"""

import asyncio
import logging
import random
import string
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from app.database import async_session
from app.models.room import RoomAnswer, RoomMember, RoomSession
from app.repositories.room_repository import RoomRepository
from app.schemas.room import (
    CreateRoomRequest,
    CreateRoomResponse,
    JoinRoomResponse,
    RoomSnapshotMember,
    RoomSnapshotResponse,
)
from app.services.dictation_service import compute_word_diff
from app.ws.room_manager import manager

logger = logging.getLogger(__name__)

# Per-room exam-timer tasks (process-local, keyed by room_code).
_exam_tasks: dict[str, asyncio.Task] = {}


def _generate_room_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def _calc_time_bonus(started_at: datetime, submitted_at: datetime) -> float:
    elapsed = (submitted_at - started_at).total_seconds()
    max_bonus = 0.2
    window = 120.0
    return round(max(0.0, max_bonus * (1 - elapsed / window)), 4)


async def end_game(room_code: str, reason: str) -> None:
    """Finish an active room and broadcast final rankings (own DB session)."""
    async with async_session() as db:
        service = RoomService(RoomRepository(db))
        room = await service.get_room_or_404(room_code)
        if room.status != "active":
            return
        room.status = "finished"
        room.finished_at = datetime.now(UTC)
        for m in await service.repo.get_unfinished_members(room.id):
            m.finished_at = datetime.now(UTC)
        await service.repo.commit()
        rankings = await service._build_rankings(room.id)
    await manager.broadcast(
        room_code,
        {"type": "game_end", "payload": {"reason": reason, "final_rankings": rankings}},
    )
    _exam_tasks.pop(room_code, None)


async def schedule_exam_end(room_code: str, duration_minutes: int) -> None:
    await asyncio.sleep(duration_minutes * 60)
    await end_game(room_code, "time_up")


class RoomService:
    def __init__(self, repo: RoomRepository):
        self.repo = repo

    # ── Shared helpers ───────────────────────────────────────────────────────

    async def get_room_or_404(self, room_code: str) -> RoomSession:
        room = await self.repo.get_room(room_code)
        if not room:
            raise HTTPException(status_code=404, detail="Room not found")
        return room

    async def _build_members_list(self, room_id: str) -> list[dict]:
        members = await self.repo.get_members_by_joined(room_id)
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

    async def _build_rankings(self, room_id: str) -> list[dict]:
        members = await self.repo.get_members_by_score(room_id)
        rankings = []
        for rank, m in enumerate(members, 1):
            rankings.append(
                {
                    "rank": rank,
                    "user_id": m.user_id,
                    "display_name": m.display_name,
                    "total_score": round(m.total_score, 2),
                    "sentences_done": m.sentences_done,
                    "accuracy_pct": 0.0,
                    "is_finished": m.finished_at is not None,
                }
            )
        return rankings

    async def _check_all_finished(self, room_id: str) -> bool:
        return await self.repo.count_unfinished(room_id) == 0

    # ── HTTP endpoints ───────────────────────────────────────────────────────

    async def create_room(self, user, body: CreateRoomRequest) -> CreateRoomResponse:
        video = await self.repo.get_video(body.video_id)
        if not video:
            raise HTTPException(status_code=404, detail="Video not found")
        publish_status = getattr(video, "publish_status", None) or "published"
        if (
            publish_status != "published"
            and not user.is_admin
            and not await self.repo.has_user_practice(user.id, body.video_id)
        ):
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
        self.repo.add(room)
        await self.repo.flush()

        member = RoomMember(
            room_id=room.id,
            user_id=user.id,
            display_name=user.display_name,
            is_ready=True,
        )
        self.repo.add(member)
        await self.repo.commit()

        return CreateRoomResponse(room_code=room_code, room_id=room.id)

    async def join_room(self, user, room_code: str) -> JoinRoomResponse:
        room = await self.get_room_or_404(room_code)

        if room.status != "waiting":
            raise HTTPException(status_code=403, detail="Game already started")

        member_count = await self.repo.get_member_count(room.id)
        if member_count >= room.max_players:
            raise HTTPException(status_code=400, detail="Room is full")

        existing = await self.repo.get_member(room.id, user.id)
        if not existing:
            member = RoomMember(
                room_id=room.id,
                user_id=user.id,
                display_name=user.display_name,
            )
            self.repo.add(member)
            await self.repo.commit()
            member_count += 1

            await manager.broadcast(
                room_code,
                {
                    "type": "member_joined",
                    "payload": {
                        "user_id": user.id,
                        "display_name": user.display_name,
                        "member_count": member_count,
                    },
                },
            )

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

    async def get_room_snapshot(self, room_code: str) -> RoomSnapshotResponse:
        room = await self.get_room_or_404(room_code)
        members_data = await self._build_members_list(room.id)
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

    async def close_room(self, user, room_code: str) -> dict:
        room = await self.get_room_or_404(room_code)
        if room.host_user_id != user.id:
            raise HTTPException(status_code=403, detail="Only host can close the room")

        task = _exam_tasks.pop(room_code, None)
        if task:
            task.cancel()

        room.status = "finished"
        room.finished_at = datetime.now(UTC)
        await self.repo.commit()

        await manager.broadcast(
            room_code,
            {"type": "game_end", "payload": {"reason": "host_ended", "final_rankings": []}},
        )

        return {"status": "closed"}

    # ── WebSocket connection lifecycle ───────────────────────────────────────

    async def mark_connected(self, member: RoomMember) -> None:
        member.is_connected = True
        await self.repo.commit()

    async def announce_connect(
        self, room: RoomSession, member: RoomMember, room_code: str, user_id: str
    ) -> None:
        members_data = await self._build_members_list(room.id)
        await manager.send_personal(
            room_code,
            user_id,
            {
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
            },
        )
        await manager.broadcast(
            room_code,
            {"type": "member_status", "payload": {"user_id": user_id, "is_connected": True}},
        )

    async def announce_disconnect(self, member: RoomMember, room_code: str, user_id: str) -> None:
        member.is_connected = False
        try:
            await self.repo.commit()
        except Exception:
            pass
        await manager.broadcast(
            room_code,
            {"type": "member_status", "payload": {"user_id": user_id, "is_connected": False}},
        )

    # ── WebSocket message handlers ───────────────────────────────────────────

    async def handle_ready(
        self, room: RoomSession, member: RoomMember, room_code: str, user_id: str
    ) -> None:
        member.is_ready = not member.is_ready
        await self.repo.commit()
        await manager.broadcast(
            room_code,
            {"type": "member_ready", "payload": {"user_id": user_id, "is_ready": member.is_ready}},
        )

    async def handle_start_game(self, room: RoomSession, user_id: str, room_code: str) -> None:
        if room.host_user_id != user_id:
            return
        if room.status != "waiting":
            return

        transcripts = await self.repo.get_transcripts_ordered(room.video_id)
        if not transcripts:
            await manager.send_personal(
                room_code,
                user_id,
                {"type": "error", "payload": {"message": "No transcripts found for this video"}},
            )
            return

        video = await self.repo.get_video(room.video_id)

        now = datetime.now(UTC)
        room.status = "active"
        room.started_at = now
        room.total_sentences = len(transcripts)
        await self.repo.commit()

        exam_ends_at = None
        if room.exam_duration_minutes > 0:
            exam_ends_at = (now + timedelta(minutes=room.exam_duration_minutes)).isoformat()
            task = asyncio.create_task(schedule_exam_end(room_code, room.exam_duration_minutes))
            _exam_tasks[room_code] = task

        sentences = [
            {"index": t.index, "start_time": t.start_time, "end_time": t.end_time}
            for t in transcripts
        ]

        await manager.broadcast(
            room_code,
            {
                "type": "game_start",
                "payload": {
                    "youtube_id": video.youtube_id,
                    "total_sentences": len(transcripts),
                    "max_replays": room.max_replays,
                    "exam_duration_minutes": room.exam_duration_minutes,
                    "exam_ends_at": exam_ends_at,
                    "sentences": sentences,
                },
            },
        )

    async def handle_submit(
        self,
        room: RoomSession,
        member: RoomMember,
        user_id: str,
        room_code: str,
        payload: dict,
    ) -> None:
        if room.status != "active":
            return

        sentence_index = payload.get("sentence_index")
        user_input = payload.get("user_input", "")
        is_skipped = payload.get("is_skipped", False)
        replay_count = payload.get("replay_count", 0)

        if sentence_index is None:
            return

        if replay_count > room.max_replays:
            await manager.send_personal(
                room_code,
                user_id,
                {"type": "error", "payload": {"message": "Replay limit exceeded"}},
            )
            return

        if await self.repo.get_answer(room.id, user_id, sentence_index):
            await manager.send_personal(
                room_code,
                user_id,
                {"type": "error", "payload": {"message": "Already answered this sentence"}},
            )
            return

        transcript = await self.repo.get_transcript_by_index(room.video_id, sentence_index)
        if not transcript:
            return

        now = datetime.now(UTC)

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
        self.repo.add(answer)

        member.total_score += final_score
        member.sentences_done += 1

        if member.sentences_done >= room.total_sentences:
            member.finished_at = now

        await self.repo.commit()

        await manager.send_personal(
            room_code,
            user_id,
            {
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
            },
        )

        rankings = await self._build_rankings(room.id)
        await manager.broadcast(
            room_code,
            {"type": "leaderboard_update", "payload": {"rankings": rankings}},
        )

        if await self._check_all_finished(room.id):
            task = _exam_tasks.pop(room_code, None)
            if task:
                task.cancel()
            await end_game(room_code, "all_finished")

    async def handle_early_exit(
        self, room: RoomSession, member: RoomMember, room_code: str, user_id: str
    ) -> None:
        """A player chooses to leave before completing every sentence.

        Marks them finished so their score is locked into the rankings and the
        room can auto-end once no active members remain. Without this, an
        early-leaving player stays ``finished_at IS NULL`` forever and the game
        can only end via the exam timer or host action.
        """
        if room.status != "active":
            return

        if member.finished_at is None:
            member.finished_at = datetime.now(UTC)
            await self.repo.commit()

        await manager.send_personal(
            room_code,
            user_id,
            {"type": "exit_confirmed", "payload": {"user_id": user_id}},
        )
        await manager.broadcast(
            room_code,
            {
                "type": "member_finished",
                "payload": {"user_id": user_id, "reason": "early_exit"},
            },
        )

        rankings = await self._build_rankings(room.id)
        await manager.broadcast(
            room_code,
            {"type": "leaderboard_update", "payload": {"rankings": rankings}},
        )

        if await self._check_all_finished(room.id):
            task = _exam_tasks.pop(room_code, None)
            if task:
                task.cancel()
            await end_game(room_code, "all_finished")

    async def handle_host_disconnect(self, room: RoomSession, room_code: str) -> None:
        new_host = await self.repo.get_next_host_candidate(room.id, room.host_user_id)
        if new_host:
            room.host_user_id = new_host.user_id
            await self.repo.commit()
            await manager.broadcast(
                room_code,
                {
                    "type": "host_changed",
                    "payload": {
                        "new_host_user_id": new_host.user_id,
                        "display_name": new_host.display_name,
                    },
                },
            )
