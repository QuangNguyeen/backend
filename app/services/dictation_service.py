import re

from app.schemas.dictation import WordDiffItem


def _normalize(word: str) -> str:
    """Normalize a word for comparison: lowercase, strip punctuation."""
    return re.sub(r'[^\w\s]', '', word.lower()).strip()


def compute_word_diff(user_input: str, correct_text: str) -> tuple[list[WordDiffItem], float]:
    """Compare user input with correct text word by word.

    Punctuation is ignored during comparison so "it" matches "it."
    Returns a list of WordDiffItem and a score between 0.0 and 1.0.
    """
    user_words = user_input.strip().split()
    correct_words = correct_text.strip().split()

    diffs: list[WordDiffItem] = []
    correct_count = 0

    max_len = max(len(user_words), len(correct_words))
    if max_len == 0:
        return [], 1.0

    for i in range(max_len):
        if i < len(user_words) and i < len(correct_words):
            if _normalize(user_words[i]) == _normalize(correct_words[i]):
                diffs.append(WordDiffItem(word=correct_words[i], status="correct"))
                correct_count += 1
            else:
                diffs.append(WordDiffItem(
                    word=user_words[i], status="wrong", expected=correct_words[i],
                ))
        elif i >= len(user_words):
            # User missed a word
            diffs.append(WordDiffItem(word=correct_words[i], status="missing"))
        else:
            # User typed extra word
            diffs.append(WordDiffItem(word=user_words[i], status="extra", expected=""))

    score = correct_count / len(correct_words) if correct_words else 0.0
    return diffs, round(score, 4)
