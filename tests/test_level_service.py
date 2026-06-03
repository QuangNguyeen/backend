"""Tests for CEFR level analysis service."""

from app.services.level_service import (
    analyze_level,
    analyze_level_detailed,
    calculate_audio_difficulty,
    compute_difficulty_score,
    extract_features,
    score_to_cefr,
    score_to_cefr_level,
)
from app.services.youtube_service import TranscriptSegment


def _seg(text: str, start: float, duration: float) -> TranscriptSegment:
    return TranscriptSegment(text=text, start=start, duration=duration)


class TestScoreToCefr:
    def test_a1(self):
        assert score_to_cefr(10) == "A1"

    def test_a2(self):
        assert score_to_cefr(25) == "A2"

    def test_b1(self):
        assert score_to_cefr(40) == "B1"

    def test_b2(self):
        assert score_to_cefr(55) == "B2"

    def test_c1(self):
        assert score_to_cefr(70) == "C1"

    def test_c2(self):
        assert score_to_cefr(90) == "C2"

    def test_boundary_a1_a2(self):
        assert score_to_cefr(20) == "A2"

    def test_boundary_c1_c2(self):
        assert score_to_cefr(80) == "C2"


class TestScoreToCefrLevel:
    def test_audio_boundaries_are_inclusive(self):
        assert score_to_cefr_level(20) == "A1"
        assert score_to_cefr_level(35) == "A2"
        assert score_to_cefr_level(50) == "B1"
        assert score_to_cefr_level(68) == "B2"
        assert score_to_cefr_level(84) == "C1"
        assert score_to_cefr_level(85) == "C2"


class TestExtractFeatures:
    def test_returns_expected_keys(self):
        features = extract_features("This is a simple test sentence.")
        assert "avg_zipf" in features
        assert "rare_ratio" in features
        assert "avg_sent_length" in features
        assert "avg_dep_depth" in features
        assert "n_sentences" in features
        assert "n_content_words" in features

    def test_simple_text_has_high_zipf(self):
        features = extract_features("I like big cats. The dog is happy.")
        assert features["avg_zipf"] > 4.5

    def test_complex_text_has_lower_zipf(self):
        features = extract_features(
            "The epistemological ramifications of quantum entanglement "
            "necessitate a paradigmatic reassessment of deterministic causality."
        )
        assert features["avg_zipf"] < 4.5

    def test_short_sentences_have_low_length(self):
        features = extract_features("I run. You jump. He sits.")
        assert features["avg_sent_length"] < 5

    def test_long_sentences_have_high_length(self):
        features = extract_features(
            "The international community has been working together to address "
            "the increasingly complex challenges posed by climate change and "
            "environmental degradation across all continents."
        )
        assert features["avg_sent_length"] > 15


class TestComputeDifficultyScore:
    def test_easy_features_give_low_score(self):
        features = {
            "avg_zipf": 6.0,
            "rare_ratio": 0.0,
            "avg_sent_length": 5.0,
            "avg_dep_depth": 2.0,
        }
        score = compute_difficulty_score(features)
        assert score < 20

    def test_hard_features_give_high_score(self):
        features = {
            "avg_zipf": 3.5,
            "rare_ratio": 0.4,
            "avg_sent_length": 25.0,
            "avg_dep_depth": 7.0,
        }
        score = compute_difficulty_score(features)
        assert score > 60


class TestAnalyzeLevel:
    def test_simple_text_returns_low_level(self):
        level = analyze_level("I am a boy. I like cats. This is my house. The cat is nice.")
        assert level in ("A1", "A2")

    def test_complex_text_returns_high_level(self):
        level = analyze_level(
            "The unprecedented economic downturn has compelled multinational corporations "
            "to reassess their strategic approaches to sustainable development, particularly "
            "in emerging markets where regulatory frameworks remain inadequate."
        )
        assert level in ("B2", "C1", "C2")

    def test_short_text_defaults_to_a2(self):
        level = analyze_level("Hello world.")
        assert level == "A2"

    def test_empty_text_defaults_to_a2(self):
        level = analyze_level("")
        assert level == "A2"

    def test_movie_dialogue_returns_a2_or_b1(self):
        text = (
            "Let us make a promise to each other, to always be together. "
            "A special heavy rain advisory has just been issued in the Tokyo area. "
            "Would you like the rain to stop? She really was the sunshine girl."
        )
        level = analyze_level(text)
        assert level in ("A2", "B1")


class TestAnalyzeLevelDetailed:
    def test_returns_level_and_score(self):
        result = analyze_level_detailed(
            "The cat sat on the mat. It was a nice day. Birds were singing in the trees."
        )
        assert "level" in result
        assert "score" in result
        assert "features" in result
        assert isinstance(result["score"], float)
        assert result["level"] in ("A1", "A2", "B1", "B2", "C1", "C2")

    def test_short_text_returns_note(self):
        result = analyze_level_detailed("Hi there.")
        assert result["level"] == "A2"
        assert result["score"] == 0.0


class TestAudioDifficulty:
    def test_short_slow_simple_transcript_is_low_level(self):
        segments = [
            _seg("I like cats.", 0.0, 3.0),
            _seg("This is my house.", 3.2, 3.0),
            _seg("The dog is happy.", 6.4, 3.0),
        ]
        result = calculate_audio_difficulty(
            None, segments, {"duration_seconds": 90, "language": "en"}
        )
        assert result["level"] in {"A1", "A2"}

    def test_medium_transcript_maps_to_intermediate_band(self):
        segments = [
            _seg("People often learn better when they practice every day.", 0.0, 4.5),
            _seg("Regular listening helps them understand natural English more quickly.", 4.8, 4.5),
            _seg("They can review mistakes and improve their pronunciation over time.", 9.6, 4.5),
        ]
        result = calculate_audio_difficulty(
            None, segments, {"duration_seconds": 12, "language": "en"}
        )
        assert result["level"] in {"A2", "B1", "B2"}

    def test_fast_rare_long_transcript_is_high_level(self):
        segments = [
            _seg(
                "The epistemological ramifications of ubiquitous automation require "
                "substantial interdisciplinary reconsideration because institutional "
                "incentives remain misaligned.",
                0.0,
                4.0,
            ),
            _seg(
                "Although policymakers acknowledge systemic vulnerabilities, "
                "implementation frequently deteriorates into fragmented procedural "
                "compliance.",
                4.2,
                4.0,
            ),
        ]
        result = calculate_audio_difficulty(
            None, segments, {"duration_seconds": 6, "language": "en"}
        )
        assert result["level"] in {"B2", "C1", "C2"}

    def test_low_confidence_markers_add_penalty(self):
        clean = [_seg("This is a normal sentence for practice.", 0.0, 5.0)]
        noisy = [_seg("This is [inaudible] a normal ?? sentence for practice.", 0.0, 0.0)]
        clean_result = calculate_audio_difficulty(
            None, clean, {"duration_seconds": 20, "language": "en"}
        )
        noisy_result = calculate_audio_difficulty(
            None, noisy, {"duration_seconds": 20, "language": "en"}
        )
        assert (
            noisy_result["factors"]["audioQualityPenalty"]
            > clean_result["factors"]["audioQualityPenalty"]
        )

    def test_returns_valid_score_and_required_factors(self):
        result = calculate_audio_difficulty(
            None, [_seg("This is a useful practice sentence.", 0.0, 5.0)], {"duration_seconds": 10}
        )
        assert 0 <= result["score"] <= 100
        assert result["level"] in {"A1", "A2", "B1", "B2", "C1", "C2"}
        assert "avgWordsPerSegment" in result["factors"]
        assert "wordsPerMinute" in result["factors"]
