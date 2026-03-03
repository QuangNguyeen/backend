from pydantic import BaseModel


class VideoResponse(BaseModel):
    id: str
    youtube_id: str
    title: str
    channel: str
    duration: int
    language: str
    level: str
    is_curated: bool
    thumbnail_url: str

    model_config = {"from_attributes": True}


class ImportVideoRequest(BaseModel):
    youtube_url: str


class SentenceResponse(BaseModel):
    id: str
    index: int
    text: str
    start_time: float
    end_time: float

    model_config = {"from_attributes": True}
