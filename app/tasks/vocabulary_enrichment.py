import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from app.celery_app import celery
from app.config import get_settings
from app.models.import_job import ImportJob
from app.models.vocabulary import SavedWord
from app.services.llm_service import (
    _TTS_FALLBACK_PREFIX,
    fetch_dictionary_data,
    generate_example_sentences,
    google_translate,
)

logger = logging.getLogger(__name__)

_engine = None

_CHUNK_SIZE = 10  # words processed per progress update
_AUDIO_CONCURRENCY = 5  # parallel dictionary lookups


def _get_sync_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(settings.DATABASE_URL_SYNC, pool_pre_ping=True)
    return _engine


def _update_job(db: Session, job_id: str, **values) -> None:
    values["updated_at"] = datetime.now(UTC)
    db.execute(update(ImportJob).where(ImportJob.id == job_id).values(**values))
    db.commit()


@celery.task(bind=True, name="vocabulary.enrich_imported_words", max_retries=2)
def enrich_imported_words(
    self,
    job_id: str,
    word_ids: list[str],
    generate_examples: bool,
):
    """Enrich imported words with meaning, IPA, audio, and optional examples.

    Phase 1 (meanings/audio): per-word dictionary lookup (Cambridge → dictionaryapi
      → TTS fallback) fills missing meaning, phonetic, part_of_speech, audio_url.
    Phase 2 (translations): for words missing a context sentence, generate a B1
      example via Gemini (batch) and translate it to Vietnamese.

    Writes incremental progress to ``import_jobs`` so the status endpoint can poll.
    Existing values on a SavedWord are never overwritten.
    """
    engine = _get_sync_engine()
    total = len(word_ids)
    logger.info("[ENRICH] ▶ job=%s words=%d examples=%s", job_id, total, generate_examples)

    with Session(engine) as db:
        _update_job(
            db, job_id, status="processing", phase="meanings", total_words=total, enriched_count=0
        )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        enriched = 0
        for start in range(0, total, _CHUNK_SIZE):
            chunk = word_ids[start : start + _CHUNK_SIZE]

            with Session(engine) as db:
                rows = db.execute(select(SavedWord).where(SavedWord.id.in_(chunk))).scalars().all()
                words = [(r.id, r.word) for r in rows]

            results = loop.run_until_complete(_lookup_chunk(words))

            with Session(engine) as db:
                rows = db.execute(select(SavedWord).where(SavedWord.id.in_(chunk))).scalars().all()
                for row in rows:
                    dr = results.get(row.id)
                    if dr is None:
                        continue
                    if not row.meaning:
                        row.meaning = dr.meaning_vi or dr.meaning_en
                    if not row.phonetic and dr.phonetic:
                        row.phonetic = dr.phonetic
                    if not row.part_of_speech and dr.part_of_speech:
                        row.part_of_speech = dr.part_of_speech
                    if not row.audio_url:
                        row.audio_url = dr.audio_url or _tts_fallback(row.word)
                db.commit()

            enriched += len(chunk)
            with Session(engine) as db:
                _update_job(db, job_id, enriched_count=min(enriched, total))
            logger.info("[ENRICH] job=%s progress %d/%d", job_id, enriched, total)

        # ── Phase 2: example sentences + translations ──
        if generate_examples:
            with Session(engine) as db:
                _update_job(db, job_id, phase="translations")
            _generate_examples_phase(engine, loop, word_ids)

        with Session(engine) as db:
            _update_job(db, job_id, status="completed", phase="done", enriched_count=total)
        logger.info("[ENRICH] ✔ job=%s complete (%d words)", job_id, total)
        return {"status": "completed", "job_id": job_id, "enriched": total}

    except Exception as exc:
        logger.error(
            "[ENRICH] ✘ job=%s failed (retry %d/%d): %s",
            job_id,
            self.request.retries,
            self.max_retries,
            exc,
            exc_info=True,
        )
        with Session(engine) as db:
            _update_job(db, job_id, status="failed", error=str(exc)[:500])
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30)
        raise
    finally:
        loop.close()


def _tts_fallback(word: str) -> str:
    from urllib.parse import quote

    return f"{_TTS_FALLBACK_PREFIX}{quote(word)}"


async def _lookup_chunk(words: list[tuple[str, str]]) -> dict:
    """Look up dictionary data for a chunk of (id, word) with bounded concurrency."""
    sem = asyncio.Semaphore(_AUDIO_CONCURRENCY)

    async def _one(word_id: str, word: str):
        async with sem:
            try:
                return word_id, await fetch_dictionary_data(word, translate_meaning=True)
            except Exception as e:
                logger.warning("[ENRICH] dictionary lookup failed for %r: %s", word, e)
                return word_id, None

    pairs = await asyncio.gather(*[_one(wid, w) for wid, w in words])
    return {wid: dr for wid, dr in pairs if dr is not None}


def _generate_examples_phase(engine, loop, word_ids: list[str]) -> None:
    """Generate + translate example sentences for words missing a context sentence."""
    with Session(engine) as db:
        rows = (
            db.execute(
                select(SavedWord).where(
                    SavedWord.id.in_(word_ids),
                    SavedWord.context_sentence.is_(None),
                )
            )
            .scalars()
            .all()
        )
        targets = [(r.id, r.word) for r in rows]

    if not targets:
        return

    word_texts = [w for _, w in targets]
    examples = loop.run_until_complete(generate_example_sentences(word_texts))
    if not examples:
        return

    # Translate the generated sentences to Vietnamese (bounded concurrency).
    sentences = list({s for s in examples.values()})
    translations = loop.run_until_complete(_translate_all(sentences))

    with Session(engine) as db:
        rows = (
            db.execute(select(SavedWord).where(SavedWord.id.in_([t[0] for t in targets])))
            .scalars()
            .all()
        )
        by_id = {r.id: r for r in rows}
        for word_id, word in targets:
            example = examples.get(word)
            row = by_id.get(word_id)
            if not example or row is None or row.context_sentence:
                continue
            row.context_sentence = example
            translated = translations.get(example)
            if translated and not row.context_translation:
                row.context_translation = translated
        db.commit()


async def _translate_all(sentences: list[str]) -> dict:
    sem = asyncio.Semaphore(_AUDIO_CONCURRENCY)

    async def _one(sentence: str):
        async with sem:
            try:
                return sentence, await google_translate(sentence, target_language="vi")
            except Exception:
                return sentence, None

    pairs = await asyncio.gather(*[_one(s) for s in sentences])
    return {s: t for s, t in pairs if t}
