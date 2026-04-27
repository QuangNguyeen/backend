import hashlib
import io
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session, get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.vocabulary import SavedWord
from app.models.word_cache import WordCache
from app.schemas.vocabulary import (
    SaveWordRequest, UpdateWordRequest, SavedWordResponse,
    ReviewRequest, ReviewResponse,
    FlashCardResponse, DueCardsResponse, WordPreviewResponse,
)
from app.services.srs_service import calculate_next_review
from app.services.llm_service import (
    fetch_dictionary_data, google_translate,
    DictionaryResult, _TTS_FALLBACK_PREFIX,
)
from app.services.level_service import _get_nlp
from app.core.exceptions import NotFoundError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vocabulary", tags=["Vocabulary"])


def _hash_context(context_sentence: str) -> str:
    return hashlib.sha256(context_sentence.encode("utf-8")).hexdigest()


def _resolve_audio_url(audio_url: str | None, request: Request) -> str | None:
    """Convert an internal TTS path to an absolute URL using the request origin."""
    if audio_url and audio_url.startswith(_TTS_FALLBACK_PREFIX):
        return str(request.base_url).rstrip("/") + audio_url
    return audio_url


def _lemmatize(word: str, context_sentence: str | None = None) -> str:
    """Convert an inflected word to its lemma (base form) using spaCy.

    When `context_sentence` is provided, the full sentence is processed so
    spaCy's POS tagger has the context it needs to disambiguate (e.g. "saw"
    as verb → "see" vs "saw" as noun → "saw"). The matching token is then
    pulled out and its lemma returned. If the token isn't found in the
    sentence (case-insensitive), we fall back to single-word processing.

    Pronouns (spaCy's legacy `-PRON-` sentinel) are left unchanged so that
    "I" / "her" / "me" never get mangled.

    Any failure — missing model, exception, empty parse — returns the word
    unchanged. The save flow must never break because of lemmatization.
    """
    # Hyphenated/compound tokens ("night-light", "self-aware") get split by
    # spaCy's tokenizer, which would return only the first segment. Short-
    # circuit and keep the original shape.
    if "-" in word:
        return word

    try:
        nlp = _get_nlp()

        if context_sentence:
            doc = nlp(context_sentence)
            target = word.lower()
            for token in doc:
                if token.text.lower() == target:
                    lemma = token.lemma_
                    if lemma == "-PRON-" or not lemma:
                        return word
                    return lemma

        doc = nlp(word)
        if len(doc) == 0:
            return word
        lemma = doc[0].lemma_
        if lemma == "-PRON-" or not lemma:
            return word
        return lemma
    except Exception as e:
        logger.warning("Lemmatization failed for word=%r: %s", word, e)
        return word


async def _translate_sentence(sentence: str) -> str | None:
    """Translate a full sentence to Vietnamese via Google Cloud Translation API."""
    return await google_translate(sentence, target_language="vi")


async def _enrich_and_persist(
    saved_word_id: str,
    word_text: str,
    original_word: str,
    context_sentence: str,
    context_hash: str,
    overwrite_meaning: bool,
) -> None:
    """Background task: Dictionary → Google Translate, update SavedWord + WordCache.

    Sequential pipeline (no LLM):
    1. dictionaryapi.dev → audio URL, IPA, English definition
    2. Google Translate → Vietnamese translation of English definition
    3. Google Translate → Vietnamese translation of context sentence
    """
    # Step 1: Dictionary API
    try:
        dict_data = await fetch_dictionary_data(word_text, translate_meaning=False)
    except Exception as e:
        logger.warning("Dictionary lookup crashed for word_id=%s: %s", saved_word_id, e)
        dict_data = DictionaryResult()

    # Step 2: Translate English definition to Vietnamese
    if dict_data.meaning_en:
        translated = await google_translate(dict_data.meaning_en, target_language="vi")
        if translated:
            dict_data.meaning_vi = translated

    # Step 3: Translate context sentence to Vietnamese
    context_translation: str | None = None
    if context_sentence:
        context_translation = await _translate_sentence(context_sentence)

    phonetic = dict_data.phonetic
    vietnamese_meaning = dict_data.meaning_vi or dict_data.meaning_en
    part_of_speech = dict_data.part_of_speech
    audio_url = dict_data.audio_url
    if not audio_url:
        from urllib.parse import quote
        audio_url = f"{_TTS_FALLBACK_PREFIX}{quote(word_text)}"

    have_any = any(v is not None for v in (phonetic, audio_url, vietnamese_meaning, context_translation))
    if not have_any:
        return  # truly nothing to persist — leave the row for a future retry flow

    # Write SavedWord first, in its own transaction, so a WordCache conflict
    # can never roll back the user-visible row.
    try:
        async with async_session() as session:
            result = await session.execute(
                select(SavedWord).where(SavedWord.id == saved_word_id)
            )
            saved = result.scalar_one_or_none()
            if saved is not None:
                if phonetic is not None:
                    saved.phonetic = phonetic
                if audio_url is not None:
                    saved.audio_url = audio_url
                if context_translation is not None:
                    saved.context_translation = context_translation
                if part_of_speech is not None:
                    saved.part_of_speech = part_of_speech
                if overwrite_meaning and vietnamese_meaning is not None:
                    saved.meaning = vietnamese_meaning
                await session.commit()
    except Exception as e:
        logger.exception("SavedWord update failed for word_id=%s: %s", saved_word_id, e)

    cache_values = dict(
        word=word_text,
        context_hash=context_hash,
        phonetic=phonetic,
        audio_url=audio_url,
        vietnamese_meaning=vietnamese_meaning,
        part_of_speech=part_of_speech,
    )
    if context_translation is not None:
        cache_values["context_translation"] = context_translation

    try:
        async with async_session() as session:
            stmt = pg_insert(WordCache).values(
                **cache_values,
            ).on_conflict_do_nothing(
                index_elements=["word", "context_hash"],
            )
            await session.execute(stmt)
            await session.commit()
    except Exception as e:
        logger.exception("WordCache upsert failed for word=%r: %s", word_text, e)


@router.post("/save", response_model=SavedWordResponse, status_code=status.HTTP_201_CREATED)
async def save_word(
    body: SaveWordRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save a word from dictation/quiz for SRS review (upsert).

    If the user already has this word (same lemma, not soft-deleted), the
    existing row is updated instead of creating a duplicate.

    Enrichment flow:
      - If a WordCache row exists for (word, context_hash), hydrate immediately.
      - Frontend preview fields (audio_url, phonetic, etc.) are used as fallback.
      - Otherwise, schedule a background enrichment task.
    """
    lemma = _lemmatize(body.word, body.context_sentence).lower()

    context_hash: str | None = None
    cached: WordCache | None = None

    if body.context_sentence:
        context_hash = _hash_context(body.context_sentence)
        result = await db.execute(
            select(WordCache).where(
                WordCache.word == lemma,
                WordCache.context_hash == context_hash,
            )
        )
        cached = result.scalar_one_or_none()

    meaning = body.meaning
    phonetic = body.phonetic
    audio_url = body.audio_url
    context_translation = body.context_translation
    part_of_speech = body.part_of_speech

    if cached is not None:
        phonetic = phonetic or cached.phonetic
        audio_url = audio_url or cached.audio_url
        context_translation = context_translation or cached.context_translation
        part_of_speech = part_of_speech or cached.part_of_speech
        if meaning is None and cached.vietnamese_meaning is not None:
            meaning = cached.vietnamese_meaning

    # Upsert: check if user already has this word (not soft-deleted)
    existing_result = await db.execute(
        select(SavedWord).where(
            SavedWord.user_id == current_user.id,
            SavedWord.word == lemma,
            SavedWord.deleted_at.is_(None),
        )
    )
    existing = existing_result.scalar_one_or_none()

    if existing is not None:
        if body.context_sentence is not None:
            existing.context_sentence = body.context_sentence
        if body.video_id is not None:
            existing.video_id = body.video_id
        if body.audio_start_time is not None:
            existing.audio_start_time = body.audio_start_time
        if meaning is not None:
            existing.meaning = meaning
        if body.note is not None:
            existing.note = body.note
        if phonetic is not None:
            existing.phonetic = phonetic
        if audio_url is not None:
            existing.audio_url = audio_url
        if context_translation is not None:
            existing.context_translation = context_translation
        if part_of_speech is not None:
            existing.part_of_speech = part_of_speech
        existing.source = body.source
        await db.commit()
        await db.refresh(existing)
        word = existing
    else:
        word = SavedWord(
            user_id=current_user.id,
            word=lemma,
            video_id=body.video_id,
            context_sentence=body.context_sentence,
            audio_start_time=body.audio_start_time,
            meaning=meaning,
            note=body.note,
            source=body.source,
            phonetic=phonetic,
            audio_url=audio_url,
            context_translation=context_translation,
            part_of_speech=part_of_speech,
            next_review_at=datetime.now(timezone.utc),
        )
        db.add(word)
        await db.commit()
        await db.refresh(word)

    if cached is None and body.context_sentence and context_hash is not None:
        background_tasks.add_task(
            _enrich_and_persist,
            saved_word_id=word.id,
            word_text=lemma,
            original_word=body.word,
            context_sentence=body.context_sentence,
            context_hash=context_hash,
            overwrite_meaning=body.meaning is None,
        )

    return word


@router.get("", response_model=list[SavedWordResponse])
async def list_words(
    video_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all saved words for the current user."""
    query = select(SavedWord).where(
        SavedWord.user_id == current_user.id,
        SavedWord.deleted_at.is_(None),
    )
    if video_id:
        query = query.where(SavedWord.video_id == video_id)
    query = query.order_by(SavedWord.created_at.desc()).limit(limit).offset(offset)

    result = await db.execute(query)
    return result.scalars().all()


@router.get("/due", response_model=DueCardsResponse)
async def get_due_cards(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get FlashCards due for review today."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(SavedWord)
        .where(
            SavedWord.user_id == current_user.id,
            SavedWord.deleted_at.is_(None),
            SavedWord.next_review_at <= now,
        )
        .order_by(SavedWord.next_review_at)
        .limit(50)
    )
    cards = result.scalars().all()

    # Count total due
    count_result = await db.execute(
        select(func.count()).where(
            SavedWord.user_id == current_user.id,
            SavedWord.deleted_at.is_(None),
            SavedWord.next_review_at <= now,
        )
    )
    total_due = count_result.scalar() or 0

    return DueCardsResponse(
        cards=[FlashCardResponse.model_validate(c) for c in cards],
        total_due=total_due,
    )


@router.get("/preview", response_model=WordPreviewResponse)
async def preview_word(
    request: Request,
    word: str,
    context: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return enrichment data for a word without saving it.

    Checks WordCache first; on miss, calls dictionary API + Google Translate and caches.
    """
    lemma = _lemmatize(word, context).lower()

    # Check if user already saved this word
    saved_result = await db.execute(
        select(SavedWord.id).where(
            SavedWord.user_id == current_user.id,
            SavedWord.word == lemma,
            SavedWord.deleted_at.is_(None),
        ).limit(1)
    )
    is_saved = saved_result.scalar() is not None

    context_hash: str | None = None
    if context:
        context_hash = _hash_context(context)
        result = await db.execute(
            select(WordCache).where(
                WordCache.word == lemma,
                WordCache.context_hash == context_hash,
            )
        )
        cached = result.scalar_one_or_none()
        if cached:
            ctx_trans = cached.context_translation
            if not ctx_trans and context:
                ctx_trans = await _translate_sentence(context)
                if ctx_trans:
                    try:
                        cached.context_translation = ctx_trans
                        await db.commit()
                    except Exception:
                        logger.warning("Cache heal failed for word=%r", lemma)
            return WordPreviewResponse(
                word=lemma,
                phonetic=cached.phonetic,
                meaning=cached.vietnamese_meaning,
                audio_url=_resolve_audio_url(cached.audio_url, request),
                context_translation=ctx_trans,
                is_saved=is_saved,
                part_of_speech=cached.part_of_speech,
            )

    # Sequential pipeline (no LLM):
    # 1. Dictionary API → audio, IPA, English definition
    # 2. Google Translate → Vietnamese meaning
    # 3. Google Translate → Vietnamese context translation

    # Step 1: Dictionary API
    try:
        dict_data = await fetch_dictionary_data(lemma, translate_meaning=False)
    except Exception as e:
        logger.warning("Dictionary lookup failed for word=%r: %s", word, e)
        dict_data = DictionaryResult()

    # Step 2: Translate English definition to Vietnamese
    if dict_data.meaning_en:
        translated = await google_translate(dict_data.meaning_en, target_language="vi")
        if translated:
            dict_data.meaning_vi = translated

    # Step 3: Translate context sentence to Vietnamese
    context_translation: str | None = None
    if context:
        context_translation = await _translate_sentence(context)

    phonetic = dict_data.phonetic
    vietnamese_meaning = dict_data.meaning_vi or dict_data.meaning_en
    part_of_speech = dict_data.part_of_speech
    audio_url = dict_data.audio_url
    if not audio_url:
        from urllib.parse import quote
        audio_url = f"{_TTS_FALLBACK_PREFIX}{quote(lemma)}"

    if context and context_hash:
        cache_values = dict(
            word=lemma,
            context_hash=context_hash,
            phonetic=phonetic,
            audio_url=audio_url,
            vietnamese_meaning=vietnamese_meaning,
            part_of_speech=part_of_speech,
        )
        if context_translation is not None:
            cache_values["context_translation"] = context_translation
        try:
            stmt = pg_insert(WordCache).values(
                **cache_values,
            ).on_conflict_do_nothing(index_elements=["word", "context_hash"])
            await db.execute(stmt)
            await db.commit()
        except Exception as e:
            logger.warning("WordCache upsert failed in preview for word=%r: %s", lemma, e)

    return WordPreviewResponse(
        word=lemma,
        phonetic=phonetic,
        meaning=vietnamese_meaning,
        audio_url=_resolve_audio_url(audio_url, request),
        context_translation=context_translation,
        is_saved=is_saved,
        part_of_speech=part_of_speech,
    )


@router.get("/tts/{word}")
async def text_to_speech(word: str):
    """Generate pronunciation audio using edge-tts (async, in-memory, no disk I/O)."""
    import edge_tts

    buf = io.BytesIO()
    try:
        communicate = edge_tts.Communicate(word, "en-US-AriaNeural", volume="+50%")
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
    except Exception as e:
        logger.warning("edge-tts failed for word=%r: %s", word, e)
        return StreamingResponse(
            io.BytesIO(b""),
            status_code=500,
            media_type="text/plain",
        )

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.post("/{word_id}/review", response_model=ReviewResponse)
async def review_word(
    word_id: str,
    body: ReviewRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Submit a FlashCard review with SM-2 quality rating (1-5)."""
    result = await db.execute(
        select(SavedWord).where(
            SavedWord.id == word_id,
            SavedWord.user_id == current_user.id,
        )
    )
    word = result.scalar_one_or_none()
    if not word:
        raise NotFoundError("Word not found")

    # Calculate next review using SM-2
    srs = calculate_next_review(
        quality=body.quality,
        repetitions=word.repetitions,
        ease_factor=word.ease_factor,
        interval_days=word.interval_days,
    )

    # Update word
    word.repetitions = srs.repetitions
    word.ease_factor = srs.ease_factor
    word.interval_days = srs.interval_days
    word.next_review_at = srs.next_review_at
    word.last_reviewed_at = datetime.now(timezone.utc)

    await db.commit()

    return ReviewResponse(
        word_id=word.id,
        next_review_at=srs.next_review_at,
        interval_days=srs.interval_days,
        ease_factor=srs.ease_factor,
        repetitions=srs.repetitions,
    )


@router.patch("/{word_id}", response_model=SavedWordResponse)
async def update_word(
    word_id: str,
    body: UpdateWordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update meaning or note for a saved word."""
    result = await db.execute(
        select(SavedWord).where(
            SavedWord.id == word_id,
            SavedWord.user_id == current_user.id,
        )
    )
    word = result.scalar_one_or_none()
    if not word:
        raise NotFoundError("Word not found")

    if body.meaning is not None:
        word.meaning = body.meaning
    if body.note is not None:
        word.note = body.note

    await db.commit()
    await db.refresh(word)
    return word


@router.delete("/{word_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_word(
    word_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft delete a saved word (SRS 7.2.7)."""
    result = await db.execute(
        select(SavedWord).where(
            SavedWord.id == word_id,
            SavedWord.user_id == current_user.id,
            SavedWord.deleted_at.is_(None),
        )
    )
    word = result.scalar_one_or_none()
    if not word:
        raise NotFoundError("Word not found")

    word.deleted_at = datetime.now(timezone.utc)
    await db.commit()
