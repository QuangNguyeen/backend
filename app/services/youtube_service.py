import html
import logging
import re
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from pathlib import Path

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


@dataclass
class TranscriptSegment:
    """Represents a single transcript segment."""
    text: str
    start: float
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration


def clean_transcript_text(text: str) -> str:
    """Clean raw transcript text for dictation use."""
    text = text.replace("\n", " ")
    text = html.unescape(text)
    text = re.sub(r'\[.*?\]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
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
) -> list[TranscriptSegment]:
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
                logger.info("Found auto-generated transcript for %s in %s", video_id, transcript.language_code)
            except NoTranscriptFound:
                pass

        # 3. Fall back to any available transcript
        if transcript is None:
            all_transcripts = list(transcript_list)
            if all_transcripts:
                transcript = all_transcripts[0]
                logger.info(
                    "Using fallback transcript for %s: %s (%s)",
                    video_id, transcript.language, "generated" if transcript.is_generated else "manual",
                )

        if transcript is None:
            raise NotFoundError(f"No transcript found for video: {video_id}")

        transcript_data = transcript.fetch()
        return [
            TranscriptSegment(
                text=clean_transcript_text(item.text),
                start=item.start,
                duration=item.duration,
            )
            for item in transcript_data
        ]

    except (TranscriptsDisabled, VideoUnavailable):
        raise  # Let caller handle with appropriate fallback logic
    except (NotFoundError, BadRequestError):
        raise
    except Exception as e:
        logger.exception("Unexpected error fetching transcript for %s", video_id)
        raise


def get_transcript_ytdlp(video_id: str, languages: list[str] | None = None) -> list[TranscriptSegment]:
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
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    # yt-dlp puts subtitles in info["subtitles"] (manual) and info["automatic_captions"] (auto)
    subs = None
    for lang in languages:
        # Check manual subtitles first
        if info.get("subtitles") and lang in info["subtitles"]:
            subs = info["subtitles"][lang]
            break
        # Then auto-generated
        if info.get("automatic_captions") and lang in info["automatic_captions"]:
            subs = info["automatic_captions"][lang]
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
        text_parts = [seg.get("utf8", "") for seg in event.get("segs", []) if seg.get("utf8", "").strip()]
        if not text_parts:
            continue
        text = clean_transcript_text("".join(text_parts))
        if not text:
            continue
        start_ms = event.get("tStartMs", 0)
        dur_ms = event.get("dDurationMs", 0)
        segments.append(TranscriptSegment(
            text=text,
            start=start_ms / 1000.0,
            duration=dur_ms / 1000.0,
        ))

    if not segments:
        raise RuntimeError(f"yt-dlp returned empty subtitle data for {video_id}")

    logger.info("Transcript for %s fetched via yt-dlp fallback (%d segments)", video_id, len(segments))
    return segments


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


def merge_segments_by_duration(
    segments: list[TranscriptSegment],
    max_duration: float = 10.0,
) -> list[TranscriptSegment]:
    """Merge consecutive segments into chunks of approximately max_duration seconds."""
    if not segments:
        return []

    merged = []
    current_texts = []
    current_start = segments[0].start
    current_duration = 0.0

    for segment in segments:
        if current_duration + segment.duration > max_duration and current_texts:
            merged.append(TranscriptSegment(
                text=" ".join(current_texts),
                start=current_start,
                duration=current_duration,
            ))
            current_texts = [segment.text]
            current_start = segment.start
            current_duration = segment.duration
        else:
            current_texts.append(segment.text)
            current_duration = (segment.start + segment.duration) - current_start

    if current_texts:
        merged.append(TranscriptSegment(
            text=" ".join(current_texts),
            start=current_start,
            duration=current_duration,
        ))

    return merged


def merge_segments_smart(
    segments: list[TranscriptSegment],
    max_duration: float = 10.0,
    min_duration: float = 2.0,
) -> list[TranscriptSegment]:
    """Merge segments respecting both duration limits AND sentence boundaries."""
    if not segments:
        return []

    SENTENCE_ENDINGS = ".?!"

    merged: list[TranscriptSegment] = []
    current_texts: list[str] = []
    current_start: float = segments[0].start
    current_duration: float = 0.0

    def _flush():
        nonlocal current_texts, current_start, current_duration
        if current_texts:
            merged.append(TranscriptSegment(
                text=" ".join(current_texts),
                start=current_start,
                duration=current_duration,
            ))
            current_texts = []
            current_duration = 0.0

    for segment in segments:
        would_be_duration = (segment.start + segment.duration) - current_start

        if current_texts and would_be_duration > max_duration:
            _flush()
            current_start = segment.start

        current_texts.append(segment.text)
        current_duration = (segment.start + segment.duration) - current_start

        text_stripped = segment.text.rstrip()
        ends_with_sentence = text_stripped and text_stripped[-1] in SENTENCE_ENDINGS

        if ends_with_sentence and current_duration >= min_duration:
            _flush()

        if not current_texts:
            current_start = segment.start + segment.duration

    _flush()
    return merged


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