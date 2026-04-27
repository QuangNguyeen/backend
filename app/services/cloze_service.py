"""Build full-transcript cloze views with difficulty-based blank selection.

Strategy:
1. Tokenise every transcript segment with spaCy (or regex fallback).
2. Collect a global pool of blank candidates across ALL segments.
3. Select blanks proportionally to the chosen difficulty level, spread evenly.
4. Assign globally-unique blank indices so the frontend can submit one flat
   answers array.

The same algorithm runs on read AND on submit (deterministic), so we don't
have to persist the cloze layout.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

from app.models.video import Transcript
from app.schemas.cloze import ClozeChunk, ClozeSegment, ClozeToken

PREFERRED_POS = {"NOUN", "VERB", "ADJ", "ADV", "PROPN"}
FALLBACK_MIN_LEN = 5

DIFFICULTY_RATIOS = {
    "easy": 0.10,
    "medium": 0.25,
    "hard": 0.40,
}

try:
    from app.services.text_analysis_service import nlp as _nlp
except Exception:
    _nlp = None


def _pick_evenly(candidates: Sequence[int], n: int) -> list[int]:
    """Return up to `n` indices spread evenly through `candidates`."""
    if len(candidates) <= n:
        return list(candidates)
    step = len(candidates) / n
    return [candidates[int(i * step)] for i in range(n)]


def _tokenize_spacy(text: str) -> tuple[list[dict], list[int]]:
    """Tokenise with spaCy, return (tokens, candidate_indices)."""
    doc = _nlp(text)  # type: ignore[misc]
    tokens: list[dict] = []
    candidates: list[int] = []
    for tok in doc:
        if tok.is_space:
            continue
        is_candidate = (
            tok.is_alpha
            and not tok.is_stop
            and (tok.pos_ in PREFERRED_POS or len(tok.text) >= FALLBACK_MIN_LEN)
        )
        tokens.append({
            "text": tok.text,
            "with_ws": tok.text_with_ws,
            "is_punct": tok.is_punct,
        })
        if is_candidate:
            candidates.append(len(tokens) - 1)
    return tokens, candidates


_WORD_RE = re.compile(r"(\w+|[^\w\s]+|\s+)")


def _tokenize_regex(text: str) -> tuple[list[dict], list[int]]:
    """Fallback tokeniser when spaCy isn't available."""
    raw_tokens = _WORD_RE.findall(text)
    tokens: list[dict] = []
    candidates: list[int] = []
    for i, raw in enumerate(raw_tokens):
        if raw.isspace():
            if tokens:
                tokens[-1]["with_ws"] = tokens[-1]["with_ws"] + raw
            continue
        is_word = raw.isalpha()
        tokens.append({
            "text": raw,
            "with_ws": raw,
            "is_punct": not is_word,
        })
        if is_word and len(raw) >= FALLBACK_MIN_LEN:
            candidates.append(len(tokens) - 1)
        if i + 1 < len(raw_tokens) and raw_tokens[i + 1].isspace():
            tokens[-1]["with_ws"] = raw + raw_tokens[i + 1]
    return tokens, candidates


def _tokenize(text: str) -> tuple[list[dict], list[int]]:
    if _nlp is not None:
        return _tokenize_spacy(text)
    return _tokenize_regex(text)


# ─── Full-transcript cloze (new) ────────────────────────────────────────────

def build_full_cloze(
    transcripts: Iterable[Transcript],
    difficulty: str = "medium",
) -> tuple[list[ClozeSegment], int]:
    """Build a single flat cloze view over ALL transcripts.

    Returns (segments, total_blanks).
    """
    ratio = DIFFICULTY_RATIOS.get(difficulty, 0.25)
    sorted_segs = sorted(transcripts, key=lambda t: t.index)

    all_seg_data: list[dict] = []
    global_candidates: list[tuple[int, int]] = []

    for seg_i, transcript in enumerate(sorted_segs):
        text = transcript.text.strip()
        if not text:
            all_seg_data.append({"transcript": transcript, "tokens": [], "candidates": []})
            continue
        tokens, candidates = _tokenize(text)
        for tok_idx in candidates:
            global_candidates.append((seg_i, tok_idx))
        all_seg_data.append({
            "transcript": transcript,
            "tokens": tokens,
            "candidates": candidates,
        })

    total_blanks_target = max(1, int(len(global_candidates) * ratio))
    selected_indices = _pick_evenly(list(range(len(global_candidates))), total_blanks_target)
    selected_set: set[tuple[int, int]] = {global_candidates[i] for i in selected_indices}

    blank_counter = 0
    segments: list[ClozeSegment] = []

    for seg_i, seg_data in enumerate(all_seg_data):
        t = seg_data["transcript"]
        seg_tokens: list[ClozeToken] = []
        seg_blank_count = 0

        for tok_i, tok in enumerate(seg_data["tokens"]):
            if (seg_i, tok_i) in selected_set:
                seg_tokens.append(ClozeToken(
                    text=tok["text"],
                    is_blank=True,
                    blank_index=blank_counter,
                ))
                blank_counter += 1
                seg_blank_count += 1
            else:
                seg_tokens.append(ClozeToken(
                    text=tok["with_ws"],
                    is_blank=False,
                ))

        segments.append(ClozeSegment(
            segment_index=t.index,
            start_time=t.start_time,
            end_time=t.end_time,
            tokens=seg_tokens,
            blank_count=seg_blank_count,
        ))

    return segments, blank_counter


def get_full_cloze_answers(segments: list[ClozeSegment]) -> list[str]:
    """Extract expected answers ordered by global blank_index."""
    pairs: list[tuple[int, str]] = []
    for seg in segments:
        for tok in seg.tokens:
            if tok.is_blank and tok.blank_index is not None:
                pairs.append((tok.blank_index, tok.text))
    pairs.sort(key=lambda p: p[0])
    return [text for _, text in pairs]


# ─── Legacy chunk-based API (kept for backward compat) ──────────────────────

DEFAULT_CHUNK_SIZE = 1
DEFAULT_BLANKS_PER_CHUNK = 3


def _build_chunk(
    chunk_index: int,
    transcripts: Sequence[Transcript],
    blanks_per_chunk: int,
) -> ClozeChunk:
    merged_text = " ".join(t.text.strip() for t in transcripts).strip()
    if _nlp is not None:
        tokens, candidates = _tokenize_spacy(merged_text)
    else:
        tokens, candidates = _tokenize_regex(merged_text)

    blank_indices = _pick_evenly(candidates, blanks_per_chunk)
    blank_set = set(blank_indices)
    blank_index_for_token: dict[int, int] = {
        tok_idx: i for i, tok_idx in enumerate(blank_indices)
    }

    cloze_tokens: list[ClozeToken] = []
    for i, tok in enumerate(tokens):
        if i in blank_set:
            cloze_tokens.append(ClozeToken(
                text=tok["text"],
                is_blank=True,
                blank_index=blank_index_for_token[i],
            ))
        else:
            cloze_tokens.append(ClozeToken(
                text=tok["with_ws"],
                is_blank=False,
            ))

    return ClozeChunk(
        chunk_index=chunk_index,
        start_time=transcripts[0].start_time,
        end_time=transcripts[-1].end_time,
        tokens=cloze_tokens,
        blank_count=len(blank_indices),
    )


def build_chunks(
    transcripts: Iterable[Transcript],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    blanks_per_chunk: int = DEFAULT_BLANKS_PER_CHUNK,
) -> list[ClozeChunk]:
    sorted_segs = sorted(transcripts, key=lambda t: t.index)
    chunks: list[ClozeChunk] = []
    for i in range(0, len(sorted_segs), chunk_size):
        group = sorted_segs[i:i + chunk_size]
        if not group:
            continue
        chunks.append(_build_chunk(i // chunk_size, group, blanks_per_chunk))
    return chunks


def get_chunk_answers(chunk: ClozeChunk) -> list[str]:
    pairs = [(t.blank_index, t.text) for t in chunk.tokens if t.is_blank]
    pairs.sort(key=lambda p: p[0] or 0)
    return [text for _, text in pairs]
