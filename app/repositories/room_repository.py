"""Database access for multiplayer rooms, members, and answers."""

from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.room import RoomAnswer, RoomMember, RoomSession
from app.models.video import Transcript, UserPracticeVideo, Video


class RoomRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── Rooms ───────────────────────────────────────────────────────────────

    async def get_room(self, room_code: str) -> RoomSession | None:
        result = await self.db.execute(
            select(RoomSession).where(RoomSession.room_code == room_code)
        )
        return result.scalar_one_or_none()

    # ── Members ─────────────────────────────────────────────────────────────

    async def get_member(self, room_id: str, user_id: str) -> RoomMember | None:
        result = await self.db.execute(
            select(RoomMember).where(
                RoomMember.room_id == room_id,
                RoomMember.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_member_count(self, room_id: str) -> int:
        result = await self.db.execute(
            select(sa_func.count()).select_from(RoomMember).where(RoomMember.room_id == room_id)
        )
        return result.scalar_one()

    async def get_members_by_joined(self, room_id: str) -> list[RoomMember]:
        result = await self.db.execute(
            select(RoomMember).where(RoomMember.room_id == room_id).order_by(RoomMember.joined_at)
        )
        return list(result.scalars().all())

    async def get_members_by_score(self, room_id: str) -> list[RoomMember]:
        result = await self.db.execute(
            select(RoomMember)
            .where(RoomMember.room_id == room_id)
            .order_by(RoomMember.total_score.desc())
        )
        return list(result.scalars().all())

    async def get_unfinished_members(self, room_id: str) -> list[RoomMember]:
        result = await self.db.execute(
            select(RoomMember).where(
                RoomMember.room_id == room_id,
                RoomMember.finished_at.is_(None),
            )
        )
        return list(result.scalars().all())

    async def count_unfinished(self, room_id: str) -> int:
        result = await self.db.execute(
            select(sa_func.count())
            .select_from(RoomMember)
            .where(RoomMember.room_id == room_id, RoomMember.finished_at.is_(None))
        )
        return result.scalar_one()

    async def get_next_host_candidate(self, room_id: str, host_user_id: str) -> RoomMember | None:
        result = await self.db.execute(
            select(RoomMember)
            .where(RoomMember.room_id == room_id, RoomMember.user_id != host_user_id)
            .order_by(RoomMember.joined_at)
            .limit(1)
        )
        return result.scalar_one_or_none()

    # ── Video / transcripts ─────────────────────────────────────────────────

    async def get_video(self, video_id: str) -> Video | None:
        result = await self.db.execute(select(Video).where(Video.id == video_id))
        return result.scalar_one_or_none()

    async def has_user_practice(self, user_id: str, video_id: str) -> bool:
        result = await self.db.execute(
            select(UserPracticeVideo.id)
            .where(
                UserPracticeVideo.user_id == user_id,
                UserPracticeVideo.video_id == video_id,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def get_transcripts_ordered(self, video_id: str) -> list[Transcript]:
        result = await self.db.execute(
            select(Transcript).where(Transcript.video_id == video_id).order_by(Transcript.index)
        )
        return list(result.scalars().all())

    async def get_transcript_by_index(self, video_id: str, index: int) -> Transcript | None:
        result = await self.db.execute(
            select(Transcript).where(
                Transcript.video_id == video_id,
                Transcript.index == index,
            )
        )
        return result.scalar_one_or_none()

    # ── Answers ─────────────────────────────────────────────────────────────

    async def get_answer(
        self, room_id: str, user_id: str, sentence_index: int
    ) -> RoomAnswer | None:
        result = await self.db.execute(
            select(RoomAnswer).where(
                RoomAnswer.room_id == room_id,
                RoomAnswer.user_id == user_id,
                RoomAnswer.sentence_index == sentence_index,
            )
        )
        return result.scalar_one_or_none()

    # ── Transaction helpers ─────────────────────────────────────────────────

    def add(self, instance) -> None:
        self.db.add(instance)

    async def commit(self) -> None:
        await self.db.commit()

    async def flush(self) -> None:
        await self.db.flush()
