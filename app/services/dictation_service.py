import re
from difflib import SequenceMatcher

from app.schemas.dictation import WordDiffItem


CONTRACTIONS_MAP: dict[str, str] = {
    "i'm": "i am", "you're": "you are", "he's": "he is", "she's": "she is",
    "it's": "it is", "we're": "we are", "they're": "they are",
    "isn't": "is not", "aren't": "are not", "wasn't": "was not", "weren't": "were not",
    "haven't": "have not", "hasn't": "has not", "hadn't": "had not",
    "won't": "will not", "wouldn't": "would not", "don't": "do not",
    "doesn't": "does not", "didn't": "did not",
    "can't": "cannot", "couldn't": "could not", "shouldn't": "should not", "mightn't": "might not",
    "mustn't": "must not",
    "i've": "i have", "you've": "you have", "we've": "we have", "they've": "they have",
    "i'll": "i will", "you'll": "you will", "he'll": "he will", "she'll": "she will",
    "we'll": "we will", "they'll": "they will",
    "i'd": "i would", "you'd": "you would", "he'd": "he would", "she'd": "she would",
    "we'd": "we would", "they'd": "they would",
}

# Reverse lookup: "i am" -> "i'm" (as tuple of words for sequence matching)
_EXPANDED_TO_CONTRACTED: dict[tuple[str, ...], str] = {
    tuple(expanded.split()): contracted
    for contracted, expanded in CONTRACTIONS_MAP.items()
}


def _normalize(word: str) -> str:
    """Normalize a word for comparison: lowercase, strip punctuation (preserve apostrophes)."""
    # Preserve apostrophes so contractions like "don't" survive normalization
    return re.sub(r"[^\w'\s]", "", word.lower()).strip()


def _chunks_match_via_contraction(
    expected_chunk: list[str], user_chunk: list[str]
) -> bool:
    """Return True if one chunk is a contraction and the other is its expansion.

    Examples:
        ["i'm"] vs ["i", "am"]            -> True
        ["i", "am"] vs ["i'm"]            -> True
        ["don't"] vs ["do", "not"]        -> True
        ["i'm", "happy"] vs ["i", "am", "happy"] -> True (scans both sides)
    """
    if not expected_chunk or not user_chunk:
        return False

    i, j = 0, 0
    while i < len(expected_chunk) and j < len(user_chunk):
        e = expected_chunk[i]
        u = user_chunk[j]

        # Direct match
        if e == u:
            i += 1
            j += 1
            continue

        # expected is contraction, user typed expansion
        if e in CONTRACTIONS_MAP:
            expansion = tuple(CONTRACTIONS_MAP[e].split())
            if tuple(user_chunk[j : j + len(expansion)]) == expansion:
                i += 1
                j += len(expansion)
                continue

        # user is contraction, expected is expansion
        if u in CONTRACTIONS_MAP:
            expansion = tuple(CONTRACTIONS_MAP[u].split())
            if tuple(expected_chunk[i : i + len(expansion)]) == expansion:
                i += len(expansion)
                j += 1
                continue

        return False

    return i == len(expected_chunk) and j == len(user_chunk)


def compute_word_diff(
    user_input: str, correct_text: str
) -> tuple[list[WordDiffItem], float]:
    """Compare user input with the correct transcript using LCS-style diffing.

    Uses difflib.SequenceMatcher over normalized word arrays so that a single
    missed or extra word does not cascade into shift-errors on every following
    word. English contractions are treated as equal to their expansion
    (e.g. "I'm" == "I am", "don't" == "do not").

    Returns a list of WordDiffItem plus a score in [0.0, 1.0] computed as
    correct_count / len(expected_words).
    """
    user_words_raw = user_input.strip().split()
    correct_words_raw = correct_text.strip().split()

    if not correct_words_raw and not user_words_raw:
        return [], 1.0
    if not correct_words_raw:
        # Nothing expected but user typed something — all extras
        return (
            [WordDiffItem(word=w, status="extra", expected="") for w in user_words_raw],
            0.0,
        )

    user_norm = [_normalize(w) for w in user_words_raw]
    correct_norm = [_normalize(w) for w in correct_words_raw]

    matcher = SequenceMatcher(a=correct_norm, b=user_norm, autojunk=False)

    diffs: list[WordDiffItem] = []
    correct_count = 0

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        expected_slice_raw = correct_words_raw[i1:i2]
        expected_slice_norm = correct_norm[i1:i2]
        user_slice_raw = user_words_raw[j1:j2]
        user_slice_norm = user_norm[j1:j2]

        if tag == "equal":
            for raw in expected_slice_raw:
                diffs.append(WordDiffItem(word=raw, status="correct"))
                correct_count += 1

        elif tag == "replace":
            # Try to resolve the whole replace block via contraction mapping
            if _chunks_match_via_contraction(expected_slice_norm, user_slice_norm):
                # Emit one "correct" entry per expected word so scoring stays
                # aligned with the transcript word count.
                for raw in expected_slice_raw:
                    diffs.append(WordDiffItem(word=raw, status="correct"))
                    correct_count += 1
                continue

            # Otherwise: pair user words against expected words as wrong,
            # then account for length mismatch with missing / extra.
            pair_len = min(len(expected_slice_raw), len(user_slice_raw))
            for k in range(pair_len):
                diffs.append(
                    WordDiffItem(
                        word=user_slice_raw[k],
                        status="wrong",
                        expected=expected_slice_raw[k],
                    )
                )
            # Leftover expected words → missing
            for raw in expected_slice_raw[pair_len:]:
                diffs.append(WordDiffItem(word=raw, status="missing"))
            # Leftover user words → extra
            for raw in user_slice_raw[pair_len:]:
                diffs.append(WordDiffItem(word=raw, status="extra", expected=""))

        elif tag == "delete":
            # Expected words the user skipped
            for raw in expected_slice_raw:
                diffs.append(WordDiffItem(word=raw, status="missing"))

        elif tag == "insert":
            # Words the user typed that aren't in the transcript.
            # Check if the inserted run is actually the expansion of an adjacent
            # contraction already emitted as "correct" — handled inside replace,
            # so here we just mark as extra.
            for raw in user_slice_raw:
                diffs.append(WordDiffItem(word=raw, status="extra", expected=""))

    score = correct_count / len(correct_words_raw)
    return diffs, round(score, 4)
