"""Tests for app.services.youtube_service — pure logic functions only (no YouTube API calls)."""

import pytest

from app.services.youtube_service import (
    TranscriptSegment,
    clean_transcript_text,
    extract_video_id,
    get_full_text,
    merge_segments_by_duration,
    merge_segments_smart,
)
from app.core.exceptions import BadRequestError


# ─── extract_video_id ─────────────────────────────────────────────

class TestExtractVideoId:
    def test_standard_url(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_short_url(self):
        url = "https://youtu.be/dQw4w9WgXcQ"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_embed_url(self):
        url = "https://www.youtube.com/embed/dQw4w9WgXcQ"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_v_url(self):
        url = "https://www.youtube.com/v/dQw4w9WgXcQ"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_direct_video_id(self):
        assert extract_video_id("dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_url_with_extra_params(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=120s&list=PLxyz"
        assert extract_video_id(url) == "dQw4w9WgXcQ"

    def test_id_with_hyphen_underscore(self):
        assert extract_video_id("ps8qwWG8Uio") == "ps8qwWG8Uio"

    def test_invalid_url_raises(self):
        with pytest.raises(BadRequestError):
            extract_video_id("https://example.com/not-a-video")

    def test_empty_string_raises(self):
        with pytest.raises(BadRequestError):
            extract_video_id("")


# ─── clean_transcript_text ────────────────────────────────────────

class TestCleanTranscriptText:
    def test_removes_newlines(self):
        assert clean_transcript_text("hello\nworld") == "hello world"

    def test_decodes_html_entities(self):
        assert clean_transcript_text("rock &amp; roll") == "rock & roll"
        assert clean_transcript_text("it&#39;s fine") == "it's fine"

    def test_removes_music_markers(self):
        assert clean_transcript_text("[Music] hello [Applause]") == "hello"

    def test_collapses_spaces(self):
        assert clean_transcript_text("too   many    spaces") == "too many spaces"

    def test_strips_whitespace(self):
        assert clean_transcript_text("  padded  ") == "padded"

    def test_combined_cleaning(self):
        raw = "[Music]\nLet&#39;s make a promise &amp;\nalways   be together [Applause]"
        expected = "Let's make a promise & always be together"
        assert clean_transcript_text(raw) == expected

    def test_empty_string(self):
        assert clean_transcript_text("") == ""

    def test_no_artifacts(self):
        clean = "This is a perfectly clean sentence."
        assert clean_transcript_text(clean) == clean


# ─── merge_segments_by_duration ───────────────────────────────────

def _seg(text: str, start: float, duration: float) -> TranscriptSegment:
    """Helper to create a TranscriptSegment quickly."""
    return TranscriptSegment(text=text, start=start, duration=duration)


class TestMergeSegmentsByDuration:
    def test_empty_list(self):
        assert merge_segments_by_duration([]) == []

    def test_single_segment(self):
        segs = [_seg("hello", 0.0, 2.0)]
        merged = merge_segments_by_duration(segs, max_duration=10.0)
        assert len(merged) == 1
        assert merged[0].text == "hello"

    def test_merges_short_segments(self):
        segs = [
            _seg("one", 0.0, 2.0),
            _seg("two", 2.0, 2.0),
            _seg("three", 4.0, 2.0),
        ]
        merged = merge_segments_by_duration(segs, max_duration=10.0)
        assert len(merged) == 1
        assert merged[0].text == "one two three"
        assert merged[0].start == 0.0

    def test_splits_on_max_duration(self):
        segs = [
            _seg("one", 0.0, 5.0),
            _seg("two", 5.0, 5.0),
            _seg("three", 10.0, 5.0),
        ]
        merged = merge_segments_by_duration(segs, max_duration=8.0)
        assert len(merged) >= 2
        # First merged segment should have started at 0.0
        assert merged[0].start == 0.0

    def test_end_property(self):
        seg = _seg("hello", 1.0, 3.0)
        assert seg.end == 4.0


# ─── get_full_text ────────────────────────────────────────────────

class TestGetFullText:
    def test_combines_segments(self):
        segs = [_seg("hello", 0.0, 1.0), _seg("world", 1.0, 1.0)]
        assert get_full_text(segs) == "hello world"

    def test_empty_segments(self):
        assert get_full_text([]) == ""


# ─── merge_segments_smart ─────────────────────────────────────────

class TestMergeSegmentsSmart:
    def test_empty_list(self):
        assert merge_segments_smart([]) == []

    def test_splits_on_sentence_boundary(self):
        """Should flush when segment ends with period, even if under max_duration."""
        segs = [
            _seg("Hello world.", 0.0, 3.0),
            _seg("How are you?", 3.0, 3.0),
            _seg("I am fine.", 6.0, 3.0),
        ]
        merged = merge_segments_smart(segs, max_duration=10.0, min_duration=2.0)
        # Each ends with sentence punctuation and >= min_duration, so 3 chunks
        assert len(merged) == 3
        assert merged[0].text == "Hello world."
        assert merged[1].text == "How are you?"
        assert merged[2].text == "I am fine."

    def test_merges_until_sentence_end(self):
        """Segments without punctuation should accumulate until one with punctuation."""
        segs = [
            _seg("The quick brown", 0.0, 2.0),
            _seg("fox jumps over", 2.0, 2.0),
            _seg("the lazy dog.", 4.0, 2.0),
            _seg("End.", 6.0, 2.0),
        ]
        merged = merge_segments_smart(segs, max_duration=15.0, min_duration=2.0)
        assert len(merged) == 2
        assert merged[0].text == "The quick brown fox jumps over the lazy dog."
        assert merged[1].text == "End."

    def test_force_flush_on_max_duration(self):
        """If max_duration is exceeded without punctuation, force flush."""
        segs = [
            _seg("word one", 0.0, 4.0),
            _seg("word two", 4.0, 4.0),
            _seg("word three", 8.0, 4.0),  # cumulative = 12s > 10s
        ]
        merged = merge_segments_smart(segs, max_duration=10.0, min_duration=2.0)
        # Should have flushed at some point due to max_duration
        assert len(merged) >= 2

    def test_respects_min_duration(self):
        """Very short sentence should NOT trigger flush if under min_duration."""
        segs = [
            _seg("Hi.", 0.0, 0.5),   # ends with "." but < min_duration
            _seg("How are you today?", 0.5, 3.0),
        ]
        merged = merge_segments_smart(segs, max_duration=10.0, min_duration=2.0)
        # "Hi." is too short (0.5s < 2s min), so it should merge with next
        assert len(merged) == 1
        assert "Hi." in merged[0].text
        assert "How are you today?" in merged[0].text

    def test_no_punctuation_uses_max_duration(self):
        """Without any sentence-ending punctuation, behaves like duration-based merge."""
        segs = [
            _seg("one", 0.0, 3.0),
            _seg("two", 3.0, 3.0),
            _seg("three", 6.0, 3.0),
            _seg("four", 9.0, 3.0),
        ]
        merged = merge_segments_smart(segs, max_duration=8.0, min_duration=2.0)
        assert len(merged) >= 2

    def test_question_and_exclamation(self):
        """Should recognize ? and ! as sentence endings."""
        segs = [
            _seg("What is this?", 0.0, 3.0),
            _seg("Stop!", 3.0, 2.0),
        ]
        merged = merge_segments_smart(segs, max_duration=10.0, min_duration=2.0)
        assert len(merged) == 2
        assert merged[0].text == "What is this?"
        assert merged[1].text == "Stop!"


# ─── get_video_metadata (import check only) ──────────────────────

class TestGetVideoMetadata:
    def test_function_is_importable(self):
        """Verify the function exists and can be imported."""
        from app.services.youtube_service import get_video_metadata
        assert callable(get_video_metadata)
