"""Tests for CEFR level analysis service."""

import pytest
from app.services.level_service import (
    analyze_level,
    analyze_level_detailed,
    compute_difficulty_score,
    extract_features,
    score_to_cefr,
)


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