"""Business logic for dictation sessions: sentence, cloze, and reorder modes."""

import math
import random
import re
from datetime import UTC, datetime

from app.core.exceptions import BadRequestError, NotFoundError
from app.events import publish_user_recommendation_event
from app.models.dictation import DictationAttempt, DictationSentence
from app.models.user import User
from app.models.video import Transcript
from app.repositories.dictation_repository import DictationRepository
from app.schemas.cloze import (
    ClozeBlankResult,
    ClozeChunksResponse,
    ClozeFullResponse,
    ClozeResultResponse,
    ClozeSubmitAllRequest,
    ClozeSubmitAllResponse,
    ClozeSubmitRequest,
    PracticeMode,
    SegmentScore,
)
from app.schemas.dictation import (
    HistoryAttemptResponse,
    HistoryPaginatedResponse,
    SentenceResultResponse,
    SubmitAnswerRequest,
    WordDiffItem,
)
from app.schemas.reorder import (
    ReorderChallenge,
    ReorderChallengesResponse,
    ReorderSentenceResult,
    ReorderSubmitAllRequest,
    ReorderSubmitAllResponse,
    ReorderSubmitRequest,
    ReorderToken,
    ReorderTokenResult,
)
from app.services.cloze_service import (
    build_chunks,
    build_full_cloze,
    get_chunk_answers,
    get_full_cloze_answers,
)
from app.services.dictation_service import compute_word_diff
from app.services.text_analysis_service import get_word_difficulty_map
from app.services.youtube_service import TranscriptSegment, process_transcript_segments

PAGE_SIZE = 20


# ── Pure helpers ────────────────────────────────────────────────────────────


def _populate_analytics(attempt: DictationAttempt, all_sentences: list[DictationSentence]):
    """Fill duration_seconds, error_summary, total_words, correct_words on completion."""
    if attempt.created_at and attempt.completed_at:
        delta = attempt.completed_at - attempt.created_at
        attempt.duration_seconds = int(delta.total_seconds())

    total_correct = 0
    total_wrong = 0
    total_missing = 0
    word_error_counts: dict[str, int] = {}

    for sentence in all_sentences:
        if not sentence.word_diff:
            continue
        for item in sentence.word_diff:
            status = item.get("status", "")
            if status == "correct":
                total_correct += 1
            elif status in ("wrong", "missing"):
                if status == "wrong":
                    total_wrong += 1
                else:
                    total_missing += 1
                word = item.get("expected") or item.get("word") or ""
                word = word.strip().lower()
                if word:
                    word_error_counts[word] = word_error_counts.get(word, 0) + 1

    total_words = total_correct + total_wrong + total_missing
    attempt.total_words = total_words
    attempt.correct_words = total_correct

    top_words = sorted(word_error_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    attempt.error_summary = {
        "top_words": [{"word": w, "count": c} for w, c in top_words],
        "total_wrong": total_wrong,
        "total_missing": total_missing,
    }


def _transcript_to_segment(transcript: Transcript) -> TranscriptSegment:
    duration = max(0.001, float(transcript.end_time - transcript.start_time))
    return TranscriptSegment(
        text=transcript.text,
        start=float(transcript.start_time),
        duration=duration,
    )


def _segments_for_practice_mode(
    transcripts: list[Transcript], practice_mode: PracticeMode
) -> list[TranscriptSegment]:
    raw_segments = [_transcript_to_segment(transcript) for transcript in transcripts]
    if practice_mode == "reorder":
        return process_transcript_segments(raw_segments, mode="word_ordering")
    if practice_mode == "cloze":
        return process_transcript_segments(raw_segments, mode="cloze")
    return process_transcript_segments(raw_segments, mode="dictation")


def _total_sentences_for_mode(transcripts: list[Transcript], practice_mode: PracticeMode) -> int:
    if practice_mode == "reorder":
        return len(_segments_for_practice_mode(transcripts, practice_mode))
    return len(transcripts)


def _session_payload(
    attempt: DictationAttempt, *, resumed: bool, sentence_results: list[dict]
) -> dict:
    return {
        "session_id": attempt.id,
        "total_sentences": attempt.total_sentences,
        "current_sentence_index": attempt.current_sentence_index if resumed else 0,
        "resumed": resumed,
        "sentence_results": sentence_results,
        "practice_mode": attempt.practice_mode,
    }


_PUNCT_RE = re.compile(r"""^[.,!?;:'"()\[\]{}\-—–…]+$""")


def _tokenize_for_reorder(text: str) -> list[str]:
    """Split text into tokens, attaching trailing punctuation to the preceding word."""
    raw = text.strip().split()
    merged: list[str] = []
    for tok in raw:
        if merged and _PUNCT_RE.match(tok):
            merged[-1] += tok
        else:
            merged.append(tok)
    return merged


def _shuffle_tokens(tokens: list[str]) -> list[str]:
    """Return a shuffled copy guaranteed to differ from the original (when ≥2 distinct)."""
    shuffled = tokens[:]
    distinct = len(set(tokens))
    if distinct < 2:
        return shuffled
    for _ in range(20):
        random.shuffle(shuffled)
        if shuffled != tokens:
            return shuffled
    return shuffled


def _score_reorder(
    submitted: list[str], expected: list[str]
) -> tuple[float, list[ReorderTokenResult]]:
    """Score by comparing token positions. Returns (score, per-token results)."""
    results: list[ReorderTokenResult] = []
    correct = 0
    for i, exp in enumerate(expected):
        got = submitted[i] if i < len(submitted) else ""
        is_correct = got.strip().lower() == exp.strip().lower()
        if is_correct:
            correct += 1
        results.append(ReorderTokenResult(index=i, token=got, expected=exp, is_correct=is_correct))
    score = correct / len(expected) if expected else 1.0
    return score, results


class DictationService:
    def __init__(self, repo: DictationRepository):
        self.repo = repo

    # ── Shared loaders / completion ──────────────────────────────────────────

    async def _load_attempt(self, session_id: str, user_id: str) -> DictationAttempt:
        attempt = await self.repo.get_attempt(session_id, user_id)
        if not attempt:
            raise NotFoundError("Session not found")
        return attempt

    async def _load_transcripts(self, video_id: str) -> list[Transcript]:
        rows = await self.repo.get_transcripts_ordered(video_id)
        if not rows:
            raise NotFoundError("No transcripts found for this video")
        return rows

    async def _ensure_video_access(self, user: User, video_id: str) -> None:
        video = await self.repo.get_video(video_id)
        if not video:
            raise NotFoundError("Video not found")
        publish_status = getattr(video, "publish_status", None) or "published"
        if publish_status == "published" or user.is_admin:
            return
        if await self.repo.has_user_practice(user.id, video_id):
            return
        raise NotFoundError("Video not found")

    async def _complete_from_sentences(self, attempt: DictationAttempt) -> None:
        """Aggregate the mean sentence score, mark completed, and populate analytics."""
        all_sentences = await self.repo.get_sentences_by_attempt(attempt.id)
        scores = [s.score for s in all_sentences if s.score is not None]
        attempt.score = sum(scores) / len(scores) if scores else 0.0
        attempt.status = "completed"
        attempt.completed_at = datetime.now(UTC)
        _populate_analytics(attempt, list(all_sentences))

    # ── Session lifecycle ────────────────────────────────────────────────────

    async def create_session(self, user: User, video_id: str, practice_mode: PracticeMode) -> dict:
        await self._ensure_video_access(user, video_id)
        transcripts = await self.repo.get_transcripts_by_video(video_id)
        if not transcripts:
            raise NotFoundError("No transcripts found for this video")

        total_sentences = _total_sentences_for_mode(transcripts, practice_mode)

        attempt = await self.repo.get_attempt_by_user_video(user.id, video_id)

        if attempt and attempt.status == "in_progress":
            # Mode-switch detection: a different mode resets the in-progress session.
            if attempt.practice_mode != practice_mode:
                attempt.score = None
                attempt.current_sentence_index = 0
                attempt.total_sentences = total_sentences
                attempt.practice_mode = practice_mode
                attempt.error_summary = None
                attempt.completed_at = None
                attempt.correct_words = None
                attempt.total_words = None
                attempt.duration_seconds = None
                await self.repo.delete_sentences_for_attempt(attempt.id)
                await self.repo.commit()
                await self.repo.refresh(attempt)
                await publish_user_recommendation_event(
                    user.id,
                    "attempt_started",
                    attempt.video_id,
                )
                return _session_payload(attempt, resumed=False, sentence_results=[])

            # Same mode — resume where the user left off.
            sentences = await self.repo.get_sentences_by_attempt_ordered(attempt.id)
            return _session_payload(
                attempt,
                resumed=True,
                sentence_results=[
                    {
                        "sentence_index": s.sentence_index,
                        "score": s.score,
                        "word_diff": s.word_diff,
                    }
                    for s in sentences
                ],
            )

        if attempt and attempt.status == "completed":
            # Re-practice — reset the existing row, including the mode.
            attempt.status = "in_progress"
            attempt.score = None
            attempt.current_sentence_index = 0
            attempt.total_sentences = total_sentences
            attempt.practice_mode = practice_mode
            attempt.error_summary = None
            attempt.completed_at = None
            attempt.correct_words = None
            attempt.total_words = None
            attempt.duration_seconds = None
            await self.repo.delete_sentences_for_attempt(attempt.id)
            await self.repo.commit()
            await self.repo.refresh(attempt)
            await publish_user_recommendation_event(
                user.id,
                "attempt_started",
                attempt.video_id,
            )
            return _session_payload(attempt, resumed=False, sentence_results=[])

        # First time — create new row.
        attempt = DictationAttempt(
            user_id=user.id,
            video_id=video_id,
            status="in_progress",
            total_sentences=total_sentences,
            practice_mode=practice_mode,
        )
        self.repo.add(attempt)
        await self.repo.commit()
        await self.repo.refresh(attempt)
        await publish_user_recommendation_event(
            user.id,
            "attempt_started",
            attempt.video_id,
        )
        return _session_payload(attempt, resumed=False, sentence_results=[])

    async def submit_answer(
        self, user: User, session_id: str, body: SubmitAnswerRequest
    ) -> SentenceResultResponse:
        attempt = await self._load_attempt(session_id, user.id)

        transcript = await self.repo.get_transcript_by_position(
            attempt.video_id, body.sentence_index
        )
        if not transcript:
            raise NotFoundError("Transcript segment not found")

        if body.skipped:
            correct_words = transcript.text.strip().split()
            diffs = [WordDiffItem(word=w, status="missing") for w in correct_words]
            final_score = 0.0
        else:
            diffs, score = compute_word_diff(body.user_input, transcript.text)
            hint_penalty = body.hints_used * 0.05
            final_score = max(0, score - hint_penalty)

        existing_sentence = await self.repo.get_sentence(session_id, body.sentence_index)

        if existing_sentence:
            existing_sentence.user_input = body.user_input
            existing_sentence.original_text = transcript.text
            existing_sentence.score = final_score
            existing_sentence.word_diff = [d.model_dump() for d in diffs]
            existing_sentence.hints_used = body.hints_used
            existing_sentence.replay_count = body.replay_count
        else:
            self.repo.add(
                DictationSentence(
                    attempt_id=session_id,
                    sentence_index=body.sentence_index,
                    user_input=body.user_input,
                    original_text=transcript.text,
                    score=final_score,
                    word_diff=[d.model_dump() for d in diffs],
                    hints_used=body.hints_used,
                    replay_count=body.replay_count,
                )
            )

        new_index = body.sentence_index + 1
        if new_index > attempt.current_sentence_index:
            attempt.current_sentence_index = new_index

        was_completed = attempt.status == "completed"
        if attempt.total_sentences and attempt.current_sentence_index >= attempt.total_sentences:
            await self._complete_from_sentences(attempt)

        await self.repo.commit()
        if not was_completed and attempt.status == "completed":
            await publish_user_recommendation_event(
                user.id,
                "attempt_completed",
                attempt.video_id,
            )

        if body.skipped:
            difficulty_map = get_word_difficulty_map(transcript.text, language=transcript.language)
            return SentenceResultResponse(
                sentence_index=body.sentence_index,
                score=0.0,
                word_diffs=diffs,
                correct_count=0,
                wrong_count=0,
                missing_count=len(diffs),
                is_skipped=True,
                original_text=transcript.text,
                video_id=attempt.video_id,
                audio_start_time=transcript.start_time,
                word_difficulty=difficulty_map,
            )

        correct = sum(1 for d in diffs if d.status == "correct")
        wrong = sum(1 for d in diffs if d.status == "wrong")
        missing = sum(1 for d in diffs if d.status == "missing")
        difficulty_map = get_word_difficulty_map(transcript.text, language=transcript.language)

        return SentenceResultResponse(
            sentence_index=body.sentence_index,
            score=final_score,
            word_diffs=diffs,
            correct_count=correct,
            wrong_count=wrong,
            missing_count=missing,
            original_text=transcript.text,
            video_id=attempt.video_id,
            audio_start_time=transcript.start_time,
            word_difficulty=difficulty_map,
        )

    async def complete_session(self, user: User, session_id: str) -> dict:
        attempt = await self._load_attempt(session_id, user.id)

        if attempt.status == "completed":
            return {"status": "completed", "score": round((attempt.score or 0) * 100, 1)}

        all_sentences = await self.repo.get_sentences_by_attempt(session_id)
        scores = [s.score for s in all_sentences if s.score is not None]
        attempt.score = sum(scores) / len(scores) if scores else 0.0
        attempt.status = "completed"
        attempt.completed_at = datetime.now(UTC)
        attempt.current_sentence_index = attempt.total_sentences or len(all_sentences)
        _populate_analytics(attempt, list(all_sentences))
        await self.repo.commit()
        await publish_user_recommendation_event(
            user.id,
            "attempt_completed",
            attempt.video_id,
        )

        return {"status": "completed", "score": round(attempt.score * 100, 1)}

    # ── History & summary ────────────────────────────────────────────────────

    async def get_history(
        self,
        user: User,
        status_value: str,
        page: int,
        video_id: str | None,
        practice_mode: str | None,
        from_date: str | None,
        to_date: str | None,
    ) -> HistoryPaginatedResponse:
        db_status = status_value.replace("-", "_")
        from_dt = datetime.fromisoformat(from_date) if from_date else None
        to_dt = datetime.fromisoformat(to_date + "T23:59:59") if to_date else None

        filters = dict(
            user_id=user.id,
            status=db_status,
            video_id=video_id,
            practice_mode=practice_mode,
            from_dt=from_dt,
            to_dt=to_dt,
        )

        total = await self.repo.count_attempts(**filters)
        total_pages = max(1, math.ceil(total / PAGE_SIZE))

        rows = await self.repo.get_attempts_with_video(
            order_by_completed=(db_status == "completed"),
            page=page,
            page_size=PAGE_SIZE,
            **filters,
        )

        items = []
        for attempt, video_title, video_thumbnail in rows:
            progress_str = f"{attempt.current_sentence_index}/{attempt.total_sentences or 0}"
            items.append(
                HistoryAttemptResponse(
                    attempt_id=attempt.id,
                    video_id=attempt.video_id,
                    status=attempt.status,
                    score=attempt.score,
                    progress_str=progress_str,
                    video_title=video_title,
                    video_thumbnail=video_thumbnail or "",
                    practice_mode=attempt.practice_mode or "sentence",
                    duration_seconds=attempt.duration_seconds,
                    error_summary=attempt.error_summary,
                    updated_at=attempt.updated_at.isoformat(),
                    completed_at=attempt.completed_at.isoformat() if attempt.completed_at else None,
                )
            )

        return HistoryPaginatedResponse(
            items=items, total=total, page=page, total_pages=total_pages
        )

    async def get_attempts_summary(
        self,
        user: User,
        video_id: str | None,
        practice_mode: str | None,
        from_date: str | None,
        to_date: str | None,
    ) -> dict:
        from_dt = datetime.fromisoformat(from_date) if from_date else None
        to_dt = datetime.fromisoformat(to_date + "T23:59:59") if to_date else None

        row = await self.repo.get_attempts_summary(
            user_id=user.id,
            status="completed",
            video_id=video_id,
            practice_mode=practice_mode,
            from_dt=from_dt,
            to_dt=to_dt,
        )
        return {
            "total_sessions": row[0],
            "total_duration_seconds": row[1],
            "average_score": round(float(row[2]) * 100, 1),
        }

    # ── Cloze ────────────────────────────────────────────────────────────────

    async def get_cloze_chunks(self, user: User, session_id: str) -> ClozeChunksResponse:
        attempt = await self._load_attempt(session_id, user.id)
        transcripts = await self._load_transcripts(attempt.video_id)
        chunks = build_chunks(transcripts)
        return ClozeChunksResponse(practice_mode="cloze", chunks=chunks)

    async def submit_cloze(
        self, user: User, session_id: str, body: ClozeSubmitRequest
    ) -> ClozeResultResponse:
        attempt = await self._load_attempt(session_id, user.id)
        transcripts = await self._load_transcripts(attempt.video_id)
        chunks = build_chunks(transcripts)

        if body.chunk_index < 0 or body.chunk_index >= len(chunks):
            raise BadRequestError("chunk_index out of range")

        chunk = chunks[body.chunk_index]
        expected = get_chunk_answers(chunk)

        given = list(body.answers) + [""] * max(0, len(expected) - len(body.answers))
        given = given[: len(expected)]

        results: list[ClozeBlankResult] = []
        correct_count = 0
        for i, (exp, got) in enumerate(zip(expected, given)):
            is_correct = exp.strip().lower() == got.strip().lower() and got.strip() != ""
            if is_correct:
                correct_count += 1
            results.append(
                ClozeBlankResult(
                    blank_index=i,
                    given=got,
                    expected=exp,
                    status="correct" if is_correct else "wrong",
                )
            )

        score = correct_count / len(expected) if expected else 1.0

        existing = await self.repo.get_sentence(session_id, body.chunk_index)
        word_diff_payload = [r.model_dump() for r in results]
        user_input_joined = " ".join(given).strip()

        if existing:
            existing.user_input = user_input_joined
            existing.original_text = " ".join(expected)
            existing.score = score
            existing.word_diff = word_diff_payload
        else:
            self.repo.add(
                DictationSentence(
                    attempt_id=session_id,
                    sentence_index=body.chunk_index,
                    user_input=user_input_joined,
                    original_text=" ".join(expected),
                    score=score,
                    word_diff=word_diff_payload,
                )
            )

        new_index = body.chunk_index + 1
        if new_index > attempt.current_sentence_index:
            attempt.current_sentence_index = new_index

        if attempt.total_sentences != len(chunks):
            attempt.total_sentences = len(chunks)

        was_completed = attempt.status == "completed"
        if attempt.current_sentence_index >= len(chunks):
            await self._complete_from_sentences(attempt)

        await self.repo.commit()
        if not was_completed and attempt.status == "completed":
            await publish_user_recommendation_event(
                user.id,
                "attempt_completed",
                attempt.video_id,
            )

        return ClozeResultResponse(
            chunk_index=body.chunk_index,
            score=score,
            correct_count=correct_count,
            total_count=len(expected),
            results=results,
            audio_start_time=chunk.start_time,
            audio_end_time=chunk.end_time,
        )

    async def get_cloze_full(
        self, user: User, session_id: str, difficulty: str
    ) -> ClozeFullResponse:
        attempt = await self._load_attempt(session_id, user.id)
        transcripts = await self._load_transcripts(attempt.video_id)
        segments, total_blanks = build_full_cloze(transcripts, difficulty)
        return ClozeFullResponse(
            practice_mode="cloze",
            difficulty=difficulty,
            total_blanks=total_blanks,
            segments=segments,
        )

    async def submit_cloze_all(
        self, user: User, session_id: str, body: ClozeSubmitAllRequest
    ) -> ClozeSubmitAllResponse:
        attempt = await self._load_attempt(session_id, user.id)
        transcripts = await self._load_transcripts(attempt.video_id)
        segments, total_blanks = build_full_cloze(transcripts, body.difficulty)
        expected = get_full_cloze_answers(segments)

        given = list(body.answers) + [""] * max(0, len(expected) - len(body.answers))
        given = given[: len(expected)]

        results: list[ClozeBlankResult] = []
        correct_count = 0
        for i, (exp, got) in enumerate(zip(expected, given)):
            is_correct = exp.strip().lower() == got.strip().lower() and got.strip() != ""
            if is_correct:
                correct_count += 1
            results.append(
                ClozeBlankResult(
                    blank_index=i,
                    given=got,
                    expected=exp,
                    status="correct" if is_correct else "wrong",
                )
            )

        score = correct_count / len(expected) if expected else 1.0

        blank_to_result: dict[int, ClozeBlankResult] = {r.blank_index: r for r in results}
        segment_scores: list[SegmentScore] = []
        for seg in segments:
            seg_results = [
                blank_to_result[tok.blank_index]
                for tok in seg.tokens
                if tok.is_blank
                and tok.blank_index is not None
                and tok.blank_index in blank_to_result
            ]
            seg_correct = sum(1 for r in seg_results if r.status == "correct")
            seg_score = seg_correct / len(seg_results) if seg_results else 1.0
            segment_scores.append(
                SegmentScore(
                    segment_index=seg.segment_index,
                    start_time=seg.start_time,
                    end_time=seg.end_time,
                    score=seg_score,
                    blank_results=seg_results,
                )
            )

        was_completed = attempt.status == "completed"
        attempt.score = score
        attempt.status = "completed"
        attempt.completed_at = datetime.now(UTC)
        attempt.current_sentence_index = attempt.total_sentences or 0

        cloze_sentences = await self.repo.get_sentences_by_attempt(session_id)
        _populate_analytics(attempt, list(cloze_sentences))
        await self.repo.commit()
        if not was_completed:
            await publish_user_recommendation_event(
                user.id,
                "attempt_completed",
                attempt.video_id,
            )

        return ClozeSubmitAllResponse(
            score=score,
            correct_count=correct_count,
            total_count=len(expected),
            results=results,
            segment_scores=segment_scores,
        )

    # ── Reorder ──────────────────────────────────────────────────────────────

    async def get_reorder_challenges(
        self, user: User, session_id: str
    ) -> ReorderChallengesResponse:
        attempt = await self._load_attempt(session_id, user.id)
        stored_transcripts = await self._load_transcripts(attempt.video_id)
        segments = _segments_for_practice_mode(stored_transcripts, "reorder")
        if attempt.total_sentences != len(segments):
            attempt.total_sentences = len(segments)
            await self.repo.commit()

        challenges: list[ReorderChallenge] = []
        for idx, segment in enumerate(segments):
            tokens = _tokenize_for_reorder(segment.text)
            shuffled = _shuffle_tokens(tokens)
            challenges.append(
                ReorderChallenge(
                    sentence_index=idx,
                    start_time=segment.start,
                    end_time=segment.end,
                    shuffled_tokens=[ReorderToken(index=i, text=s) for i, s in enumerate(shuffled)],
                    token_count=len(tokens),
                )
            )

        return ReorderChallengesResponse(total_sentences=len(challenges), challenges=challenges)

    async def submit_reorder(
        self, user: User, session_id: str, body: ReorderSubmitRequest
    ) -> ReorderSentenceResult:
        attempt = await self._load_attempt(session_id, user.id)
        stored_transcripts = await self._load_transcripts(attempt.video_id)
        segments = _segments_for_practice_mode(stored_transcripts, "reorder")
        if attempt.total_sentences != len(segments):
            attempt.total_sentences = len(segments)

        if body.sentence_index < 0 or body.sentence_index >= len(segments):
            raise BadRequestError("sentence_index out of range")

        segment = segments[body.sentence_index]
        expected_tokens = _tokenize_for_reorder(segment.text)
        score, token_results = _score_reorder(body.ordered_tokens, expected_tokens)

        word_diff_payload = [r.model_dump() for r in token_results]
        user_input_joined = " ".join(body.ordered_tokens)

        existing = await self.repo.get_sentence(session_id, body.sentence_index)

        if existing:
            existing.user_input = user_input_joined
            existing.original_text = segment.text
            existing.score = score
            existing.word_diff = word_diff_payload
        else:
            self.repo.add(
                DictationSentence(
                    attempt_id=session_id,
                    sentence_index=body.sentence_index,
                    user_input=user_input_joined,
                    original_text=segment.text,
                    score=score,
                    word_diff=word_diff_payload,
                )
            )

        new_index = body.sentence_index + 1
        if new_index > attempt.current_sentence_index:
            attempt.current_sentence_index = new_index

        was_completed = attempt.status == "completed"
        if attempt.total_sentences and attempt.current_sentence_index >= attempt.total_sentences:
            await self._complete_from_sentences(attempt)

        await self.repo.commit()
        if not was_completed and attempt.status == "completed":
            await publish_user_recommendation_event(
                user.id,
                "attempt_completed",
                attempt.video_id,
            )

        return ReorderSentenceResult(
            sentence_index=body.sentence_index,
            score=score,
            token_results=token_results,
            correct_order=expected_tokens,
            start_time=segment.start,
            end_time=segment.end,
        )

    async def submit_reorder_all(
        self, user: User, session_id: str, body: ReorderSubmitAllRequest
    ) -> ReorderSubmitAllResponse:
        attempt = await self._load_attempt(session_id, user.id)
        stored_transcripts = await self._load_transcripts(attempt.video_id)
        segments = _segments_for_practice_mode(stored_transcripts, "reorder")
        if attempt.total_sentences != len(segments):
            attempt.total_sentences = len(segments)

        sentence_results: list[ReorderSentenceResult] = []
        total_correct = 0
        total_tokens = 0

        for idx, segment in enumerate(segments):
            expected_tokens = _tokenize_for_reorder(segment.text)
            submitted = body.answers[idx] if idx < len(body.answers) else []
            score, token_results = _score_reorder(submitted, expected_tokens)

            correct_in_sentence = sum(1 for r in token_results if r.is_correct)
            total_correct += correct_in_sentence
            total_tokens += len(expected_tokens)

            word_diff_payload = [r.model_dump() for r in token_results]
            user_input_joined = " ".join(submitted)

            existing = await self.repo.get_sentence(session_id, idx)

            if existing:
                existing.user_input = user_input_joined
                existing.original_text = segment.text
                existing.score = score
                existing.word_diff = word_diff_payload
            else:
                self.repo.add(
                    DictationSentence(
                        attempt_id=session_id,
                        sentence_index=idx,
                        user_input=user_input_joined,
                        original_text=segment.text,
                        score=score,
                        word_diff=word_diff_payload,
                    )
                )

            sentence_results.append(
                ReorderSentenceResult(
                    sentence_index=idx,
                    score=score,
                    token_results=token_results,
                    correct_order=expected_tokens,
                    start_time=segment.start,
                    end_time=segment.end,
                )
            )

        overall_score = total_correct / total_tokens if total_tokens else 1.0

        was_completed = attempt.status == "completed"
        attempt.score = overall_score
        attempt.status = "completed"
        attempt.completed_at = datetime.now(UTC)
        attempt.current_sentence_index = len(segments)

        all_sentences = await self.repo.get_sentences_by_attempt(session_id)
        _populate_analytics(attempt, list(all_sentences))
        await self.repo.commit()
        if not was_completed:
            await publish_user_recommendation_event(
                user.id,
                "attempt_completed",
                attempt.video_id,
            )

        return ReorderSubmitAllResponse(
            score=overall_score,
            correct_count=total_correct,
            total_count=total_tokens,
            sentence_results=sentence_results,
        )
