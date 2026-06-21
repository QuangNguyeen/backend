"""Unit tests for room helpers and provider-free RoomService paths."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.models.room import RoomMember, RoomSession
from app.schemas.room import CreateRoomRequest
from app.services.room_service import RoomService, _calc_time_bonus, _generate_room_code

# ── Pure helpers ────────────────────────────────────────────────────────────


def test_generate_room_code_format():
    code = _generate_room_code()
    assert len(code) == 6
    assert code.isalnum() and code.isupper()


def test_time_bonus_is_max_at_zero_elapsed():
    t = datetime(2026, 1, 1, tzinfo=UTC)
    assert _calc_time_bonus(t, t) == 0.2


def test_time_bonus_decays_linearly():
    t = datetime(2026, 1, 1, tzinfo=UTC)
    # Halfway through the 120s window → half of max bonus.
    assert _calc_time_bonus(t, t + timedelta(seconds=60)) == 0.1


def test_time_bonus_clamped_to_zero_after_window():
    t = datetime(2026, 1, 1, tzinfo=UTC)
    assert _calc_time_bonus(t, t + timedelta(seconds=300)) == 0.0


# ── Service (fake repo, no manager broadcasts on these paths) ────────────────


class FakeRoomRepository:
    def __init__(self):
        self.video = None
        self.room = None
        self.member = None
        self.member_count = 0
        self.members_by_score: list[RoomMember] = []
        self.unfinished = 0

    async def get_video(self, video_id):
        return self.video

    async def has_user_practice(self, user_id, video_id):
        return False

    async def get_room(self, room_code):
        return self.room

    async def get_member(self, room_id, user_id):
        return self.member

    async def get_member_count(self, room_id):
        return self.member_count

    async def get_members_by_score(self, room_id):
        return self.members_by_score

    async def count_unfinished(self, room_id):
        return self.unfinished


class _User:
    id = "u1"
    display_name = "U"
    is_admin = False


def _room(**kw):
    defaults = dict(
        id="r1",
        room_code="ABC123",
        host_user_id="u1",
        video_id="v1",
        status="waiting",
        max_players=10,
        max_replays=3,
        exam_duration_minutes=30,
        total_sentences=0,
    )
    defaults.update(kw)
    return RoomSession(**defaults)


@pytest.mark.asyncio
async def test_create_room_video_not_found_raises_404():
    service = RoomService(FakeRoomRepository())
    body = CreateRoomRequest(video_id="missing")
    with pytest.raises(HTTPException) as exc:
        await service.create_room(_User(), body)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_join_room_rejects_started_game():
    repo = FakeRoomRepository()
    repo.room = _room(status="active")
    service = RoomService(repo)
    with pytest.raises(HTTPException) as exc:
        await service.join_room(_User(), "ABC123")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_join_room_rejects_when_full():
    repo = FakeRoomRepository()
    repo.room = _room(status="waiting", max_players=2)
    repo.member_count = 2
    service = RoomService(repo)
    with pytest.raises(HTTPException) as exc:
        await service.join_room(_User(), "ABC123")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_build_rankings_orders_and_rounds():
    repo = FakeRoomRepository()
    repo.members_by_score = [
        RoomMember(
            room_id="r1",
            user_id="a",
            display_name="A",
            total_score=9.876,
            sentences_done=3,
            finished_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        RoomMember(
            room_id="r1",
            user_id="b",
            display_name="B",
            total_score=4.2,
            sentences_done=1,
            finished_at=None,
        ),
    ]
    service = RoomService(repo)

    rankings = await service._build_rankings("r1")

    assert [r["rank"] for r in rankings] == [1, 2]
    assert rankings[0] == {
        "rank": 1,
        "user_id": "a",
        "display_name": "A",
        "total_score": 9.88,
        "sentences_done": 3,
        "accuracy_pct": 0.0,
        "is_finished": True,
    }
    assert rankings[1]["is_finished"] is False


@pytest.mark.asyncio
async def test_check_all_finished():
    repo = FakeRoomRepository()
    service = RoomService(repo)
    repo.unfinished = 0
    assert await service._check_all_finished("r1") is True
    repo.unfinished = 2
    assert await service._check_all_finished("r1") is False
