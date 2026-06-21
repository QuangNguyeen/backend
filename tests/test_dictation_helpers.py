"""Tests for pure dictation helpers (no DB, no HTTP)."""

from app.models.dictation import DictationAttempt, DictationSentence
from app.services.dictation_session_service import (
    _populate_analytics,
    _score_reorder,
    _shuffle_tokens,
    _tokenize_for_reorder,
)


def test_tokenize_attaches_trailing_punctuation():
    assert _tokenize_for_reorder("Hello, world!") == ["Hello,", "world!"]


def test_tokenize_empty_string():
    assert _tokenize_for_reorder("   ") == []


def test_shuffle_preserves_multiset_and_length():
    tokens = ["a", "b", "c", "d"]
    shuffled = _shuffle_tokens(tokens)
    assert sorted(shuffled) == sorted(tokens)
    assert len(shuffled) == len(tokens)


def test_shuffle_single_distinct_token_returns_copy():
    assert _shuffle_tokens(["x"]) == ["x"]


def test_score_reorder_positional_match():
    score, results = _score_reorder(["the", "cat", "sat"], ["the", "cat", "sat"])
    assert score == 1.0
    assert all(r.is_correct for r in results)


def test_score_reorder_partial_and_missing():
    # Submitted shorter than expected; second token wrong.
    score, results = _score_reorder(["the", "dog"], ["the", "cat", "sat"])
    assert results[0].is_correct is True
    assert results[1].is_correct is False
    assert results[2].token == ""  # missing position
    assert score == 1 / 3


def test_populate_analytics_counts_errors_and_top_words():
    attempt = DictationAttempt(id="a1", user_id="u1", video_id="v1")
    sentences = [
        DictationSentence(
            attempt_id="a1",
            sentence_index=0,
            word_diff=[
                {"status": "correct", "word": "the"},
                {"status": "wrong", "expected": "cat"},
                {"status": "missing", "expected": "sat"},
            ],
        ),
        DictationSentence(
            attempt_id="a1",
            sentence_index=1,
            word_diff=[{"status": "wrong", "expected": "cat"}],
        ),
    ]

    _populate_analytics(attempt, sentences)

    assert attempt.total_words == 4
    assert attempt.correct_words == 1
    assert attempt.error_summary["total_wrong"] == 2
    assert attempt.error_summary["total_missing"] == 1
    # "cat" appears twice → ranked first in top_words.
    assert attempt.error_summary["top_words"][0] == {"word": "cat", "count": 2}
