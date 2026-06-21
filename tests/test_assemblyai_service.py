from pathlib import Path
from types import SimpleNamespace

import assemblyai as aai
import pytest

from app.services.assemblyai_service import transcribe_with_assemblyai
from app.services.stt_audio_service import AudioFile


class FakeTranscript:
    status = aai.TranscriptStatus.completed
    error = None

    def __init__(self, sentences=None, words=None, text=None, fail_sentences=False):
        self._sentences = sentences
        self.words = words
        self.text = text
        self.fail_sentences = fail_sentences

    def get_sentences(self):
        if self.fail_sentences:
            raise AssertionError("get_sentences should not be called when top-level words exist")
        if self._sentences is not None:
            return self._sentences
        return [SimpleNamespace(text="Hello world.", start=0, end=1200)]


class FakeTranscriber:
    def __init__(self):
        self.calls: list[str] = []

    def transcribe(self, data, config=None):
        self.calls.append(data)
        return FakeTranscript()


def _patch_settings(monkeypatch):
    monkeypatch.setattr(
        "app.services.assemblyai_service.get_settings",
        lambda: SimpleNamespace(ASSEMBLYAI_API_KEY="test-key"),
    )


def _word(text, start, end):
    return SimpleNamespace(text=text, start=start, end=end)


def test_assemblyai_uses_direct_audio_url_first(monkeypatch):
    _patch_settings(monkeypatch)
    fake = FakeTranscriber()
    monkeypatch.setattr("app.services.assemblyai_service.aai.Transcriber", lambda: fake)
    monkeypatch.setattr(
        "app.services.assemblyai_service.get_direct_audio_url",
        lambda video_id: "https://media.example/audio.m4a",
    )
    monkeypatch.setattr(
        "app.services.assemblyai_service.download_native_audio",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("native download should not run")
        ),
    )

    segments = transcribe_with_assemblyai("abcdefghijk", video_duration=120)

    assert fake.calls == ["https://media.example/audio.m4a"]
    assert segments[0].text == "Hello world."


def test_assemblyai_falls_back_to_native_audio_upload(monkeypatch):
    _patch_settings(monkeypatch)
    fake = FakeTranscriber()
    monkeypatch.setattr("app.services.assemblyai_service.aai.Transcriber", lambda: fake)
    monkeypatch.setattr(
        "app.services.assemblyai_service.get_direct_audio_url",
        lambda video_id: (_ for _ in ()).throw(RuntimeError("expired")),
    )
    monkeypatch.setattr(
        "app.services.assemblyai_service.download_native_audio",
        lambda video_id, out_dir: AudioFile(path=Path(out_dir) / "audio.m4a", duration_seconds=120),
    )
    monkeypatch.setattr(
        "app.services.assemblyai_service.extract_wav_audio",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("WAV should not run")),
    )

    transcribe_with_assemblyai("abcdefghijk", video_duration=120)

    assert len(fake.calls) == 1
    assert fake.calls[0].endswith("audio.m4a")


def test_assemblyai_uses_word_boundaries_for_segment_timestamps(monkeypatch):
    _patch_settings(monkeypatch)
    sentences = [
        SimpleNamespace(
            text="Hello world from AssemblyAI.",
            start=0,
            end=3200,
            words=[
                _word("Hello", 0, 400),
                _word("world", 500, 900),
                _word("from", 1200, 1700),
                _word("AssemblyAI", 1900, 2500),
            ],
        ),
        SimpleNamespace(
            text="Next sentence starts now.",
            start=2600,
            end=5200,
            words=[
                _word("Next", 2600, 3100),
                _word("sentence", 3200, 3800),
                _word("starts", 3900, 4500),
                _word("now", 4700, 5100),
            ],
        ),
    ]
    fake = FakeTranscriber()
    monkeypatch.setattr("app.services.assemblyai_service.aai.Transcriber", lambda: fake)
    monkeypatch.setattr(
        "app.services.assemblyai_service.get_direct_audio_url",
        lambda video_id: "https://media.example/audio.m4a",
    )
    monkeypatch.setattr(
        fake,
        "transcribe",
        lambda data, config=None: FakeTranscript(sentences),
    )

    segments = transcribe_with_assemblyai("abcdefghijk", video_duration=120)

    assert segments[0].text == "Hello world from AssemblyAI."
    assert segments[0].end == pytest.approx(2.5)
    assert segments[1].start == pytest.approx(2.6)


def test_assemblyai_prefers_top_level_words_over_sentence_endpoint(monkeypatch):
    _patch_settings(monkeypatch)
    top_level_words = [
        _word("So", 32290, 32600),
        _word("first", 32700, 33100),
        _word("let's", 33200, 33600),
        _word("talk", 33700, 34000),
        _word("about", 34100, 34400),
        _word("currencies", 34500, 35850),
        _word("A", 36590, 36700),
        _word("currency", 36800, 37500),
        _word("is", 37600, 37800),
        _word("money", 37900, 41070),
    ]
    bad_sentences = [
        SimpleNamespace(
            text="money",
            start=37900,
            end=41070,
            words=[_word("money", 37900, 41070)],
        )
    ]
    fake = FakeTranscriber()
    monkeypatch.setattr("app.services.assemblyai_service.aai.Transcriber", lambda: fake)
    monkeypatch.setattr(
        "app.services.assemblyai_service.get_direct_audio_url",
        lambda video_id: "https://media.example/audio.m4a",
    )
    monkeypatch.setattr(
        fake,
        "transcribe",
        lambda data, config=None: FakeTranscript(
            sentences=bad_sentences,
            words=top_level_words,
            text="So first let's talk about currencies. A currency is money.",
            fail_sentences=True,
        ),
    )

    segments = transcribe_with_assemblyai("abcdefghijk", video_duration=120)

    assert segments[0].start == pytest.approx(32.29)
    assert "So first" in segments[0].text
    assert "money" in segments[-1].text


def test_assemblyai_does_not_merge_short_word_aligned_sentences(monkeypatch):
    _patch_settings(monkeypatch)
    sentences = [
        SimpleNamespace(
            text="Go.",
            start=0,
            end=500,
            words=[_word("Go", 0, 500)],
        ),
        SimpleNamespace(
            text="Now.",
            start=800,
            end=1300,
            words=[_word("Now", 800, 1300)],
        ),
    ]
    fake = FakeTranscriber()
    monkeypatch.setattr("app.services.assemblyai_service.aai.Transcriber", lambda: fake)
    monkeypatch.setattr(
        "app.services.assemblyai_service.get_direct_audio_url",
        lambda video_id: "https://media.example/audio.m4a",
    )
    monkeypatch.setattr(
        fake,
        "transcribe",
        lambda data, config=None: FakeTranscript(sentences),
    )

    segments = transcribe_with_assemblyai("abcdefghijk", video_duration=120)

    assert [segment.text for segment in segments] == ["Go.", "Now."]
    assert segments[0].end == pytest.approx(0.5)
    assert segments[1].start == pytest.approx(0.8)


def test_assemblyai_adds_guard_gap_when_word_segments_touch(monkeypatch):
    _patch_settings(monkeypatch)
    sentences = [
        SimpleNamespace(
            text="Hello.",
            start=0,
            end=1030,
            words=[_word("Hello", 0, 1030)],
        ),
        SimpleNamespace(
            text="Next.",
            start=1030,
            end=1500,
            words=[_word("Next", 1030, 1500)],
        ),
    ]
    fake = FakeTranscriber()
    monkeypatch.setattr("app.services.assemblyai_service.aai.Transcriber", lambda: fake)
    monkeypatch.setattr(
        "app.services.assemblyai_service.get_direct_audio_url",
        lambda video_id: "https://media.example/audio.m4a",
    )
    monkeypatch.setattr(
        fake,
        "transcribe",
        lambda data, config=None: FakeTranscript(sentences),
    )

    segments = transcribe_with_assemblyai("abcdefghijk", video_duration=120)

    assert segments[0].end == pytest.approx(0.85)
    assert segments[1].start == pytest.approx(1.03)
