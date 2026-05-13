import asyncio
import json
import logging
import os
from dataclasses import dataclass

import httpx
import pydantic
from google import genai

from app.config import get_settings

logger = logging.getLogger(__name__)

_settings = get_settings()
_client: genai.Client | None = None

# ── Google Cloud Translation ──────────────────────────────────────────────────

_translate_client = None
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))


def _resolve_creds_path() -> str | None:
    raw = _settings.GOOGLE_APPLICATION_CREDENTIALS
    if not raw:
        return None
    if os.path.isabs(raw):
        return raw
    return os.path.join(_PROJECT_ROOT, raw)


def _get_translate_client():
    global _translate_client
    if _translate_client is None:
        creds_path = _resolve_creds_path()
        if creds_path:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_path
        from google.cloud import translate as gc_translate
        _translate_client = gc_translate.TranslationServiceClient()
    return _translate_client


def _extract_project_id() -> str:
    creds_path = _resolve_creds_path()
    if creds_path:
        try:
            with open(creds_path) as f:
                return json.load(f).get("project_id", "")
        except Exception:
            pass
    return ""


async def google_translate(
    text: str,
    target_language: str = "vi",
    source_language: str = "en",
) -> str | None:
    """Translate text via Google Cloud Translation API (service account auth)."""
    try:
        client = _get_translate_client()
        project_id = _extract_project_id()
        if not project_id:
            logger.warning("No project_id found in GOOGLE_APPLICATION_CREDENTIALS")
            return None
        parent = f"projects/{project_id}/locations/global"

        response = await asyncio.to_thread(
            client.translate_text,
            request={
                "parent": parent,
                "contents": [text],
                "mime_type": "text/plain",
                "source_language_code": source_language,
                "target_language_code": target_language,
            },
        )
        result = response.translations[0].translated_text
        return result.strip() if result else None
    except Exception as e:
        logger.warning("Google Cloud Translation failed for %r: %s", text[:80], e)
        return None
_MODEL_NAME = "gemini-2.5-flash"


def _get_client() -> genai.Client | None:
    """Lazily construct the Gemini client."""
    global _client
    if _client is None:
        if not _settings.GEMINI_API_KEY:
            return None
        _client = genai.Client(api_key=_settings.GEMINI_API_KEY)
    return _client


async def punctuate_transcript(raw_text: str, language: str = "en") -> str | None:
    """Add punctuation and capitalization to raw auto-generated subtitle text.

    Auto-generated YouTube subtitles lack all sentence punctuation. This sends
    the raw text to Gemini and asks it to restore periods, commas, question marks,
    and capitalize sentence starts — WITHOUT changing, adding, or removing words.

    The punctuated text is then fed back into merge_segments_smart() which uses
    sentence-ending punctuation (.?!) to create proper sentence-level segments
    with correct audio timestamps.

    Returns punctuated text string, or None on failure. On None, the caller
    should fall back to the original unpunctuated text.
    """
    client = _get_client()
    if client is None:
        logger.warning("GEMINI_API_KEY not configured; skipping punctuation")
        return None
    if not raw_text.strip():
        return None

    words = raw_text.split()
    CHUNK_SIZE = 3000
    chunks: list[str] = []
    for i in range(0, len(words), CHUNK_SIZE):
        chunks.append(" ".join(words[i:i + CHUNK_SIZE]))

    punctuated_parts: list[str] = []

    for chunk_idx, chunk in enumerate(chunks):
        prompt = (
            "You are a punctuation restoration engine. "
            f"The following is a transcript in {language} from auto-generated YouTube subtitles. "
            "It has NO punctuation and NO proper capitalization.\n\n"
            "Your task:\n"
            "1. Add sentence-ending punctuation: periods (.), question marks (?), "
            "exclamation marks (!) at natural sentence boundaries\n"
            "2. Add commas (,) where grammatically appropriate\n"
            "3. Capitalize the first letter of each sentence\n"
            "4. Capitalize proper nouns (names of people, places, organizations)\n"
            "5. DO NOT change, add, remove, or reorder ANY words\n"
            "6. DO NOT add quotation marks, parentheses, or any other formatting\n"
            "7. The word count of your output MUST exactly match the input\n"
            "8. Return ONLY the punctuated text, with no preamble or explanation\n\n"
            f"Raw transcript:\n{chunk}"
        )

        last_exc: Exception | None = None
        attempts = 2
        success = False

        for attempt in range(attempts):
            try:
                response = await client.aio.models.generate_content(
                    model=_MODEL_NAME,
                    contents=prompt,
                )
                result = (response.text or "").strip()
                if result:
                    input_wc = len(chunk.split())
                    output_wc = len(result.split())
                    drift = abs(output_wc - input_wc) / max(input_wc, 1)
                    if drift > 0.30:
                        logger.warning(
                            "Gemini punctuation word-count drift too high "
                            "(chunk %d: input=%d, output=%d, drift=%.1f%%). "
                            "Discarding result.",
                            chunk_idx, input_wc, output_wc, drift * 100,
                        )
                        return None
                    if drift > 0.10:
                        logger.warning(
                            "Gemini punctuation word-count drift elevated but acceptable "
                            "(chunk %d: input=%d, output=%d, drift=%.1f%%).",
                            chunk_idx, input_wc, output_wc, drift * 100,
                        )
                    punctuated_parts.append(result)
                    success = True
                    break
            except Exception as e:
                logger.warning(
                    "Gemini punctuation failed (chunk %d, attempt %d): %s",
                    chunk_idx, attempt + 1, e,
                )
                last_exc = e
                if attempt < attempts - 1:
                    await asyncio.sleep(2.0)

        if not success:
            logger.warning(
                "Gemini punctuation gave up on chunk %d: %s", chunk_idx, last_exc,
            )
            return None

    full_result = " ".join(punctuated_parts)
    logger.warning(
        "Gemini punctuation completed: %d words in, %d words out",
        len(words), len(full_result.split()),
    )
    return full_result


def _audio_url_matches_word(audio_url: str, word: str) -> bool:
    """Conservative check: the audio filename must start with the exact word.

    dictionaryapi.dev hosts files like `hear-us.mp3`, `hear-uk.mp3`, `hear.mp3`.
    Rejecting anything that doesn't start with `{word}` (followed by `.`, `-`,
    or `_`) keeps us from silently caching "heart-us.mp3" when the user asked
    for "hear".
    """
    if not audio_url:
        return False
    try:
        path = httpx.URL(audio_url).path
    except Exception:
        return False
    filename = path.rsplit("/", 1)[-1].lower()
    w = word.lower()
    if not filename.startswith(w):
        return False
    trailing = filename[len(w):]
    return trailing == "" or trailing[0] in {".", "-", "_"}


_TTS_FALLBACK_PREFIX = "/api/v1/vocabulary/tts/"


@dataclass
class DictionaryResult:
    """Everything we can extract from a single dictionaryapi.dev call."""
    audio_url: str | None = None
    phonetic: str | None = None
    meaning_en: str | None = None
    meaning_vi: str | None = None
    part_of_speech: str | None = None


class DictionaryMeaning(pydantic.BaseModel):
    part_of_speech: str
    meaning_vi: str
    meaning_en: str


class GeminiDictionaryResponse(pydantic.BaseModel):
    phonetic: str
    meanings: list[DictionaryMeaning]


async def _fetch_gemini_dictionary(word: str) -> GeminiDictionaryResponse | None:
    """Use Gemini to get accurate IPA and English-Vietnamese dictionary definitions."""
    client = _get_client()
    if not client:
        return None

    prompt = (
        f"You are a professional English-Vietnamese dictionary. "
        f"Look up the English word '{word}'. "
        f"For 'phonetic', provide the most common IPA pronunciation (e.g. /wɪnd/ for wind meaning breeze). "
        f"For 'meaning_vi', provide concise, accurate Vietnamese dictionary equivalents (1-4 words max). "
        f"For 'meaning_en', provide a short English definition (under 10 words). "
        f"Do NOT provide long explanatory sentences. Return only the most common parts of speech (max 3)."
    )

    try:
        response = await client.aio.models.generate_content(
            model=_MODEL_NAME,
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=GeminiDictionaryResponse,
                temperature=0.1,
            ),
        )
        if response.text:
            data = json.loads(response.text)
            return GeminiDictionaryResponse(**data)
    except Exception as e:
        logger.warning("Gemini dictionary lookup failed for word=%r: %s", word, e)

    return None


async def _fetch_dictionary_audio_ipa(word: str) -> DictionaryResult:
    """Fetch audio URL and IPA from dictionaryapi.dev (meanings are handled by Gemini)."""
    url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{word}"
    result = DictionaryResult()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            entries = response.json()
    except Exception as e:
        logger.info("dictionaryapi.dev miss for word=%r: %s", word, e)
        from urllib.parse import quote
        result.audio_url = f"{_TTS_FALLBACK_PREFIX}{quote(word)}"
        return result

    for entry in entries or []:
        entry_word = (entry.get("word") or "").lower()
        if entry_word and entry_word != word.lower():
            continue

        if not result.phonetic:
            for phon in entry.get("phonetics", []) or []:
                text = phon.get("text")
                if text and text.strip():
                    result.phonetic = text.strip()
                    break
            if not result.phonetic:
                top = entry.get("phonetic")
                if top and top.strip():
                    result.phonetic = top.strip()

        if not result.audio_url:
            for phon in entry.get("phonetics", []) or []:
                audio = phon.get("audio")
                if audio and _audio_url_matches_word(audio, word):
                    result.audio_url = audio
                    break

        if result.phonetic and result.audio_url:
            break

    if not result.audio_url:
        from urllib.parse import quote
        result.audio_url = f"{_TTS_FALLBACK_PREFIX}{quote(word)}"

    return result


async def fetch_dictionary_data(word: str, translate_meaning: bool = True) -> DictionaryResult:
    """Fetch audio/IPA from dictionaryapi.dev and meanings from Gemini.

    Args:
        word: The word to look up.
        translate_meaning: When True (default), use Gemini for Vietnamese meanings.
    """
    dict_task = _fetch_dictionary_audio_ipa(word)

    if translate_meaning:
        dict_result, gemini_result = await asyncio.gather(
            dict_task,
            _fetch_gemini_dictionary(word),
        )
    else:
        dict_result = await dict_task
        gemini_result = None

    result = dict_result

    if gemini_result:
        if gemini_result.phonetic:
            result.phonetic = gemini_result.phonetic

        parts: list[str] = []
        for m in gemini_result.meanings:
            pos = m.part_of_speech.lower()
            parts.append(f"{pos}: {m.meaning_vi}")
            if not result.meaning_en:
                result.meaning_en = m.meaning_en
            if not result.meaning_vi:
                result.meaning_vi = m.meaning_vi
        result.part_of_speech = "; ".join(parts)

    return result