import html
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
    """Clean raw transcript text for dictation use.

    Handles common YouTube subtitle artifacts:
    - Line breaks within segments
    - HTML entities (&amp; etc.)
    - Non-speech markers like [Music], [Applause]
    - Multiple consecutive spaces
    """
    # Replace line breaks with spaces
    text = text.replace("\n", " ")
    # Decode HTML entities
    text = html.unescape(text)
    # Remove non-speech markers like [Music], [Applause], etc.
    text = re.sub(r'\[.*?\]', '', text)
    # Collapse multiple spaces into one
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def extract_video_id(youtube_url: str) -> str:
    """Extract video ID from various YouTube URL formats.

    Supported formats:
    - https://www.youtube.com/watch?v=VIDEO_ID
    - https://youtu.be/VIDEO_ID
    - https://www.youtube.com/embed/VIDEO_ID
    - https://www.youtube.com/v/VIDEO_ID
    """
    patterns = [
        r"(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/embed\/|youtube\.com\/v\/)([a-zA-Z0-9_-]{11})",
        r"^([a-zA-Z0-9_-]{11})$",  # Direct video ID
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

    Args:
        video_id: YouTube video ID (11 characters)
        languages: List of language codes to try, in order of preference.
                  Defaults to ["en", "en-US", "en-GB"]

    Returns:
        List of TranscriptSegment objects

    Raises:
        NotFoundError: If no transcript is available
        BadRequestError: If transcripts are disabled or video is unavailable
    """
    if languages is None:
        languages = ["en", "en-US", "en-GB"]

    try:
        transcript_data = _ytt_api.fetch(video_id, languages=languages)

        return [
            TranscriptSegment(
                text=clean_transcript_text(item.text),
                start=item.start,
                duration=item.duration,
            )
            for item in transcript_data
        ]

    except TranscriptsDisabled:
        raise BadRequestError(
            f"Transcripts are disabled for video: {video_id}"
        )
    except NoTranscriptFound:
        raise NotFoundError(
            f"No transcript found for video: {video_id} in languages: {languages}"
        )
    except VideoUnavailable:
        raise NotFoundError(f"Video not available: {video_id}")


def list_available_transcripts(video_id: str) -> list[dict]:
    """List all available transcripts for a video.

    Returns:
        List of dicts with language info:
        [{"language": "English", "language_code": "en", "is_generated": False}, ...]
    """
    try:
        transcript_list = _ytt_api.list(video_id)

        result = []
        for transcript in transcript_list:
            result.append({
                "language": transcript.language,
                "language_code": transcript.language_code,
                "is_generated": transcript.is_generated,
                "is_translatable": transcript.is_translatable,
            })

        return result

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
    """Merge consecutive segments into chunks of approximately max_duration seconds.

    This is useful for creating sentence-like chunks for dictation practice.
    """
    if not segments:
        return []

    merged = []
    current_texts = []
    current_start = segments[0].start
    current_duration = 0.0

    for segment in segments:
        if current_duration + segment.duration > max_duration and current_texts:
            # Save current chunk and start new one
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

    # Don't forget the last chunk
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
    """Merge segments respecting both duration limits AND sentence boundaries.

    Strategy:
    1. Accumulate segments until we approach max_duration.
    2. When a segment ends with sentence-ending punctuation (. ? !),
       flush the current chunk — even if shorter than max_duration.
    3. If we exceed max_duration without hitting a sentence boundary,
       force-flush at that point anyway.
    4. Very short chunks (< min_duration) are merged into the next chunk.

    This produces more natural dictation sentences compared to
    the pure duration-based merge.
    """
    if not segments:
        return []

    SENTENCE_ENDINGS = ".?!"

    merged: list[TranscriptSegment] = []
    current_texts: list[str] = []
    current_start: float = segments[0].start
    current_duration: float = 0.0

    def _flush():
        """Save the current accumulated chunk."""
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
        # Check if adding this segment would exceed max_duration
        would_be_duration = (segment.start + segment.duration) - current_start

        if current_texts and would_be_duration > max_duration:
            # Force flush before adding — exceeded max without sentence boundary
            _flush()
            current_start = segment.start

        current_texts.append(segment.text)
        current_duration = (segment.start + segment.duration) - current_start

        text_stripped = segment.text.rstrip()
        ends_with_sentence = text_stripped and text_stripped[-1] in SENTENCE_ENDINGS

        # Flush if we hit a sentence boundary and have enough content
        if ends_with_sentence and current_duration >= min_duration:
            _flush()

        # Set start for next chunk if we just flushed
        if not current_texts:
            current_start = segment.start + segment.duration

    # Don't forget the last chunk
    _flush()

    return merged


def get_video_metadata(video_id: str) -> dict:
    """Fetch video metadata from YouTube using yt-dlp.

    Returns:
        Dict with keys: title, channel, duration (seconds), thumbnail_url

    Raises:
        NotFoundError: If the video is unavailable
        BadRequestError: If yt-dlp fails for another reason
    """
    try:
        import yt_dlp
    except ImportError:
        raise BadRequestError(
            "yt-dlp is not installed. Run: pip install yt-dlp"
        )

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
                "thumbnail_url": info.get("thumbnail", f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"),
            }
    except yt_dlp.utils.DownloadError:
        raise NotFoundError(f"Video not available or cannot fetch metadata: {video_id}")
    except Exception as e:
        raise BadRequestError(f"Failed to fetch video metadata: {e}")
