import re
import unicodedata
from difflib import SequenceMatcher

from app.schemas.dictation import WordDiffItem


def _normalize(word: str) -> str:
    """Normalize a word for comparison before diffing."""
    # 1. Normalize quotes (curved -> straight)
    clean_word = (
        word.lower()
        .replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("`", "'")
        .replace("\u00b4", "'")
    )

    # 2. Strip diacritics (e.g., café -> cafe)
    nfd_form = unicodedata.normalize("NFD", clean_word)
    ascii_word = "".join(c for c in nfd_form if unicodedata.category(c) != "Mn")

    # 3. Strip remaining punctuation (including apostrophes)
    return re.sub(r"[^\w\s]", "", ascii_word).strip()


def compute_word_diff(user_input: str, correct_text: str) -> tuple[list[WordDiffItem], float]:
    """Compare user input with the correct transcript using LCS-style diffing.

    Uses difflib.SequenceMatcher over normalized word arrays so that a single
    missed or extra word does not cascade into shift-errors on every following
    word. Diacritics and curly apostrophes are normalized before comparison.

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
        user_slice_raw = user_words_raw[j1:j2]

        if tag == "equal":
            for raw in expected_slice_raw:
                diffs.append(WordDiffItem(word=raw, status="correct"))
                correct_count += 1

        elif tag == "replace":
            # Pair user words against expected words as wrong,
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
            # Words the user typed that aren't in the transcript
            for raw in user_slice_raw:
                diffs.append(WordDiffItem(word=raw, status="extra", expected=""))

    score = correct_count / len(correct_words_raw)
    return diffs, round(score, 4)
