"""CEFR text level analyzer using spaCy (NLP) and wordfreq (word frequency).

Algorithm
---------
Combines three independent feature groups into a weighted composite score (0–100),
then maps the score to a CEFR band (A1–C2).

Feature groups
~~~~~~~~~~~~~~
1. Vocabulary difficulty  (weight 50 %)
   - Average Zipf frequency of content words  (nouns, verbs, adj, adv)
     · Zipf scale: 7 = extremely common ("the"), 0 = extremely rare
     · A1 texts cluster around 5.5–6.5; C2 texts around 3.5–4.5
   - Rare-word ratio  (words with Zipf < 3.5)

2. Sentence length complexity  (weight 30 %)
   - Average number of tokens per sentence

3. Syntactic depth  (weight 20 %)
   - Average maximum dependency-tree depth per sentence

CEFR thresholds (composite score)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
  A1  : 0–20
  A2  : 20–35
  B1  : 35–50
  B2  : 50–65
  C1  : 65–80
  C2  : 80–100

Reference Zipf values (English)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
  "the"        → 7.5   "be"       → 7.3
  "house"      → 5.7   "happy"    → 4.8
  "analyze"    → 4.1   "conclude" → 4.0
  "ubiquitous" → 3.5   "ephemeral"→ 2.9
"""

from __future__ import annotations

import logging
import re
from statistics import mean
from typing import Any

import spacy
from wordfreq import zipf_frequency

logger = logging.getLogger(__name__)

# ─── spaCy model (loaded once at module import) ───────────────────────────────

_nlp: spacy.Language | None = None


def _get_nlp() -> spacy.Language:
    global _nlp
    if _nlp is None:
        try:
            _nlp = spacy.load("en_core_web_sm")
        except OSError:
            logger.warning(
                "spaCy model 'en_core_web_sm' not found. "
                "Run: python -m spacy download en_core_web_sm"
            )
            raise
    return _nlp


# ─── Constants ────────────────────────────────────────────────────────────────

CONTENT_POS = {"NOUN", "VERB", "ADJ", "ADV"}

# CEFR bands ordered from easiest to hardest
CEFR_THRESHOLDS = [
    (20, "A1"),
    (35, "A2"),
    (50, "B1"),
    (65, "B2"),
    (80, "C1"),
]

CEFR_LABELS = {
    "A1": "Beginner",
    "A2": "Elementary",
    "B1": "Intermediate",
    "B2": "Upper Intermediate",
    "C1": "Advanced",
    "C2": "Proficient",
}


# ─── Internal helpers ─────────────────────────────────────────────────────────


def _dep_depth(token: spacy.tokens.Token) -> int:
    """Recursively compute the depth of a token in the dependency tree."""
    if not list(token.children):
        return 1
    return 1 + max(_dep_depth(child) for child in token.children)


def _max_sent_depth(sent: spacy.tokens.Span) -> int:
    """Return the maximum dependency depth of a sentence."""
    roots = [t for t in sent if t.dep_ == "ROOT"]
    if not roots:
        return 1
    return _dep_depth(roots[0])


# ─── Feature extraction ───────────────────────────────────────────────────────


def extract_features(text: str, language: str = "en") -> dict:
    """Extract all NLP and frequency features from text.

    Returns a dict with:
      avg_zipf        – mean Zipf frequency of content words  (higher = easier)
      rare_ratio      – fraction of content words with Zipf < 3.5
      avg_sent_length – mean token count per sentence
      avg_dep_depth   – mean max dependency depth per sentence
      n_sentences     – number of sentences analysed
      n_content_words – number of content words analysed
    """
    nlp = _get_nlp()
    doc = nlp(text)

    sentences = list(doc.sents)
    n_sentences = len(sentences)

    # ── Sentence length ───────────────────────────────────────────────────────
    sent_lengths = [sum(1 for t in sent if not t.is_space and not t.is_punct) for sent in sentences]
    avg_sent_length = mean(sent_lengths) if sent_lengths else 0.0

    # ── Syntactic depth ────────────────────────────────────────────────────────
    depths = [_max_sent_depth(sent) for sent in sentences]
    avg_dep_depth = mean(depths) if depths else 0.0

    # ── Vocabulary frequency ───────────────────────────────────────────────────
    content_tokens = [
        t
        for t in doc
        if t.pos_ in CONTENT_POS and not t.is_stop and t.lemma_.isalpha() and len(t.lemma_) > 1
    ]

    zipf_scores: list[float] = []
    for token in content_tokens:
        lemma = token.lemma_.lower()
        freq = zipf_frequency(lemma, language, wordlist="best", minimum=0.0)
        zipf_scores.append(freq)

    avg_zipf = mean(zipf_scores) if zipf_scores else 4.5
    rare_ratio = sum(1 for f in zipf_scores if f < 3.5) / len(zipf_scores) if zipf_scores else 0.0

    return {
        "avg_zipf": round(avg_zipf, 3),
        "rare_ratio": round(rare_ratio, 3),
        "avg_sent_length": round(avg_sent_length, 2),
        "avg_dep_depth": round(avg_dep_depth, 2),
        "n_sentences": n_sentences,
        "n_content_words": len(content_tokens),
    }


# ─── Composite scoring ────────────────────────────────────────────────────────


def compute_difficulty_score(features: dict) -> float:
    """Map extracted features to a 0–100 difficulty score.

    0  = extremely easy (A1)
    100 = extremely hard (C2)
    """
    # ── 1. Vocabulary score (0–100) ───────────────────────────────────────────
    # Zipf 6.5 → score ~0   (very easy)
    # Zipf 3.5 → score ~75  (very hard)
    # Each 1.0 Zipf unit ≈ 25 difficulty points
    zipf_score = max(0.0, min(100.0, (6.5 - features["avg_zipf"]) * 25.0))

    # Rare-word ratio amplifier: 30% rare words → +15 points
    rare_bonus = features["rare_ratio"] * 50.0
    vocab_score = min(100.0, zipf_score + rare_bonus)

    # ── 2. Sentence length score (0–100) ─────────────────────────────────────
    # 5 tokens → 0    20 tokens → 75    30+ tokens → 100
    length_score = max(0.0, min(100.0, (features["avg_sent_length"] - 5.0) * 5.0))

    # ── 3. Syntactic depth score (0–100) ─────────────────────────────────────
    # depth 2 → 0    depth 7 → 75    depth 9+ → 100
    depth_score = max(0.0, min(100.0, (features["avg_dep_depth"] - 2.0) * 15.0))

    # ── Weighted composite ────────────────────────────────────────────────────
    composite = 0.50 * vocab_score + 0.30 * length_score + 0.20 * depth_score
    return round(composite, 2)


# ─── CEFR mapping ─────────────────────────────────────────────────────────────


def score_to_cefr(score: float) -> str:
    """Map 0–100 difficulty score to CEFR band."""
    for threshold, level in CEFR_THRESHOLDS:
        if score < threshold:
            return level
    return "C2"


def score_to_cefr_level(score: float) -> str:
    """Map audio difficulty score to CEFR using inclusive requested bands."""
    if score <= 20:
        return "A1"
    if score <= 35:
        return "A2"
    if score <= 50:
        return "B1"
    if score <= 68:
        return "B2"
    if score <= 84:
        return "C1"
    return "C2"


def _clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    return max(lower, min(upper, value))


def _segment_text(segment: Any) -> str:
    return str(getattr(segment, "text", "") or "")


def _segment_start(segment: Any) -> float:
    return float(getattr(segment, "start", getattr(segment, "start_time", 0.0)) or 0.0)


def _segment_end(segment: Any) -> float:
    if hasattr(segment, "end"):
        return float(getattr(segment, "end") or 0.0)
    return float(getattr(segment, "end_time", _segment_start(segment)) or _segment_start(segment))


WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)?")
UNCLEAR_RE = re.compile(r"\[(?:inaudible|unclear|music|applause)\]|\?{2,}|_{2,}", re.I)
COMPLEX_MARKERS = {
    "because",
    "although",
    "while",
    "whereas",
    "since",
    "unless",
    "however",
    "therefore",
    "which",
    "who",
    "that",
}


def _word_count(text: str) -> int:
    return len(WORD_RE.findall(text))


def _scale(value: float, low: float, high: float) -> float:
    if high <= low:
        return 0.0
    return _clamp(((value - low) / (high - low)) * 100.0)


def _fallback_lexical_metrics(text: str, language: str) -> tuple[float, float, float]:
    words = [w.lower() for w in WORD_RE.findall(text)]
    if not words:
        return 30.0, 0.0, 0.0

    scores = [zipf_frequency(word, language, wordlist="best", minimum=0.0) for word in words]
    unknown_ratio = sum(1 for score in scores if score <= 0.5) / len(scores)
    rare_ratio = sum(1 for score in scores if score < 3.5) / len(scores)
    long_word_ratio = sum(1 for word in words if len(word) >= 9) / len(words)
    lexical = _clamp((rare_ratio * 65.0) + (unknown_ratio * 25.0) + (long_word_ratio * 25.0))
    return lexical, unknown_ratio, rare_ratio


def _sentence_complexity_score(text: str, avg_words: float, max_words: int) -> float:
    lowered = text.lower()
    marker_count = sum(
        len(re.findall(rf"\b{re.escape(marker)}\b", lowered)) for marker in COMPLEX_MARKERS
    )
    punctuation_count = len(re.findall(r"[,;:]", text))
    long_segment_score = _scale(max_words, 14, 34)
    avg_length_score = _scale(avg_words, 8, 24)
    marker_score = _scale(marker_count, 2, 12)
    punctuation_score = _scale(punctuation_count, 3, 20)
    return round(
        _clamp(
            0.35 * avg_length_score
            + 0.25 * long_segment_score
            + 0.25 * marker_score
            + 0.15 * punctuation_score
        ),
        2,
    )


def _recommended_modes(level: str) -> dict[str, bool]:
    if level in {"A1", "A2"}:
        return {"dictation": True, "cloze": True, "wordOrdering": True, "shadowing": False}
    if level in {"B1", "B2"}:
        return {"dictation": True, "cloze": True, "wordOrdering": level == "B1", "shadowing": False}
    return {"dictation": True, "cloze": True, "wordOrdering": False, "shadowing": True}


def calculate_audio_difficulty(
    video: Any,
    transcript_segments: list[Any],
    options: dict | None = None,
) -> dict:
    """Score transcript/audio difficulty for practice and persistable metadata."""
    options = options or {}
    language = options.get("language") or getattr(video, "language", "en") or "en"
    duration_seconds = float(options.get("duration_seconds") or getattr(video, "duration", 0) or 0)

    texts = [
        _segment_text(segment).strip()
        for segment in transcript_segments
        if _segment_text(segment).strip()
    ]
    full_text = " ".join(texts)
    word_counts = [_word_count(text) for text in texts]
    total_words = sum(word_counts)
    segment_count = len(word_counts)
    avg_words = mean(word_counts) if word_counts else 0.0
    max_words = max(word_counts) if word_counts else 0

    if duration_seconds <= 0 and transcript_segments:
        starts = [_segment_start(segment) for segment in transcript_segments]
        ends = [_segment_end(segment) for segment in transcript_segments]
        duration_seconds = max(0.0, max(ends) - min(starts))
    duration_minutes = max(duration_seconds / 60.0, 0.1)
    words_per_minute = total_words / duration_minutes if total_words else 0.0

    avg_words_score = _scale(avg_words, 6, 24)
    max_words_score = _scale(max_words, 14, 34)
    wpm_score = _scale(words_per_minute, 80, 190)

    try:
        nlp_features = (
            extract_features(full_text, language=language)
            if full_text and total_words >= 10
            else {}
        )
        avg_zipf = nlp_features.get("avg_zipf", 4.5)
        rare_ratio = nlp_features.get("rare_ratio", 0.0)
        lexical_score = _clamp(((6.2 - avg_zipf) * 28.0) + (rare_ratio * 65.0))
        unknown_ratio = _fallback_lexical_metrics(full_text, language)[1]
        proper_noun_ratio = 0.0
    except Exception:
        lexical_score, unknown_ratio, _rare_ratio = _fallback_lexical_metrics(full_text, language)
        proper_noun_ratio = 0.0

    sentence_complexity = _sentence_complexity_score(full_text, avg_words, max_words)
    unclear_hits = len(UNCLEAR_RE.findall(full_text))
    zero_duration = sum(
        1 for segment in transcript_segments if _segment_end(segment) <= _segment_start(segment)
    )
    audio_quality_penalty = _clamp((unclear_hits * 10.0) + (zero_duration * 8.0))
    transcript_confidence = _clamp(100.0 - audio_quality_penalty)

    score = round(
        _clamp(
            0.20 * avg_words_score
            + 0.10 * max_words_score
            + 0.20 * wpm_score
            + 0.25 * lexical_score
            + 0.15 * sentence_complexity
            + 0.10 * audio_quality_penalty
        ),
        2,
    )
    level = score_to_cefr_level(score)

    factors = {
        "avgWordsPerSegment": round(avg_words, 2),
        "maxWordsPerSegment": max_words,
        "wordsPerMinute": round(words_per_minute, 2),
        "lexicalDifficulty": round(lexical_score, 2),
        "sentenceComplexity": sentence_complexity,
        "transcriptConfidence": round(transcript_confidence, 2),
        "audioQualityPenalty": round(audio_quality_penalty, 2),
        "unknownWordRatio": round(unknown_ratio, 4),
        "properNounRatio": round(proper_noun_ratio, 4),
    }
    explanation = [
        f"Average segment length is {avg_words:.1f} words.",
        f"Speech rate is {words_per_minute:.0f} WPM.",
        f"Vocabulary difficulty score is {lexical_score:.0f}/100.",
    ]
    if max_words > 30:
        explanation.append(
            f"Longest segment has {max_words} words, which is difficult for dictation."
        )
    if audio_quality_penalty > 0:
        explanation.append("Transcript/audio quality signals add a difficulty penalty.")

    return {
        "score": score,
        "level": level,
        "label": CEFR_LABELS[level],
        "factors": factors,
        "explanation": explanation,
        "recommendedModes": _recommended_modes(level),
        "segmentCount": segment_count,
        "totalWordCount": total_words,
    }


def difficulty_update_values(analysis: dict) -> dict:
    """Map an audio difficulty result to Video column names."""
    factors = analysis.get("factors") or {}
    return {
        "difficulty_score": analysis.get("score"),
        "difficulty_level": analysis.get("level"),
        "difficulty_label": analysis.get("label"),
        "difficulty_factors": factors,
        "avg_words_per_segment": factors.get("avgWordsPerSegment"),
        "max_words_per_segment": factors.get("maxWordsPerSegment"),
        "words_per_minute": factors.get("wordsPerMinute"),
        "segment_count": analysis.get("segmentCount"),
        "total_word_count": analysis.get("totalWordCount"),
    }


# ─── Public API ───────────────────────────────────────────────────────────────


def analyze_level(text: str, language: str = "en") -> str:
    """Analyze text and return its estimated CEFR level (A1–C2).

    Args:
        text: The full transcript or passage to analyse.
        language: BCP-47 language code (default "en"). Used by wordfreq.

    Returns:
        CEFR level string, e.g. "B1".
    """
    if not text or len(text.split()) < 10:
        logger.debug("Text too short for reliable analysis, defaulting to A2")
        return "A2"

    try:
        features = extract_features(text, language=language)
        score = compute_difficulty_score(features)
        level = score_to_cefr(score)

        logger.info(
            "Level analysis: level=%s score=%.1f zipf=%.2f rare=%.0f%% "
            "sent_len=%.1f dep_depth=%.1f n_words=%d",
            level,
            score,
            features["avg_zipf"],
            features["rare_ratio"] * 100,
            features["avg_sent_length"],
            features["avg_dep_depth"],
            features["n_content_words"],
        )
        return level

    except Exception:
        logger.exception("Level analysis failed, defaulting to A2")
        return "A2"


# ─── BR-02.5 / BR-02.6: FK Grade + Speech Rate CEFR estimation ──────────────

_FK_CEFR_MAP = ["A1", "A2", "B1", "B2"]  # index 0–3
_FK_GRADE_BREAKS = [4, 6, 9]  # grade <4→A1, <6→A2, <9→B1, ≥9→B2


def _fk_grade_to_cefr_index(grade: float) -> int:
    """Map Flesch-Kincaid Grade Level to a 0–3 CEFR index."""
    for i, brk in enumerate(_FK_GRADE_BREAKS):
        if grade < brk:
            return i
    return 3  # B2


def _wpm_adjust(cefr_idx: int, wpm: float) -> int:
    """Shift CEFR index by speech rate: slow → -1, fast → +1."""
    if wpm < 100:
        return max(0, cefr_idx - 1)
    if wpm > 140:
        return min(3, cefr_idx + 1)
    return cefr_idx


def estimate_cefr_with_speech_rate(
    text: str,
    duration_seconds: float,
    language: str = "en",
) -> str:
    """Estimate CEFR using Flesch-Kincaid readability + WPM adjustment.

    BR-02.5: FK Grade → CEFR base level (English only).
    BR-02.6: WPM adjustment (±1 level).

    Returns a clean CEFR string like "B1".
    """
    if language != "en" or not text or len(text.split()) < 10:
        # Fall back to the spaCy-based analyzer for non-English
        return analyze_level(text, language=language)

    try:
        import textstat

        grade = textstat.flesch_kincaid_grade(text)
        base_idx = _fk_grade_to_cefr_index(grade)

        # Speech rate adjustment
        words = text.split()
        duration_min = max(duration_seconds / 60.0, 0.1)
        wpm = len(words) / duration_min
        final_idx = _wpm_adjust(base_idx, wpm)

        level = _FK_CEFR_MAP[final_idx]
        logger.info(
            "CEFR estimation: FK_grade=%.1f base=%s wpm=%.0f final=%s",
            grade,
            _FK_CEFR_MAP[base_idx],
            wpm,
            level,
        )
        return level

    except Exception:
        logger.exception("CEFR estimation failed, falling back to spaCy")
        return analyze_level(text, language=language)


def analyze_level_detailed(text: str, language: str = "en") -> dict:
    """Like analyze_level but returns the full feature breakdown.

    Useful for debugging or exposing via an API endpoint.
    """
    if not text or len(text.split()) < 10:
        return {
            "level": "A2",
            "score": 0.0,
            "features": {},
            "note": "Text too short for analysis",
        }

    try:
        features = extract_features(text, language=language)
        score = compute_difficulty_score(features)
        level = score_to_cefr(score)

        return {
            "level": level,
            "score": score,
            "features": features,
        }
    except Exception as exc:
        logger.exception("Detailed level analysis failed")
        return {
            "level": "A2",
            "score": 0.0,
            "features": {},
            "error": str(exc),
        }
