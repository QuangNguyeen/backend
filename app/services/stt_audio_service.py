"""Shared helpers for YouTube audio used by STT providers."""

import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from app.services.youtube_service import TranscriptSegment

logger = logging.getLogger(__name__)

STT_MAX_DURATION_SECONDS = 600

_YOUTUBE_URL = "https://www.youtube.com/watch?v={video_id}"
_NATIVE_AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio[acodec^=opus]/bestaudio/best"

# YouTube blocks datacenter IPs ("Sign in to confirm you're not a bot") unless
# authenticated cookies are supplied. Mounted at /app/cookies.txt in the worker.
_COOKIE_PATH = Path(__file__).resolve().parent.parent.parent / "cookies.txt"


def _writable_cookie_file() -> str | None:
    """Return a writable copy of the cookies file, or None if none exists.

    The mounted cookies.txt is read-only, but yt-dlp rewrites the cookie jar on
    close to persist refreshed session cookies — writing to the read-only mount
    raises OSError(Errno 30). Copy it to a writable temp path so yt-dlp can read
    and rewrite freely; the mounted file stays the source of truth.
    """
    if not _COOKIE_PATH.exists():
        return None
    # If the mounted cookie file is writable, use it directly so yt-dlp persists
    # YouTube's rotated session cookies back to it — keeping the session alive
    # across runs. (Mount it read-write in compose for this to take effect.)
    if os.access(_COOKIE_PATH, os.W_OK):
        return str(_COOKIE_PATH)
    # Read-only mount: copy to a writable temp path so yt-dlp's cookie-jar rewrite
    # on close doesn't raise OSError(Errno 30). Rotations won't persist here.
    dst = Path(tempfile.gettempdir()) / "yt-cookies.txt"
    shutil.copy2(_COOKIE_PATH, dst)
    return str(dst)


class VideoUnavailableError(Exception):
    """Raised when YouTube refuses to serve a video.

    Covers permanent conditions (removed, private, terminated account,
    age/login-gated, or region-blocked) where retrying is pointless.
    """


# Lowercased substrings yt-dlp emits when a video cannot be served.
_UNAVAILABLE_MARKERS = (
    "video is not available",
    "this video is unavailable",
    "video unavailable",
    "private video",
    "this video is private",
    "removed by the user",
    "account associated with this video has been terminated",
    "video has been removed",
    "video is no longer available",
    "not available in your country",
    "blocked it in your country",
    "sign in to confirm your age",
    "members-only",
)

_UNAVAILABLE_MESSAGE = (
    "This YouTube video is unavailable (it may be removed, private, "
    "age-restricted, or blocked in this region)."
)


def _is_unavailable_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _UNAVAILABLE_MARKERS)


@dataclass(frozen=True)
class AudioFile:
    path: Path
    duration_seconds: int


def _youtube_url(video_id: str) -> str:
    return _YOUTUBE_URL.format(video_id=video_id)


def _yt_dlp_options(**overrides):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "format": _NATIVE_AUDIO_FORMAT,
        "noplaylist": True,
        # Let yt-dlp fetch the EJS solver script so Deno can solve YouTube's "n"
        # challenge — without it only storyboard images are exposed and audio
        # extraction fails with "Requested format is not available".
        "remote_components": ["ejs:github"],
    }
    cookie_file = _writable_cookie_file()
    if cookie_file:
        opts["cookiefile"] = cookie_file
    opts.update(overrides)
    return opts


def _selected_url(info: dict) -> str | None:
    for entry in info.get("requested_downloads") or []:
        url = entry.get("url")
        if url:
            return url
    url = info.get("url")
    return url if isinstance(url, str) and url.startswith(("http://", "https://")) else None


def get_direct_audio_url(video_id: str) -> str:
    """Return a fresh direct YouTube audio URL suitable for immediate STT submission."""
    import yt_dlp

    try:
        with yt_dlp.YoutubeDL(_yt_dlp_options(skip_download=True)) as ydl:
            info = ydl.extract_info(_youtube_url(video_id), download=False)
    except yt_dlp.utils.DownloadError as exc:
        if _is_unavailable_error(str(exc)):
            raise VideoUnavailableError(_UNAVAILABLE_MESSAGE) from exc
        raise

    direct_url = _selected_url(info)
    if not direct_url:
        raise RuntimeError(f"yt-dlp did not return a direct audio URL for {video_id}")
    return direct_url


def _probe_duration(path: Path) -> int:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return int(float(result.stdout.strip()))
    except Exception:
        return 0


def download_native_audio(video_id: str, out_dir: Path) -> AudioFile:
    """Download YouTube audio in its native m4a/webm/opus format without transcoding."""
    import yt_dlp

    out_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(out_dir / f"{video_id}.%(ext)s")
    t0 = time.time()
    try:
        with yt_dlp.YoutubeDL(_yt_dlp_options(outtmpl=outtmpl)) as ydl:
            info = ydl.extract_info(_youtube_url(video_id), download=True)
            prepared = Path(ydl.prepare_filename(info))
    except yt_dlp.utils.DownloadError as exc:
        if _is_unavailable_error(str(exc)):
            raise VideoUnavailableError(_UNAVAILABLE_MESSAGE) from exc
        raise

    if prepared.exists():
        audio_path = prepared
    else:
        candidates = sorted(out_dir.glob(f"{video_id}.*"))
        if not candidates:
            raise RuntimeError(f"yt-dlp produced no native audio file for {video_id}")
        audio_path = candidates[0]

    duration = int(info.get("duration") or 0) or _probe_duration(audio_path)
    logger.info(
        "[STT] Native audio downloaded: %s | %.1f MB | %ds in %.1fs",
        audio_path.name,
        audio_path.stat().st_size / (1024 * 1024),
        duration,
        time.time() - t0,
    )
    return AudioFile(path=audio_path, duration_seconds=duration)


def extract_wav_audio(video_id: str, out_dir: Path) -> AudioFile:
    """Last-resort WAV extraction for providers that reject native YouTube audio."""
    out_dir.mkdir(parents=True, exist_ok=True)
    wav_path = out_dir / f"{video_id}.wav"
    cmd = [
        "yt-dlp",
        "--quiet",
        "--no-warnings",
        # Fetch the EJS solver so Deno can solve YouTube's "n" challenge (see
        # _yt_dlp_options); otherwise no downloadable audio formats are exposed.
        "--remote-components",
        "ejs:github",
        "-x",
        "--audio-format",
        "wav",
        "--postprocessor-args",
        "ffmpeg:-ac 1 -ar 16000",
        "-o",
        str(wav_path),
    ]
    cookie_file = _writable_cookie_file()
    if cookie_file:
        cmd += ["--cookies", cookie_file]
    cmd.append(_youtube_url(video_id))

    logger.info("[STT] Extracting WAV fallback for %s -> %s", video_id, wav_path)
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        stderr = result.stderr or ""
        if _is_unavailable_error(stderr):
            raise VideoUnavailableError(_UNAVAILABLE_MESSAGE)
        raise subprocess.CalledProcessError(
            result.returncode, cmd, output=result.stdout, stderr=stderr
        )

    if not wav_path.exists():
        candidates = sorted(out_dir.glob(f"{video_id}.*"))
        if not candidates:
            raise RuntimeError(f"yt-dlp produced no WAV audio file for {video_id}")
        wav_path = candidates[0]

    duration = _probe_duration(wav_path)
    logger.info(
        "[STT] WAV fallback extracted: %s | %.1f MB | %ds in %.1fs",
        wav_path.name,
        wav_path.stat().st_size / (1024 * 1024),
        duration,
        time.time() - t0,
    )
    return AudioFile(path=wav_path, duration_seconds=duration)


def enforce_non_overlapping(
    segments: list[TranscriptSegment],
    *,
    min_gap_seconds: float = 0.05,
) -> list[TranscriptSegment]:
    """Clamp each segment's end so playback cannot bleed into the next segment."""
    if len(segments) <= 1:
        return segments

    min_duration = 0.2
    result = list(segments)
    clamped = 0

    for i in range(len(result) - 1):
        cur = result[i]
        nxt = result[i + 1]
        if cur.end < nxt.start - 0.01:
            continue
        target_end = nxt.start - min_gap_seconds
        if target_end > cur.start + min_duration:
            clamped_end = target_end
            result[i] = TranscriptSegment(
                text=cur.text,
                start=cur.start,
                duration=clamped_end - cur.start,
            )
            clamped += 1

    if clamped:
        logger.info("[STT] Clamped %d overlapping segment boundaries", clamped)
    return result
