"""Mode-aware transcript segmentation for dictation practice.

The STT and YouTube transcript providers return sentence-like chunks, but those
chunks are not always suitable for practice. This module keeps the provider
timestamps, splits overlong chunks, and only merges short neighbors when the
result remains comfortable for the selected practice mode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.services.youtube_service import TranscriptSegment

PracticeMode = Literal["dictation", "cloze", "word_ordering"]


@dataclass(frozen=True)
class SegmentLimits:
    min_words: int
    preferred_min_words: int
    preferred_max_words: int
    hard_max_words: int
    global_hard_max_words: int
    max_pause_ms: int
    max_duration_seconds: float


SEGMENT_LIMITS: dict[PracticeMode, SegmentLimits] = {
    "dictation": SegmentLimits(
        min_words=4,
        preferred_min_words=8,
        preferred_max_words=18,
        hard_max_words=20,
        global_hard_max_words=35,
        max_pause_ms=1200,
        max_duration_seconds=16.0,
    ),
    "cloze": SegmentLimits(
        min_words=6,
        preferred_min_words=12,
        preferred_max_words=28,
        hard_max_words=30,
        global_hard_max_words=35,
        max_pause_ms=1500,
        max_duration_seconds=24.0,
    ),
    "word_ordering": SegmentLimits(
        min_words=3,
        preferred_min_words=5,
        preferred_max_words=12,
        hard_max_words=14,
        global_hard_max_words=35,
        max_pause_ms=1000,
        max_duration_seconds=12.0,
    ),
}

WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)?")
STRONG_END_RE = re.compile(r"[.!?;:][\"')\]]*$")
SPLIT_TOKEN_RE = re.compile(r"\S+")
SUBORDINATE_MARKERS = {
    "although",
    "because",
    "while",
    "whereas",
    "since",
    "unless",
    "however",
    "therefore",
    "which",
    "who",
    "that",
    "when",
    "then",
    "but",
    "and",
    "or",
    "so",
}


def count_words(text: str) -> int:
    return len(WORD_RE.findall(text))


def tokenize_words(text: str) -> list[str]:
    return [match.group(0) for match in WORD_RE.finditer(text)]


def _tokenize_with_spans(text: str) -> list[re.Match[str]]:
    return list(SPLIT_TOKEN_RE.finditer(text))


def _segment_from_text(text: str, *, start: float, end: float) -> TranscriptSegment:
    start = max(0.0, start)
    end = max(start + 0.05, end)
    return TranscriptSegment(text=text.strip(), start=start, duration=end - start)


def _segment_from_token_range(
    segment: TranscriptSegment,
    tokens: list[re.Match[str]],
    start_idx: int,
    end_idx: int,
) -> TranscriptSegment:
    text = segment.text[tokens[start_idx].start() : tokens[end_idx - 1].end()].strip()
    total = max(len(tokens), 1)
    duration = max(segment.duration, 0.05)
    chunk_start = segment.start + duration * (start_idx / total)
    chunk_end = segment.start + duration * (end_idx / total)
    return _segment_from_text(text, start=chunk_start, end=chunk_end)


def _find_candidate_split_points(tokens: list[re.Match[str]]) -> dict[int, int]:
    """Return split indexes with lower score = better boundary."""
    candidates: dict[int, int] = {}
    for index in range(1, len(tokens)):
        prev = tokens[index - 1].group(0)
        current = tokens[index].group(0).lower().strip(".,!?;:\"'()[]")

        if re.search(r"[.!?;:]$", prev):
            candidates[index] = min(candidates.get(index, 99), 0)
        elif re.search(r"[,—–-]$", prev):
            candidates[index] = min(candidates.get(index, 99), 1)
        elif current in SUBORDINATE_MARKERS:
            candidates[index] = min(candidates.get(index, 99), 2)

    return candidates


def _choose_split_point(
    *,
    start: int,
    total: int,
    preferred_max_words: int,
    hard_max_words: int,
    candidates: dict[int, int],
) -> int:
    remaining = total - start
    if remaining <= hard_max_words:
        return total

    min_end = min(total, start + 3)
    preferred_end = min(total, start + preferred_max_words)
    hard_end = min(total, start + hard_max_words)

    valid = [idx for idx in candidates if min_end <= idx <= hard_end]
    if valid:
        return min(valid, key=lambda idx: (candidates[idx], abs(idx - preferred_end), -idx))

    return hard_end


def split_long_segment(
    segment: TranscriptSegment,
    *,
    mode: PracticeMode = "dictation",
    limits: SegmentLimits | None = None,
) -> list[TranscriptSegment]:
    config = limits or SEGMENT_LIMITS[mode]
    tokens = _tokenize_with_spans(segment.text)
    if len(tokens) <= config.hard_max_words:
        return [segment]

    candidates = _find_candidate_split_points(tokens)
    chunks: list[TranscriptSegment] = []
    start = 0

    while start < len(tokens):
        end = _choose_split_point(
            start=start,
            total=len(tokens),
            preferred_max_words=config.preferred_max_words,
            hard_max_words=config.hard_max_words,
            candidates=candidates,
        )
        if end <= start:
            end = min(len(tokens), start + config.hard_max_words)
        chunks.append(_segment_from_token_range(segment, tokens, start, end))
        start = end

    return chunks


def _merge_text(a: str, b: str) -> str:
    if not a:
        return b.strip()
    if not b:
        return a.strip()
    return f"{a.strip()} {b.strip()}".strip()


def can_merge_segments(a: TranscriptSegment, b: TranscriptSegment, config: SegmentLimits) -> bool:
    merged_word_count = count_words(a.text) + count_words(b.text)
    pause_ms = max(0.0, (b.start - a.end) * 1000.0)
    duration = b.end - a.start

    if merged_word_count > config.hard_max_words:
        return False
    if merged_word_count > config.global_hard_max_words:
        return False
    if pause_ms > config.max_pause_ms:
        return False
    if duration > config.max_duration_seconds:
        return False
    if STRONG_END_RE.search(a.text) and count_words(a.text) >= config.min_words:
        return False

    return True


def merge_short_segments(
    segments: list[TranscriptSegment],
    *,
    mode: PracticeMode = "dictation",
    limits: SegmentLimits | None = None,
) -> list[TranscriptSegment]:
    config = limits or SEGMENT_LIMITS[mode]
    if len(segments) <= 1:
        return segments

    result: list[TranscriptSegment] = []
    i = 0
    while i < len(segments):
        current = segments[i]

        if count_words(current.text) < config.preferred_min_words and i + 1 < len(segments):
            nxt = segments[i + 1]
            if can_merge_segments(current, nxt, config):
                result.append(
                    _segment_from_text(
                        _merge_text(current.text, nxt.text), start=current.start, end=nxt.end
                    )
                )
                i += 2
                continue

        result.append(current)
        i += 1

    if len(result) >= 2 and count_words(result[-1].text) < config.min_words:
        last = result[-1]
        prev = result[-2]
        if can_merge_segments(prev, last, config):
            result[-2] = _segment_from_text(
                _merge_text(prev.text, last.text), start=prev.start, end=last.end
            )
            result.pop()

    return result


def validate_segments(
    segments: list[TranscriptSegment],
    *,
    mode: PracticeMode = "dictation",
    limits: SegmentLimits | None = None,
) -> list[TranscriptSegment]:
    config = limits or SEGMENT_LIMITS[mode]
    validated: list[TranscriptSegment] = []

    for segment in segments:
        if not segment.text.strip():
            continue

        pieces = split_long_segment(segment, mode=mode, limits=config)
        for piece in pieces:
            start = max(0.0, piece.start)
            end = max(start + 0.05, piece.end)
            if validated and start < validated[-1].end:
                start = validated[-1].end
                end = max(start + 0.05, piece.end)
            validated.append(_segment_from_text(piece.text, start=start, end=end))

    return validated


def process_transcript_segments(
    segments: list[TranscriptSegment],
    *,
    mode: PracticeMode = "dictation",
) -> list[TranscriptSegment]:
    """Split long segments first, then safely merge short adjacent segments."""
    if not segments:
        return []

    config = SEGMENT_LIMITS[mode]
    ordered = sorted(segments, key=lambda seg: (seg.start, seg.end))

    split_segments: list[TranscriptSegment] = []
    for segment in ordered:
        split_segments.extend(split_long_segment(segment, mode=mode, limits=config))

    merged = merge_short_segments(split_segments, mode=mode, limits=config)
    return validate_segments(merged, mode=mode, limits=config)
