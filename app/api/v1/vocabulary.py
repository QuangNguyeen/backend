import csv
import io
import logging

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.repositories.vocabulary_repository import VocabularyRepository
from app.schemas.vocabulary import (
    BatchPreviewRequest,
    DueCardsResponse,
    ImportJobStatus,
    ImportResultResponse,
    ReviewRequest,
    ReviewResponse,
    SavedWordResponse,
    SaveWordRequest,
    UpdateWordRequest,
    WordPreviewResponse,
)
from app.services.vocabulary_service import VocabularyService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vocabulary", tags=["Vocabulary"])


def get_vocabulary_service(db: AsyncSession = Depends(get_db)) -> VocabularyService:
    return VocabularyService(VocabularyRepository(db))


@router.post("/save", response_model=SavedWordResponse, status_code=status.HTTP_201_CREATED)
async def save_word(
    body: SaveWordRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    service: VocabularyService = Depends(get_vocabulary_service),
):
    """Save a word from dictation for SRS review (upsert)."""
    return await service.save_word(current_user, body, background_tasks)


@router.get("", response_model=list[SavedWordResponse])
async def list_words(
    video_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    service: VocabularyService = Depends(get_vocabulary_service),
):
    """List all saved words for the current user."""
    return await service.list_words(current_user, video_id, limit, offset)


@router.get("/due", response_model=DueCardsResponse)
async def get_due_cards(
    current_user: User = Depends(get_current_user),
    service: VocabularyService = Depends(get_vocabulary_service),
):
    """Get FlashCards due for review today."""
    return await service.get_due_cards(current_user)


@router.get("/preview", response_model=WordPreviewResponse)
async def preview_word(
    request: Request,
    word: str,
    context: str | None = None,
    current_user: User = Depends(get_current_user),
    service: VocabularyService = Depends(get_vocabulary_service),
):
    """Return enrichment data for a word without saving it."""
    return await service.preview_word(current_user, word, context, str(request.base_url))


@router.post("/preview/batch", response_model=dict[str, WordPreviewResponse])
async def preview_words_batch(
    request: Request,
    body: BatchPreviewRequest,
    current_user: User = Depends(get_current_user),
    service: VocabularyService = Depends(get_vocabulary_service),
):
    """Batch preview: warm caches for multiple words in one round-trip."""
    return await service.preview_words_batch(current_user, body, str(request.base_url))


@router.get("/tts/{word}")
async def text_to_speech(
    word: str,
    service: VocabularyService = Depends(get_vocabulary_service),
):
    """Generate pronunciation audio using edge-tts (async, in-memory, no disk I/O)."""
    try:
        audio = await service.generate_tts(word)
    except Exception as e:
        logger.warning("edge-tts failed for word=%r: %s", word, e)
        return StreamingResponse(io.BytesIO(b""), status_code=500, media_type="text/plain")

    return StreamingResponse(
        io.BytesIO(audio),
        media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.post("/{word_id}/review", response_model=ReviewResponse)
async def review_word(
    word_id: str,
    body: ReviewRequest,
    current_user: User = Depends(get_current_user),
    service: VocabularyService = Depends(get_vocabulary_service),
):
    """Submit a FlashCard review with SM-2 quality rating (1-5)."""
    return await service.review_word(current_user, word_id, body)


@router.patch("/{word_id}", response_model=SavedWordResponse)
async def update_word(
    word_id: str,
    body: UpdateWordRequest,
    current_user: User = Depends(get_current_user),
    service: VocabularyService = Depends(get_vocabulary_service),
):
    """Update meaning or note for a saved word."""
    return await service.update_word(current_user, word_id, body)


@router.delete("/{word_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_word(
    word_id: str,
    current_user: User = Depends(get_current_user),
    service: VocabularyService = Depends(get_vocabulary_service),
):
    """Soft delete a saved word (SRS 7.2.7)."""
    await service.delete_word(current_user, word_id)


@router.get("/export")
async def export_words(
    current_user: User = Depends(get_current_user),
    service: VocabularyService = Depends(get_vocabulary_service),
):
    """Export all saved words as CSV."""
    words = await service.get_export_words(current_user)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["word", "meaning", "phonetic", "part_of_speech", "note", "context_sentence"])
    for w in words:
        writer.writerow(
            [
                w.word,
                w.meaning or "",
                w.phonetic or "",
                w.part_of_speech or "",
                w.note or "",
                w.context_sentence or "",
            ]
        )

    buf.seek(0)
    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=vocabulary.csv"},
    )


@router.post("/import", response_model=ImportResultResponse)
async def import_words(
    file: UploadFile = File(...),
    enrich: bool = Form(False),
    current_user: User = Depends(get_current_user),
    service: VocabularyService = Depends(get_vocabulary_service),
):
    """Import words from a CSV or XLSX file.

    Columns are auto-detected from the header row (word/term, meaning/definition,
    phonetic/ipa, part_of_speech/pos, note, context_sentence/example). Only
    ``word`` is required. Up to 100 words may be imported at once.

    When ``enrich=True``, an ``ImportJob`` is created and a Celery task is queued
    to fill missing meaning, IPA, audio, and example sentences in the background;
    the returned ``job_id`` can be polled via ``/import/{job_id}/status``.
    """
    content = await file.read()
    return await service.import_words(current_user, file.filename or "", content, enrich)


@router.get("/import/template")
async def download_template():
    """Download a sample CSV template with headers and example rows."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["word", "meaning", "phonetic", "part_of_speech", "note", "context_sentence"])
    writer.writerow(["apple", "quả táo", "/ˈæp.əl/", "noun", "", "I eat an apple every day."])
    writer.writerow(["resilient", "kiên cường", "/rɪˈzɪl.i.ənt/", "adjective", "", ""])
    writer.writerow(
        ["contribute", "đóng góp", "/kənˈtrɪb.juːt/", "verb", "", "He contributes to the team."]
    )
    buf.seek(0)
    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=vocabulary_template.csv"},
    )


@router.get("/import/{job_id}/status", response_model=ImportJobStatus)
async def get_import_status(
    job_id: str,
    current_user: User = Depends(get_current_user),
    service: VocabularyService = Depends(get_vocabulary_service),
):
    """Poll enrichment progress for an import job."""
    return await service.get_import_status(current_user, job_id)