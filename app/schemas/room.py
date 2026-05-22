from pydantic import BaseModel


class CreateRoomRequest(BaseModel):
    video_id: str
    max_players: int = 10
    max_replays: int = 3
    exam_duration_minutes: int = 30


class CreateRoomResponse(BaseModel):
    room_code: str
    room_id: str


class JoinRoomResponse(BaseModel):
    room_code: str
    status: str
    host_user_id: str
    video_id: str
    max_replays: int
    exam_duration_minutes: int
    total_sentences: int
    member_count: int


class RoomSnapshotMember(BaseModel):
    user_id: str
    display_name: str
    is_ready: bool
    sentences_done: int
    total_score: float
    is_finished: bool


class RoomSnapshotResponse(BaseModel):
    room_code: str
    status: str
    host_user_id: str
    video_id: str
    max_replays: int
    exam_duration_minutes: int
    total_sentences: int
    members: list[RoomSnapshotMember]
