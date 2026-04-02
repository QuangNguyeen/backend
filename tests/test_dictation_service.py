"""Tests for app.services.dictation_service — compute_word_diff."""

from app.services.dictation_service import compute_word_diff


class TestComputeWordDiff:
    def test_perfect_match(self):
        diffs, score = compute_word_diff("hello world", "hello world")
        assert score == 1.0
        assert all(d.status == "correct" for d in diffs)

    def test_case_insensitive(self):
        diffs, score = compute_word_diff("Hello World", "hello world")
        assert score == 1.0

    def test_completely_wrong(self):
        diffs, score = compute_word_diff("foo bar", "hello world")
        assert score == 0.0
        assert all(d.status == "wrong" for d in diffs)

    def test_missing_words(self):
        diffs, score = compute_word_diff("hello", "hello world today")
        # "hello" correct, "world" and "today" missing
        assert score == round(1 / 3, 4)
        missing = [d for d in diffs if d.status == "missing"]
        assert len(missing) == 2

    def test_extra_words(self):
        diffs, score = compute_word_diff("hello world extra", "hello world")
        # "hello" correct, "world" correct, "extra" is wrong (no expected)
        assert score == 1.0  # score = correct / len(correct_words) = 2/2
        wrong = [d for d in diffs if d.status == "wrong"]
        assert len(wrong) == 1
        assert wrong[0].expected == ""

    def test_partial_correct(self):
        diffs, score = compute_word_diff("the quick fox", "the slow fox")
        assert score == round(2 / 3, 4)
        assert diffs[0].status == "correct"  # "the"
        assert diffs[1].status == "wrong"    # "quick" vs "slow"
        assert diffs[1].expected == "slow"
        assert diffs[2].status == "correct"  # "fox"

    def test_both_empty(self):
        diffs, score = compute_word_diff("", "")
        assert score == 1.0
        assert diffs == []

    def test_user_empty_correct_has_words(self):
        diffs, score = compute_word_diff("", "hello world")
        assert score == 0.0
        assert all(d.status == "missing" for d in diffs)

    def test_single_word_correct(self):
        diffs, score = compute_word_diff("hello", "hello")
        assert score == 1.0
        assert len(diffs) == 1
