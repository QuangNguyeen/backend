"""Business logic for vocabulary: saving, SRS review, enrichment, and import."""

import asyncio
import hashlib
import io
import logging
import time
from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import BackgroundTasks

from app.core.exceptions import BadRequestError, NotFoundError
from app.database import async_session
from app.models.import_job import ImportJob
from app.models.user import User
from app.models.vocabulary import SavedWord
from app.repositories.vocabulary_repository import VocabularyRepository
from app.schemas.vocabulary import (
    BatchPreviewRequest,
    DueCardsResponse,
    FlashCardResponse,
    ImportJobStatus,
    ImportResultResponse,
    ReviewRequest,
    ReviewResponse,
    SaveWordRequest,
    UpdateWordRequest,
    WordPreviewResponse,
)
from app.services.level_service import _get_nlp
from app.services.llm_service import (
    _TTS_FALLBACK_PREFIX,
    DictionaryResult,
    fetch_dictionary_data,
    google_translate,
)
from app.services.srs_service import calculate_next_review
from app.utils.vocabulary_import import parse_import_file

logger = logging.getLogger(__name__)

MAX_IMPORT_WORDS = 100


# ── Pure helpers & process-level caches ─────────────────────────────────────


def _hash_context(context_sentence: str) -> str:
    return hashlib.sha256(context_sentence.encode("utf-8")).hexdigest()


def resolve_audio_url(audio_url: str | None, base_url: str) -> str | None:
    """Convert an internal TTS path to an absolute URL using the request origin."""
    if audio_url and audio_url.startswith(_TTS_FALLBACK_PREFIX):
        return base_url.rstrip("/") + audio_url
    return audio_url


_lemma_cache: dict[tuple[str, str | None], str] = {}
_LEMMA_CACHE_MAX = 5000


def _lemmatize(word: str, context_sentence: str | None = None) -> str:
    cache_key = (word, context_sentence)
    cached = _lemma_cache.get(cache_key)
    if cached is not None:
        return cached

    if "-" in word:
        _lemma_cache[cache_key] = word
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
                        _lemma_cache[cache_key] = word
                        return word
                    _lemma_cache[cache_key] = lemma
                    if len(_lemma_cache) > _LEMMA_CACHE_MAX:
                        _first = next(iter(_lemma_cache))
                        _lemma_cache.pop(_first, None)
                    return lemma

        doc = nlp(word)
        if len(doc) == 0:
            _lemma_cache[cache_key] = word
            return word
        lemma = doc[0].lemma_
        if lemma == "-PRON-" or not lemma:
            _lemma_cache[cache_key] = word
            return word
        _lemma_cache[cache_key] = lemma
        if len(_lemma_cache) > _LEMMA_CACHE_MAX:
            _first = next(iter(_lemma_cache))
            _lemma_cache.pop(_first, None)
        return lemma
    except Exception as e:
        logger.warning("Lemmatization failed for word=%r: %s", word, e)
        return word


_translate_cache: dict[str, tuple[float, str | None]] = {}
_TRANSLATE_CACHE_TTL = 3600
_TRANSLATE_CACHE_MAX = 2000


async def _translate_sentence(sentence: str) -> str | None:
    """Translate with in-memory cache (same sentences reappear across users)."""
    cached = _translate_cache.get(sentence)
    if cached and (time.monotonic() - cached[0]) < _TRANSLATE_CACHE_TTL:
        return cached[1]
    result = await google_translate(sentence, target_language="vi")
    _translate_cache[sentence] = (time.monotonic(), result)
    if len(_translate_cache) > _TRANSLATE_CACHE_MAX:
        _first = next(iter(_translate_cache))
        _translate_cache.pop(_first, None)
    return result


async def enrich_and_persist(
    saved_word_id: str,
    word_text: str,
    original_word: str,
    context_sentence: str,
    context_hash: str,
    overwrite_meaning: bool,
) -> None:
    """Background task: fetch dictionary data, update SavedWord + WordCache.

    Checks global WordCache first; on miss calls dictionaryapi.dev + Gemini.
    Context sentence translation is always fetched via Google Translate.
    """
    phonetic: str | None = None
    vietnamese_meaning: str | None = None
    part_of_speech: str | None = None
    audio_url: str | None = None

    try:
        async with async_session() as session:
            cached = await VocabularyRepository(session).get_cached_word(word_text)
            if cached:
                phonetic = cached.phonetic
                vietnamese_meaning = cached.vietnamese_meaning
                part_of_speech = cached.part_of_speech
                audio_url = cached.audio_url
    except Exception:
        pass

    display_meaning: str | None = vietnamese_meaning
    if not vietnamese_meaning:
        try:
            dict_data = await fetch_dictionary_data(word_text, translate_meaning=True)
        except Exception as e:
            logger.warning("Dictionary lookup crashed for word_id=%s: %s", saved_word_id, e)
            dict_data = DictionaryResult()

        phonetic = dict_data.phonetic
        vietnamese_meaning = dict_data.meaning_vi
        display_meaning = vietnamese_meaning or dict_data.meaning_en
        part_of_speech = dict_data.part_of_speech
        audio_url = dict_data.audio_url

    if not audio_url:
        audio_url = f"{_TTS_FALLBACK_PREFIX}{quote(word_text)}"

    context_translation: str | None = None
    if context_sentence:
        context_translation = await _translate_sentence(context_sentence)

    have_any = any(
        v is not None for v in (phonetic, audio_url, display_meaning, context_translation)
    )
    if not have_any:
        return

    try:
        async with async_session() as session:
            saved = await VocabularyRepository(session).get_saved_word_by_id_any_user(saved_word_id)
            if saved is not None:
                if phonetic is not None:
                    saved.phonetic = phonetic
                if audio_url is not None:
                    saved.audio_url = audio_url
                if context_translation is not None:
                    saved.context_translation = context_translation
                if part_of_speech is not None:
                    saved.part_of_speech = part_of_speech
                if overwrite_meaning and display_meaning is not None:
                    saved.meaning = display_meaning
                await session.commit()
    except Exception as e:
        logger.exception("SavedWord update failed for word_id=%s: %s", saved_word_id, e)

    # Persist to global cache — only store actual Vietnamese, never English fallback
    try:
        async with async_session() as session:
            await VocabularyRepository(session).upsert_word_cache(
                dict(
                    word=word_text,
                    context_hash="__global__",
                    phonetic=phonetic,
                    audio_url=audio_url,
                    vietnamese_meaning=vietnamese_meaning,
                    part_of_speech=part_of_speech,
                    context_translation=context_translation,
                )
            )
            await session.commit()
    except Exception as e:
        logger.exception("WordCache upsert failed for word=%r: %s", word_text, e)


class VocabularyService:
    def __init__(self, repo: VocabularyRepository):
        self.repo = repo

    # ── Save ────────────────────────────────────────────────────────────────

    async def save_word(
        self, user: User, body: SaveWordRequest, background_tasks: BackgroundTasks
    ) -> SavedWord:
        lemma = _lemmatize(body.word, body.context_sentence).lower()

        context_hash: str | None = None
        if body.context_sentence:
            context_hash = _hash_context(body.context_sentence)

        cached = await self.repo.get_cached_word(lemma)

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

        existing = await self.repo.get_active_saved_word_by_lemma(user.id, lemma)

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
            await self.repo.commit()
            await self.repo.refresh(existing)
            word = existing
        else:
            word = SavedWord(
                user_id=user.id,
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
                next_review_at=datetime.now(UTC),
            )
            self.repo.add(word)
            await self.repo.commit()
            await self.repo.refresh(word)

        if cached is None and body.context_sentence and context_hash is not None:
            background_tasks.add_task(
                enrich_and_persist,
                saved_word_id=word.id,
                word_text=lemma,
                original_word=body.word,
                context_sentence=body.context_sentence,
                context_hash=context_hash,
                overwrite_meaning=body.meaning is None,
            )

        return word

    # ── Listing & review ─────────────────────────────────────────────────────

    async def list_words(
        self, user: User, video_id: str | None, limit: int, offset: int
    ) -> list[SavedWord]:
        return await self.repo.list_saved_words(user.id, video_id, limit, offset)

    async def get_due_cards(self, user: User) -> DueCardsResponse:
        now = datetime.now(UTC)
        cards = await self.repo.get_due_cards(user.id, now, limit=50)
        total_due = await self.repo.count_due(user.id, now)
        return DueCardsResponse(
            cards=[FlashCardResponse.model_validate(c) for c in cards],
            total_due=total_due,
        )

    async def review_word(self, user: User, word_id: str, body: ReviewRequest) -> ReviewResponse:
        word = await self.repo.get_saved_word_by_id(user.id, word_id)
        if not word:
            raise NotFoundError("Word not found")

        srs = calculate_next_review(
            quality=body.quality,
            repetitions=word.repetitions,
            ease_factor=word.ease_factor,
            interval_days=word.interval_days,
        )

        word.repetitions = srs.repetitions
        word.ease_factor = srs.ease_factor
        word.interval_days = srs.interval_days
        word.next_review_at = srs.next_review_at
        word.last_reviewed_at = datetime.now(UTC)

        await self.repo.commit()

        return ReviewResponse(
            word_id=word.id,
            next_review_at=srs.next_review_at,
            interval_days=srs.interval_days,
            ease_factor=srs.ease_factor,
            repetitions=srs.repetitions,
        )

    async def update_word(self, user: User, word_id: str, body: UpdateWordRequest) -> SavedWord:
        word = await self.repo.get_saved_word_by_id(user.id, word_id)
        if not word:
            raise NotFoundError("Word not found")

        if body.meaning is not None:
            word.meaning = body.meaning
        if body.note is not None:
            word.note = body.note

        await self.repo.commit()
        await self.repo.refresh(word)
        return word

    async def delete_word(self, user: User, word_id: str) -> None:
        word = await self.repo.get_active_saved_word_by_id(user.id, word_id)
        if not word:
            raise NotFoundError("Word not found")

        word.deleted_at = datetime.now(UTC)
        await self.repo.commit()

    # ── Preview (enrichment without saving) ──────────────────────────────────

    async def preview_word(
        self, user: User, word: str, context: str | None, base_url: str
    ) -> WordPreviewResponse:
        lemma = _lemmatize(word, context).lower()

        # Parallel: is_saved + cache lookup (preserves the original concurrency).
        is_saved, cached = await asyncio.gather(
            self.repo.saved_word_exists(user.id, lemma),
            self.repo.get_cached_word(lemma),
        )

        if cached:
            context_translation = cached.context_translation
            if not context_translation and context:
                context_translation = await _translate_sentence(context)
            return WordPreviewResponse(
                word=lemma,
                phonetic=cached.phonetic,
                meaning=cached.vietnamese_meaning,
                audio_url=resolve_audio_url(cached.audio_url, base_url),
                context_translation=context_translation,
                is_saved=is_saved,
                part_of_speech=cached.part_of_speech,
            )

        tasks: list[asyncio.Task] = []
        tasks.append(asyncio.ensure_future(fetch_dictionary_data(lemma, translate_meaning=True)))
        if context:
            tasks.append(asyncio.ensure_future(_translate_sentence(context)))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        dict_data = results[0] if not isinstance(results[0], Exception) else DictionaryResult()
        if isinstance(dict_data, Exception):
            logger.warning("Dictionary lookup failed for word=%r: %s", word, dict_data)
            dict_data = DictionaryResult()

        context_translation: str | None = None
        if context and len(results) > 1 and not isinstance(results[1], Exception):
            context_translation = results[1]

        phonetic = dict_data.phonetic
        vietnamese_meaning = dict_data.meaning_vi
        display_meaning = vietnamese_meaning or dict_data.meaning_en
        part_of_speech = dict_data.part_of_speech
        audio_url = dict_data.audio_url
        if not audio_url:
            audio_url = f"{_TTS_FALLBACK_PREFIX}{quote(lemma)}"

        try:
            cache_values = dict(
                word=lemma,
                context_hash="__global__",
                phonetic=phonetic,
                audio_url=audio_url,
                vietnamese_meaning=vietnamese_meaning,
                part_of_speech=part_of_speech,
            )
            if context_translation is not None:
                cache_values["context_translation"] = context_translation
            await self.repo.upsert_word_cache(cache_values)
            await self.repo.commit()
        except Exception as e:
            logger.warning("WordCache upsert failed in preview for word=%r: %s", lemma, e)

        return WordPreviewResponse(
            word=lemma,
            phonetic=phonetic,
            meaning=display_meaning,
            audio_url=resolve_audio_url(audio_url, base_url),
            context_translation=context_translation,
            is_saved=is_saved,
            part_of_speech=part_of_speech,
        )

    async def preview_words_batch(
        self, user: User, body: BatchPreviewRequest, base_url: str
    ) -> dict[str, WordPreviewResponse]:
        if not body.words:
            return {}
        unique_words = list(dict.fromkeys(w.strip().lower() for w in body.words[:50]))

        lemmas = {w: _lemmatize(w, body.context).lower() for w in unique_words}
        unique_lemmas = list(set(lemmas.values()))

        saved_set = await self.repo.get_saved_lemmas(user.id, unique_lemmas)
        cache_map = await self.repo.get_cached_words(unique_lemmas)

        uncached_lemmas = [lemma for lemma in unique_lemmas if lemma not in cache_map]
        dict_results: dict[str, DictionaryResult] = {}
        if uncached_lemmas:
            tasks = [
                fetch_dictionary_data(lemma, translate_meaning=True) for lemma in uncached_lemmas
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for lemma, res in zip(uncached_lemmas, results):
                if isinstance(res, Exception):
                    dict_results[lemma] = DictionaryResult()
                else:
                    dict_results[lemma] = res

        ctx_translation = None
        if body.context:
            ctx_translation = await _translate_sentence(body.context)

        out: dict[str, WordPreviewResponse] = {}
        for w in unique_words:
            lemma = lemmas[w]
            cached = cache_map.get(lemma)
            if cached:
                out[w] = WordPreviewResponse(
                    word=lemma,
                    phonetic=cached.phonetic,
                    meaning=cached.vietnamese_meaning,
                    audio_url=resolve_audio_url(cached.audio_url, base_url),
                    context_translation=cached.context_translation or ctx_translation,
                    is_saved=lemma in saved_set,
                    part_of_speech=cached.part_of_speech,
                )
            else:
                dr = dict_results.get(lemma, DictionaryResult())
                audio_url = dr.audio_url
                if not audio_url:
                    audio_url = f"{_TTS_FALLBACK_PREFIX}{quote(lemma)}"
                out[w] = WordPreviewResponse(
                    word=lemma,
                    phonetic=dr.phonetic,
                    meaning=dr.meaning_vi or dr.meaning_en,
                    audio_url=resolve_audio_url(audio_url, base_url),
                    context_translation=ctx_translation,
                    is_saved=lemma in saved_set,
                    part_of_speech=dr.part_of_speech,
                )
                try:
                    cache_vals = dict(
                        word=lemma,
                        context_hash="__global__",
                        phonetic=dr.phonetic,
                        audio_url=audio_url,
                        vietnamese_meaning=dr.meaning_vi,
                        part_of_speech=dr.part_of_speech,
                    )
                    if ctx_translation is not None:
                        cache_vals["context_translation"] = ctx_translation
                    await self.repo.upsert_word_cache(cache_vals)
                except Exception:
                    pass
        try:
            await self.repo.commit()
        except Exception:
            pass
        return out

    # ── TTS ──────────────────────────────────────────────────────────────────

    async def generate_tts(self, word: str) -> bytes:
        """Generate pronunciation audio via edge-tts (in-memory). Raises on failure."""
        import edge_tts

        buf = io.BytesIO()
        communicate = edge_tts.Communicate(word, "en-US-AriaNeural", volume="+50%")
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    # ── Export & import ──────────────────────────────────────────────────────

    async def get_export_words(self, user: User) -> list[SavedWord]:
        return await self.repo.get_all_saved_words(user.id)

    async def import_words(
        self, user: User, filename: str, content: bytes, enrich: bool
    ) -> ImportResultResponse:
        try:
            rows = parse_import_file(filename or "", content)
        except Exception as e:
            logger.warning("Import file parse failed: %s", e)
            raise BadRequestError("Could not parse file. Please upload a valid CSV or XLSX.")

        word_rows = [r for r in rows if (r.get("word") or "").strip()]
        if len(word_rows) > MAX_IMPORT_WORDS:
            raise BadRequestError(f"Maximum {MAX_IMPORT_WORDS} words per import")

        imported = 0
        updated = 0
        errors: list[dict] = []
        existing_ids: list[str] = []
        new_words: list[SavedWord] = []

        for row_num, row in enumerate(rows, start=2):
            word_text = (row.get("word") or "").strip().lower()
            if not word_text:
                errors.append({"row": row_num, "error": "missing word"})
                continue

            existing = await self.repo.get_active_saved_word_by_lemma(user.id, word_text)

            if existing:
                if row.get("meaning"):
                    existing.meaning = row["meaning"].strip()
                if row.get("phonetic"):
                    existing.phonetic = row["phonetic"].strip()
                if row.get("note"):
                    existing.note = row["note"].strip()
                if row.get("part_of_speech"):
                    existing.part_of_speech = row["part_of_speech"].strip()
                if row.get("context_sentence"):
                    existing.context_sentence = row["context_sentence"].strip()
                updated += 1
                if enrich:
                    existing_ids.append(existing.id)
            else:
                new_word = SavedWord(
                    user_id=user.id,
                    word=word_text,
                    meaning=(row.get("meaning") or "").strip() or None,
                    phonetic=(row.get("phonetic") or "").strip() or None,
                    note=(row.get("note") or "").strip() or None,
                    part_of_speech=(row.get("part_of_speech") or "").strip() or None,
                    context_sentence=(row.get("context_sentence") or "").strip() or None,
                    source="csv_import",
                    next_review_at=datetime.now(UTC),
                )
                self.repo.add(new_word)
                imported += 1
                if enrich:
                    new_words.append(new_word)

        await self.repo.commit()

        # IDs of newly-inserted rows are only populated after the commit above.
        new_word_ids = existing_ids + [w.id for w in new_words]

        job_id: str | None = None
        enrich_queued = False
        if enrich and new_word_ids:
            job = ImportJob(
                user_id=user.id,
                status="pending",
                total_words=len(new_word_ids),
                phase="meanings",
            )
            self.repo.add(job)
            await self.repo.commit()
            await self.repo.refresh(job)
            job_id = job.id
            try:
                from app.tasks.vocabulary_enrichment import enrich_imported_words

                enrich_imported_words.delay(job.id, new_word_ids, True)
                enrich_queued = True
            except Exception as e:
                logger.error("Failed to queue enrichment task for job %s: %s", job.id, e)
                job.status = "failed"
                job.error = "Could not queue enrichment task"
                await self.repo.commit()

        return ImportResultResponse(
            job_id=job_id,
            imported=imported,
            updated=updated,
            errors=errors,
            total_words=len(word_rows),
            enrich_queued=enrich_queued,
        )

    async def get_import_status(self, user: User, job_id: str) -> ImportJobStatus:
        job = await self.repo.get_import_job(user.id, job_id)
        if not job:
            raise NotFoundError("Import job not found")

        total = job.total_words or 0
        enriched = min(job.enriched_count or 0, total) if total else 0
        progress_pct = (
            int(round((enriched / total) * 100))
            if total
            else (100 if job.status == "completed" else 0)
        )
        if job.status == "completed":
            progress_pct = 100

        return ImportJobStatus(
            job_id=job.id,
            status=job.status,
            total=total,
            enriched=enriched,
            phase=job.phase,
            progress_pct=progress_pct,
            error=job.error,
        )