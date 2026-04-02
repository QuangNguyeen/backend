"""Text difficulty analysis service using spaCy and word frequency.

Assigns CEFR levels (A1-C2) based on:
1. Word frequency (how common words are in everyday language)
2. Sentence complexity (average sentence length, clause depth)
3. Vocabulary diversity (type-token ratio)
4. Syntactic complexity (POS tag distribution, dependency depth)
"""

import spacy
from wordfreq import zipf_frequency

nlp = spacy.load("en_core_web_sm")

# CEFR level thresholds — calibrated against known graded texts
# Each level maps to a max score. The score is computed from multiple metrics.
CEFR_LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]

# Zipf frequency thresholds: higher zipf = more common word
# A1 learners know very common words (zipf >= 5.5), C2 knows rare ones (zipf < 2.5)
FREQUENCY_BANDS = {
    "high": 5.0,     # Very common: the, is, go, like
    "medium": 4.0,   # Common: explain, dangerous, system
    "low": 3.0,      # Uncommon: reluctant, coherent, arbitrary
    "rare": 0.0,     # Rare: juxtaposition, ephemeral, perfunctory
}


def analyze_text(text: str) -> dict:
    """Analyze text and return CEFR level with detailed metrics.

    Args:
        text: English text to analyze

    Returns:
        Dict with keys: level, score, metrics
    """
    doc = nlp(text)

    tokens = [token for token in doc if not token.is_punct and not token.is_space]
    if not tokens:
        return {"level": "A1", "score": 0.0, "metrics": {}}

    sentences = list(doc.sents)

    metrics = {
        "word_count": len(tokens),
        "sentence_count": len(sentences),
        **_word_frequency_metrics(tokens),
        **_sentence_complexity_metrics(sentences, tokens),
        **_vocabulary_diversity_metrics(tokens),
        **_syntactic_metrics(tokens),
    }

    score = _compute_difficulty_score(metrics)
    level = _score_to_cefr(score)

    metrics["difficulty_score"] = round(score, 2)

    return {"level": level, "score": round(score, 2), "metrics": metrics}


def detect_level(text: str) -> str:
    """Return just the CEFR level string."""
    return analyze_text(text)["level"]


def _word_frequency_metrics(tokens: list) -> dict:
    """Analyze word frequency distribution using Zipf scale."""
    frequencies = []
    rare_words = []
    for token in tokens:
        if token.is_stop or len(token.text) <= 2:
            continue
        freq = zipf_frequency(token.lemma_.lower(), "en")
        frequencies.append(freq)
        if freq < FREQUENCY_BANDS["low"]:
            rare_words.append(token.text)

    if not frequencies:
        return {
            "avg_word_frequency": 7.0,
            "low_freq_ratio": 0.0,
            "rare_word_ratio": 0.0,
            "rare_words": [],
        }

    total = len(frequencies)
    low_freq = sum(1 for f in frequencies if f < FREQUENCY_BANDS["medium"])
    rare = sum(1 for f in frequencies if f < FREQUENCY_BANDS["low"])

    return {
        "avg_word_frequency": round(sum(frequencies) / total, 2),
        "low_freq_ratio": round(low_freq / total, 2),
        "rare_word_ratio": round(rare / total, 2),
        "rare_words": list(set(rare_words))[:20],
    }


def _sentence_complexity_metrics(sentences: list, tokens: list) -> dict:
    """Analyze sentence-level complexity."""
    if not sentences:
        return {"avg_sentence_length": 0.0, "max_sentence_length": 0, "long_sentence_ratio": 0.0}

    lengths = []
    for sent in sentences:
        sent_tokens = [t for t in sent if not t.is_punct and not t.is_space]
        lengths.append(len(sent_tokens))

    avg_len = sum(lengths) / len(lengths) if lengths else 0
    long_sentences = sum(1 for l in lengths if l > 15)

    return {
        "avg_sentence_length": round(avg_len, 1),
        "max_sentence_length": max(lengths) if lengths else 0,
        "long_sentence_ratio": round(long_sentences / len(lengths), 2) if lengths else 0.0,
    }


def _vocabulary_diversity_metrics(tokens: list) -> dict:
    """Type-token ratio measures vocabulary richness."""
    if not tokens:
        return {"type_token_ratio": 0.0}

    types = set(t.lemma_.lower() for t in tokens)
    return {"type_token_ratio": round(len(types) / len(tokens), 2)}


def _syntactic_metrics(tokens: list) -> dict:
    """Analyze syntactic complexity via dependency tree depth and POS distribution."""
    if not tokens:
        return {"avg_dep_depth": 0.0, "complex_pos_ratio": 0.0}

    # Dependency depth: how deep each token is in the parse tree
    depths = []
    for token in tokens:
        depth = 0
        current = token
        while current.head != current:
            depth += 1
            current = current.head
        depths.append(depth)

    # Complex POS tags indicate more advanced grammar
    complex_pos = {"SCONJ", "CCONJ", "AUX", "ADV", "ADP"}
    complex_count = sum(1 for t in tokens if t.pos_ in complex_pos)

    return {
        "avg_dep_depth": round(sum(depths) / len(depths), 2) if depths else 0.0,
        "complex_pos_ratio": round(complex_count / len(tokens), 2),
    }


def _compute_difficulty_score(metrics: dict) -> float:
    """Combine metrics into a single difficulty score (0-100).

    Weights:
    - Word frequency: 40% (most predictive of CEFR level)
    - Sentence complexity: 25%
    - Vocabulary diversity: 15%
    - Syntactic complexity: 20%
    """
    score = 0.0

    # Word frequency (40%): lower avg frequency = harder
    avg_freq = metrics.get("avg_word_frequency", 7.0)
    # Map zipf 7.0 (very easy) -> 0, zipf 2.0 (very hard) -> 100
    freq_score = max(0, min(100, (7.0 - avg_freq) * 20))
    # Boost for rare words
    rare_ratio = metrics.get("rare_word_ratio", 0)
    freq_score += rare_ratio * 30
    score += min(100, freq_score) * 0.40

    # Sentence complexity (25%)
    avg_sent_len = metrics.get("avg_sentence_length", 0)
    # Map: 5 words -> 0 (easy), 25+ words -> 100 (hard)
    sent_score = max(0, min(100, (avg_sent_len - 5) * 5))
    long_ratio = metrics.get("long_sentence_ratio", 0)
    sent_score += long_ratio * 30
    score += min(100, sent_score) * 0.25

    # Vocabulary diversity (15%)
    ttr = metrics.get("type_token_ratio", 0)
    # Higher TTR = more diverse vocabulary = harder
    # Map: 0.3 -> 0 (repetitive), 0.9 -> 100 (diverse)
    ttr_score = max(0, min(100, (ttr - 0.3) * 166))
    score += ttr_score * 0.15

    # Syntactic complexity (20%)
    dep_depth = metrics.get("avg_dep_depth", 0)
    # Map: depth 2.0 -> 0 (simple), depth 4.5 -> 100 (complex)
    syn_score = max(0, min(100, (dep_depth - 2.0) * 40))
    complex_pos = metrics.get("complex_pos_ratio", 0)
    syn_score += complex_pos * 40
    score += min(100, syn_score) * 0.20

    return max(0, min(100, score))


def _score_to_cefr(score: float) -> str:
    """Map difficulty score to CEFR level."""
    if score < 20:
        return "A1"
    elif score < 35:
        return "A2"
    elif score < 50:
        return "B1"
    elif score < 65:
        return "B2"
    elif score < 80:
        return "C1"
    else:
        return "C2"