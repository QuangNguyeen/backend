import html
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Literal

import requests
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)

from app.core.exceptions import BadRequestError, NotFoundError

logger = logging.getLogger(__name__)

# Create a single instance for reuse, with cookies if available
_cookie_path = Path(__file__).resolve().parent.parent.parent / "cookies.txt"


def _create_api() -> YouTubeTranscriptApi:
    if _cookie_path.exists():
        session = requests.Session()
        jar = MozillaCookieJar(str(_cookie_path))
        jar.load(ignore_discard=True, ignore_expires=True)
        session.cookies = jar
        return YouTubeTranscriptApi(http_client=session)
    return YouTubeTranscriptApi()


_ytt_api = _create_api()


def _writable_cookie_file() -> str | None:
    """Return a throwaway writable copy of the cookies file, or None if absent.

    yt-dlp rewrites the cookie jar on close; the mounted cookies file is
    read-only, so copy it to /tmp (also avoids OSError on the read-only mount).
    """
    if not _cookie_path.exists():
        return None
    dst = Path(tempfile.gettempdir()) / "yt-cookies-subs.txt"
    shutil.copy2(_cookie_path, dst)
    return str(dst)


@dataclass
class TranscriptSegment:
    """Represents a single transcript segment."""

    text: str
    start: float
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class TranscriptResult:
    segments: list[TranscriptSegment]
    is_generated: bool


PracticeMode = Literal["dictation", "cloze", "word_ordering"]


@dataclass(frozen=True)
class SegmentLimits:
    min_words: int
    preferred_min_words: int
    preferred_max_words: int
    hard_max_words: int
    global_hard_max_words: int
    max_pause_ms: int
    max_duration_seconds: float


SEGMENT_LIMITS: dict[PracticeMode, SegmentLimits] = {
    "dictation": SegmentLimits(
        min_words=4,
        preferred_min_words=8,
        preferred_max_words=18,
        hard_max_words=20,
        global_hard_max_words=35,
        max_pause_ms=1200,
        max_duration_seconds=16.0,
    ),
    "cloze": SegmentLimits(
        min_words=6,
        preferred_min_words=12,
        preferred_max_words=28,
        hard_max_words=30,
        global_hard_max_words=35,
        max_pause_ms=1500,
        max_duration_seconds=24.0,
    ),
    "word_ordering": SegmentLimits(
        min_words=3,
        preferred_min_words=5,
        preferred_max_words=12,
        hard_max_words=14,
        global_hard_max_words=35,
        max_pause_ms=1000,
        max_duration_seconds=12.0,
    ),
}


WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)?")
SPLIT_TOKEN_RE = re.compile(r"\S+")
STRONG_END_RE = re.compile(r"[.!?;:][\"')\]]*$")
SPLIT_MARKERS = {
    "although",
    "because",
    "while",
    "whereas",
    "since",
    "unless",
    "however",
    "therefore",
    "which",
    "who",
    "that",
    "when",
    "then",
    "but",
    "and",
    "or",
    "so",
}


def clean_transcript_text(text: str) -> str:
    """Clean raw transcript text for dictation use."""
    text = text.replace("\n", " ")
    text = html.unescape(text)
    text = re.sub(r"\[.*?\]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_video_id(youtube_url: str) -> str:
    """Extract video ID from various YouTube URL formats."""
    patterns = [
        r"(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/embed\/|youtube\.com\/v\/)([a-zA-Z0-9_-]{11})",
        r"^([a-zA-Z0-9_-]{11})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, youtube_url)
        if match:
            return match.group(1)
    raise BadRequestError(f"Invalid YouTube URL or video ID: {youtube_url}")


def get_transcript(
    video_id: str,
    languages: list[str] | None = None,
) -> TranscriptResult:
    """Fetch transcript for a YouTube video.

    Strategy:
    1. Try to find a manually created transcript in the preferred languages.
    2. Fall back to auto-generated transcript in the preferred languages.
    3. Fall back to any available transcript (manual or generated).

    Raises:
        NotFoundError: If no transcript is available at all
        BadRequestError: If transcripts are disabled or video is unavailable
    """
    if languages is None:
        languages = ["en", "en-US", "en-GB"]

    try:
        transcript_list = _ytt_api.list(video_id)

        transcript = None

        # 1. Try manually created transcript
        try:
            transcript = transcript_list.find_manually_created_transcript(languages)
            logger.info("Found manual transcript for %s in %s", video_id, transcript.language_code)
        except NoTranscriptFound:
            pass

        # 2. Fall back to auto-generated transcript
        if transcript is None:
            try:
                transcript = transcript_list.find_generated_transcript(languages)
                logger.info(
                    "Found auto-generated transcript for %s in %s",
                    video_id,
                    transcript.language_code,
                )
            except NoTranscriptFound:
                pass

        # 3. Fall back to any available transcript
        if transcript is None:
            all_transcripts = list(transcript_list)
            if all_transcripts:
                transcript = all_transcripts[0]
                logger.info(
                    "Using fallback transcript for %s: %s (%s)",
                    video_id,
                    transcript.language,
                    "generated" if transcript.is_generated else "manual",
                )

        if transcript is None:
            raise NotFoundError(f"No transcript found for video: {video_id}")

        transcript_data = transcript.fetch()
        segments = [
            TranscriptSegment(
                text=clean_transcript_text(item.text),
                start=item.start,
                duration=item.duration,
            )
            for item in transcript_data
        ]
        return TranscriptResult(segments=segments, is_generated=transcript.is_generated)

    except (TranscriptsDisabled, VideoUnavailable):
        raise  # Let caller handle with appropriate fallback logic
    except (NotFoundError, BadRequestError):
        raise
    except Exception:
        logger.exception("Unexpected error fetching transcript for %s", video_id)
        raise


def get_transcript_ytdlp(video_id: str, languages: list[str] | None = None) -> TranscriptResult:
    """Fallback transcript extraction via yt-dlp --write-auto-subs.

    Used when youtube-transcript-api is blocked by bot detection.
    Returns segments parsed from yt-dlp's JSON subtitle output.

    Raises Exception if yt-dlp is unavailable or extraction fails.
    """
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp is not installed")

    if languages is None:
        languages = ["en", "en-US", "en-GB"]

    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "writeautomaticsub": True,
        "writesubtitles": True,
        "subtitleslangs": languages,
        "subtitlesformat": "json3",
        "no_color": True,
        # Cookies + EJS solver are required from datacenter IPs; without them
        # YouTube returns "Sign in to confirm you're not a bot" and no subtitles,
        # forcing an avoidable AssemblyAI STT call for caption-bearing videos.
        "remote_components": ["ejs:github"],
    }
    cookie_file = _writable_cookie_file()
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    # yt-dlp puts subtitles in info["subtitles"] (manual) and info["automatic_captions"] (auto)
    subs = None
    is_generated = True
    for lang in languages:
        if info.get("subtitles") and lang in info["subtitles"]:
            subs = info["subtitles"][lang]
            is_generated = False
            break
        if info.get("automatic_captions") and lang in info["automatic_captions"]:
            subs = info["automatic_captions"][lang]
            is_generated = True
            break

    if not subs:
        raise RuntimeError(f"yt-dlp found no subtitles for {video_id} in {languages}")

    # Find json3 format entry
    json3_entry = next((s for s in subs if s.get("ext") == "json3"), None)
    if not json3_entry or "url" not in json3_entry:
        raise RuntimeError("yt-dlp subtitle format json3 not available")

    # Fetch the json3 subtitle data
    import requests as _requests

    resp = _requests.get(json3_entry["url"], timeout=15)
    resp.raise_for_status()
    data = resp.json()

    segments = []
    for event in data.get("events", []):
        text_parts = [
            seg.get("utf8", "") for seg in event.get("segs", []) if seg.get("utf8", "").strip()
        ]
        if not text_parts:
            continue
        text = clean_transcript_text("".join(text_parts))
        if not text:
            continue
        start_ms = event.get("tStartMs", 0)
        dur_ms = event.get("dDurationMs", 0)
        segments.append(
            TranscriptSegment(
                text=text,
                start=start_ms / 1000.0,
                duration=dur_ms / 1000.0,
            )
        )

    if not segments:
        raise RuntimeError(f"yt-dlp returned empty subtitle data for {video_id}")

    logger.info(
        "Transcript for %s fetched via yt-dlp fallback (%d segments, is_generated=%s)",
        video_id,
        len(segments),
        is_generated,
    )
    return TranscriptResult(segments=segments, is_generated=is_generated)


def list_available_transcripts(video_id: str) -> list[dict]:
    """List all available transcripts for a video."""
    try:
        transcript_list = _ytt_api.list(video_id)
        return [
            {
                "language": t.language,
                "language_code": t.language_code,
                "is_generated": t.is_generated,
                "is_translatable": t.is_translatable,
            }
            for t in transcript_list
        ]
    except TranscriptsDisabled:
        raise BadRequestError(f"Transcripts are disabled for video: {video_id}")
    except VideoUnavailable:
        raise NotFoundError(f"Video not available: {video_id}")


def get_full_text(segments: list[TranscriptSegment]) -> str:
    """Combine all transcript segments into a single text."""
    return " ".join(segment.text for segment in segments)


def count_words(text: str) -> int:
    """Count practice words without treating punctuation as words."""
    return len(WORD_RE.findall(text))


def tokenize_words(text: str) -> list[str]:
    return [match.group(0) for match in WORD_RE.finditer(text)]


def _tokenize_with_spans(text: str) -> list[re.Match[str]]:
    return list(SPLIT_TOKEN_RE.finditer(text))


def _make_segment(text: str, start: float, end: float) -> TranscriptSegment:
    start = max(0.0, start)
    end = max(start + 0.05, end)
    return TranscriptSegment(text=text.strip(), start=start, duration=end - start)


def _segment_from_token_range(
    segment: TranscriptSegment,
    tokens: list[re.Match[str]],
    start_idx: int,
    end_idx: int,
) -> TranscriptSegment:
    text = segment.text[tokens[start_idx].start() : tokens[end_idx - 1].end()].strip()
    total = max(len(tokens), 1)
    duration = max(segment.duration, 0.05)
    chunk_start = segment.start + duration * (start_idx / total)
    chunk_end = segment.start + duration * (end_idx / total)
    return _make_segment(text, chunk_start, chunk_end)


def _candidate_split_points(tokens: list[re.Match[str]]) -> dict[int, int]:
    """Return split indexes with lower score representing a stronger boundary."""
    candidates: dict[int, int] = {}
    for index in range(1, len(tokens)):
        previous = tokens[index - 1].group(0)
        current = tokens[index].group(0).lower().strip(".,!?;:\"'()[]")

        if re.search(r"[.!?;:]$", previous):
            candidates[index] = min(candidates.get(index, 99), 0)
        elif re.search(r"[,—–-]$", previous):
            candidates[index] = min(candidates.get(index, 99), 1)
        elif current in SPLIT_MARKERS:
            candidates[index] = min(candidates.get(index, 99), 2)
    return candidates


def _choose_split_point(
    *,
    start: int,
    total: int,
    config: SegmentLimits,
    candidates: dict[int, int],
) -> int:
    if total - start <= config.hard_max_words:
        return total

    min_end = min(total, start + 3)
    preferred_end = min(total, start + config.preferred_max_words)
    hard_end = min(total, start + config.hard_max_words)
    valid = [idx for idx in candidates if min_end <= idx <= hard_end]

    if valid:
        return min(valid, key=lambda idx: (candidates[idx], abs(idx - preferred_end), -idx))
    return hard_end


def split_long_segment(
    segment: TranscriptSegment,
    *,
    mode: PracticeMode = "dictation",
) -> list[TranscriptSegment]:
    """Split one overlong segment into practice-sized chunks.

    If word-level timestamps are unavailable, timestamps are estimated by token
    ratio inside the original segment duration.
    """
    config = SEGMENT_LIMITS[mode]
    tokens = _tokenize_with_spans(segment.text)
    if len(tokens) <= config.hard_max_words:
        return [segment]

    candidates = _candidate_split_points(tokens)
    chunks: list[TranscriptSegment] = []
    start = 0
    while start < len(tokens):
        end = _choose_split_point(
            start=start,
            total=len(tokens),
            config=config,
            candidates=candidates,
        )
        if end <= start:
            end = min(len(tokens), start + config.hard_max_words)
        chunks.append(_segment_from_token_range(segment, tokens, start, end))
        start = end
    return chunks


def can_merge_segments(
    a: TranscriptSegment,
    b: TranscriptSegment,
    config: SegmentLimits,
) -> bool:
    """Return whether two adjacent segments can merge without hurting practice UX."""
    merged_words = count_words(a.text) + count_words(b.text)
    pause_ms = max(0.0, (b.start - a.end) * 1000.0)
    merged_duration = b.end - a.start

    if merged_words > config.hard_max_words:
        return False
    if merged_words > config.global_hard_max_words:
        return False
    if pause_ms > config.max_pause_ms:
        return False
    if merged_duration > config.max_duration_seconds:
        return False
    if STRONG_END_RE.search(a.text) and count_words(a.text) >= config.min_words:
        return False
    return True


def _merge_segment_text(left: str, right: str) -> str:
    return f"{left.strip()} {right.strip()}".strip()


def merge_short_segments(
    segments: list[TranscriptSegment],
    *,
    mode: PracticeMode = "dictation",
) -> list[TranscriptSegment]:
    """Merge only short adjacent segments that remain within mode limits."""
    config = SEGMENT_LIMITS[mode]
    if len(segments) <= 1:
        return segments

    merged: list[TranscriptSegment] = []
    index = 0
    while index < len(segments):
        current = segments[index]
        if count_words(current.text) < config.preferred_min_words and index + 1 < len(segments):
            nxt = segments[index + 1]
            if can_merge_segments(current, nxt, config):
                merged.append(
                    _make_segment(
                        _merge_segment_text(current.text, nxt.text),
                        current.start,
                        nxt.end,
                    )
                )
                index += 2
                continue
        merged.append(current)
        index += 1

    if len(merged) >= 2 and count_words(merged[-1].text) < config.min_words:
        previous = merged[-2]
        last = merged[-1]
        if can_merge_segments(previous, last, config):
            merged[-2] = _make_segment(
                _merge_segment_text(previous.text, last.text),
                previous.start,
                last.end,
            )
            merged.pop()

    return merged


def validate_segments(
    segments: list[TranscriptSegment],
    *,
    mode: PracticeMode = "dictation",
) -> list[TranscriptSegment]:
    """Final pass: non-empty, mode-sized, ordered, non-overlapping timestamps."""
    validated: list[TranscriptSegment] = []
    for segment in segments:
        if not segment.text.strip():
            continue
        for piece in split_long_segment(segment, mode=mode):
            start = max(0.0, piece.start)
            end = max(start + 0.05, piece.end)
            if validated and start < validated[-1].end:
                start = validated[-1].end
                end = max(start + 0.05, piece.end)
            validated.append(_make_segment(piece.text, start, end))
    return validated


def process_transcript_segments(
    segments: list[TranscriptSegment],
    *,
    mode: PracticeMode = "dictation",
) -> list[TranscriptSegment]:
    """Split long segments first, merge short segments safely, then validate."""
    if not segments:
        return []

    ordered = sorted(segments, key=lambda seg: (seg.start, seg.end))
    split_segments: list[TranscriptSegment] = []
    for segment in ordered:
        split_segments.extend(split_long_segment(segment, mode=mode))
    return validate_segments(merge_short_segments(split_segments, mode=mode), mode=mode)


def apply_punctuation_to_segments(
    segments: list[TranscriptSegment],
    punctuated_text: str,
) -> list[TranscriptSegment]:
    """Map punctuated text back onto original segments, preserving timestamps.

    Each segment's word count is used to slice the corresponding words from
    the punctuated text so that timestamps remain aligned.
    """
    punct_words = punctuated_text.split()
    result: list[TranscriptSegment] = []
    offset = 0

    for seg in segments:
        seg_word_count = len(seg.text.split())
        end = offset + seg_word_count
        if end > len(punct_words):
            end = len(punct_words)
        new_text = " ".join(punct_words[offset:end])
        result.append(
            TranscriptSegment(
                text=new_text if new_text else seg.text,
                start=seg.start,
                duration=seg.duration,
            )
        )
        offset = end

    if offset < len(punct_words):
        if result:
            result[-1] = TranscriptSegment(
                text=result[-1].text + " " + " ".join(punct_words[offset:]),
                start=result[-1].start,
                duration=result[-1].duration,
            )

    return result


def merge_segments_by_duration(
    segments: list[TranscriptSegment],
    max_duration: float = 10.0,
    mode: PracticeMode = "dictation",
) -> list[TranscriptSegment]:
    """Merge consecutive segments into duration chunks without exceeding word limits."""
    if not segments:
        return []

    config = SEGMENT_LIMITS[mode]

    merged = []
    current_texts = []
    current_start = segments[0].start
    current_duration = 0.0

    for segment in segments:
        candidate_text = " ".join([*current_texts, segment.text])
        candidate_words = count_words(candidate_text)
        if current_texts and (
            current_duration + segment.duration > max_duration
            or candidate_words > config.hard_max_words
            or candidate_words > config.global_hard_max_words
        ):
            merged.append(
                TranscriptSegment(
                    text=" ".join(current_texts),
                    start=current_start,
                    duration=current_duration,
                )
            )
            current_texts = [segment.text]
            current_start = segment.start
            current_duration = segment.duration
        else:
            current_texts.append(segment.text)
            current_duration = (segment.start + segment.duration) - current_start

    if current_texts:
        merged.append(
            TranscriptSegment(
                text=" ".join(current_texts),
                start=current_start,
                duration=current_duration,
            )
        )

    return validate_segments(merged, mode=mode)


def merge_segments_smart(
    segments: list[TranscriptSegment],
    max_duration: float = 10.0,
    min_duration: float = 2.0,
    mode: PracticeMode = "dictation",
) -> list[TranscriptSegment]:
    """Merge segments while respecting duration, boundaries, and word limits."""
    if not segments:
        return []

    SENTENCE_ENDINGS = ".?!"
    config = SEGMENT_LIMITS[mode]

    merged: list[TranscriptSegment] = []
    current_texts: list[str] = []
    current_start: float = segments[0].start
    current_duration: float = 0.0

    def _flush():
        nonlocal current_texts, current_start, current_duration
        if current_texts:
            merged.append(
                TranscriptSegment(
                    text=" ".join(current_texts),
                    start=current_start,
                    duration=current_duration,
                )
            )
            current_texts = []
            current_duration = 0.0

    for segment in segments:
        if not current_texts:
            current_start = segment.start

        would_be_duration = (segment.start + segment.duration) - current_start
        candidate_word_count = count_words(" ".join([*current_texts, segment.text]))

        if current_texts and (
            would_be_duration > max_duration
            or candidate_word_count > config.hard_max_words
            or candidate_word_count > config.global_hard_max_words
        ):
            _flush()
            current_start = segment.start

        current_texts.append(segment.text)
        current_duration = (segment.start + segment.duration) - current_start

        text_stripped = segment.text.rstrip()
        ends_with_sentence = text_stripped and text_stripped[-1] in SENTENCE_ENDINGS

        if ends_with_sentence and current_duration >= min_duration:
            _flush()

    _flush()
    return validate_segments(merged, mode=mode)


# ─── Metadata extraction (isolated from transcript) ─────────────────────────


def _get_metadata_oembed(video_id: str) -> dict | None:
    """Fetch metadata via YouTube oEmbed (lightweight, no API key needed)."""
    try:
        url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return {
            "title": data.get("title", ""),
            "channel": data.get("author_name", ""),
            "duration": 0,  # oEmbed doesn't provide duration
            "thumbnail_url": data.get("thumbnail_url", ""),
        }
    except Exception:
        logger.debug("oEmbed metadata fetch failed for %s", video_id)
        return None


def _get_metadata_ytdlp(video_id: str) -> dict | None:
    """Fetch metadata via yt-dlp (heavier, but includes duration)."""
    try:
        import yt_dlp
    except ImportError:
        logger.debug("yt-dlp not installed, skipping")
        return None

    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "no_color": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "title": info.get("title", ""),
                "channel": info.get("channel", info.get("uploader", "")),
                "duration": info.get("duration", 0),
                "thumbnail_url": info.get("thumbnail", ""),
            }
    except Exception:
        logger.debug("yt-dlp metadata fetch failed for %s", video_id)
        return None


def get_video_metadata(video_id: str) -> dict:
    """Fetch video metadata using multiple strategies with fallbacks.

    Strategy order (least-blocked first):
    1. oEmbed API (official, lightweight, rarely rate-limited)
    2. yt-dlp (heavy scraper — best quality but easily blocked)
    3. Minimal defaults (YouTube thumbnail URL, empty title)

    Never raises — always returns a dict with title, channel, duration, thumbnail_url.
    """
    defaults = {
        "title": "",
        "channel": "",
        "duration": 0,
        "thumbnail_url": f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
    }

    # 1. Try oEmbed first (official API, rarely blocked)
    meta = _get_metadata_oembed(video_id)
    if meta and meta.get("title"):
        if not meta.get("thumbnail_url"):
            meta["thumbnail_url"] = defaults["thumbnail_url"]
        logger.info("Metadata for %s fetched via oEmbed", video_id)
        # oEmbed lacks duration — try yt-dlp just for duration if available
        ytdlp_meta = _get_metadata_ytdlp(video_id)
        if ytdlp_meta and ytdlp_meta.get("duration"):
            meta["duration"] = ytdlp_meta["duration"]
            logger.info("Duration for %s supplemented via yt-dlp: %ds", video_id, meta["duration"])
        return meta

    # 2. Try yt-dlp as full fallback
    meta = _get_metadata_ytdlp(video_id)
    if meta and meta.get("title"):
        if not meta.get("thumbnail_url"):
            meta["thumbnail_url"] = defaults["thumbnail_url"]
        logger.info("Metadata for %s fetched via yt-dlp", video_id)
        return meta

    # 3. Return defaults
    logger.warning("Could not fetch metadata for %s, using defaults", video_id)
    return defaults
