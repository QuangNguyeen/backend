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
from statistics import mean

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
    sent_lengths = [
        sum(1 for t in sent if not t.is_space and not t.is_punct)
        for sent in sentences
    ]
    avg_sent_length = mean(sent_lengths) if sent_lengths else 0.0

    # ── Syntactic depth ────────────────────────────────────────────────────────
    depths = [_max_sent_depth(sent) for sent in sentences]
    avg_dep_depth = mean(depths) if depths else 0.0

    # ── Vocabulary frequency ───────────────────────────────────────────────────
    content_tokens = [
        t for t in doc
        if t.pos_ in CONTENT_POS
        and not t.is_stop
        and t.lemma_.isalpha()
        and len(t.lemma_) > 1
    ]

    zipf_scores: list[float] = []
    for token in content_tokens:
        lemma = token.lemma_.lower()
        freq = zipf_frequency(lemma, language, wordlist="best", minimum=0.0)
        zipf_scores.append(freq)

    avg_zipf = mean(zipf_scores) if zipf_scores else 4.5
    rare_ratio = (
        sum(1 for f in zipf_scores if f < 3.5) / len(zipf_scores)
        if zipf_scores else 0.0
    )

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
    composite = (
        0.50 * vocab_score
        + 0.30 * length_score
        + 0.20 * depth_score
    )
    return round(composite, 2)


# ─── CEFR mapping ─────────────────────────────────────────────────────────────

def score_to_cefr(score: float) -> str:
    """Map 0–100 difficulty score to CEFR band."""
    for threshold, level in CEFR_THRESHOLDS:
        if score < threshold:
            return level
    return "C2"


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
            level, score,
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
