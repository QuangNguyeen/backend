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
        chunks.append(" ".join(words[i : i + CHUNK_SIZE]))

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
                            chunk_idx,
                            input_wc,
                            output_wc,
                            drift * 100,
                        )
                        return None
                    if drift > 0.10:
                        logger.warning(
                            "Gemini punctuation word-count drift elevated but acceptable "
                            "(chunk %d: input=%d, output=%d, drift=%.1f%%).",
                            chunk_idx,
                            input_wc,
                            output_wc,
                            drift * 100,
                        )
                    punctuated_parts.append(result)
                    success = True
                    break
            except Exception as e:
                logger.warning(
                    "Gemini punctuation failed (chunk %d, attempt %d): %s",
                    chunk_idx,
                    attempt + 1,
                    e,
                )
                last_exc = e
                if attempt < attempts - 1:
                    await asyncio.sleep(2.0)

        if not success:
            logger.warning(
                "Gemini punctuation gave up on chunk %d: %s",
                chunk_idx,
                last_exc,
            )
            return None

    full_result = " ".join(punctuated_parts)
    logger.warning(
        "Gemini punctuation completed: %d words in, %d words out",
        len(words),
        len(full_result.split()),
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
    trailing = filename[len(w) :]
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
        "For 'phonetic', provide the most common IPA pronunciation "
        "(e.g. /wɪnd/ for wind meaning breeze). "
        "For 'meaning_vi', provide concise, accurate Vietnamese dictionary "
        "equivalents (1-4 words max). "
        f"For 'meaning_en', provide a short English definition (under 10 words). "
        "Do NOT provide long explanatory sentences. Return only the most "
        "common parts of speech (max 3)."
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


class _ExampleItem(pydantic.BaseModel):
    word: str
    example: str


class _ExampleBatchResponse(pydantic.BaseModel):
    items: list[_ExampleItem]


async def generate_example_sentences(words: list[str]) -> dict[str, str]:
    """Generate one natural B1-level example sentence per word via Gemini.

    Returns a mapping of ``word -> example sentence``. Words for which Gemini
    fails to produce a sentence are simply omitted from the result. Safe to call
    with an empty list (returns ``{}``).
    """
    if not words:
        return {}

    client = _get_client()
    if client is None:
        logger.warning("GEMINI_API_KEY not configured; skipping example generation")
        return {}

    word_list = "\n".join(words)
    prompt = (
        "You are an English teacher. For each English word below, write ONE "
        "natural, B1-level example sentence that uses the word in context. "
        "Keep each sentence under 18 words. Return the word exactly as given.\n\n"
        f"Words:\n{word_list}"
    )

    try:
        response = await client.aio.models.generate_content(
            model=_MODEL_NAME,
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_ExampleBatchResponse,
                temperature=0.4,
            ),
        )
        if not response.text:
            return {}
        data = _ExampleBatchResponse(**json.loads(response.text))
    except Exception as e:
        logger.warning("Gemini example generation failed for %d words: %s", len(words), e)
        return {}

    wanted = {w.lower(): w for w in words}
    out: dict[str, str] = {}
    for item in data.items:
        key = item.word.strip().lower()
        original = wanted.get(key)
        if original and item.example and item.example.strip():
            out[original] = item.example.strip()
    return out


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


_dict_cache: dict[str, tuple[float, DictionaryResult]] = {}
_DICT_CACHE_TTL = 1800  # 30 min
_DICT_CACHE_MAX = 3000


async def fetch_dictionary_data(word: str, translate_meaning: bool = True) -> DictionaryResult:
    """Fetch dictionary data for a word.

    Primary: Cambridge Dictionary (en + en-vi in parallel) for IPA, audio, and translations.
    Fallback: dictionaryapi.dev + Gemini if Cambridge fails.
    """
    import time as _time

    cache_key = f"{word}:{translate_meaning}"
    cached = _dict_cache.get(cache_key)
    if cached and (_time.monotonic() - cached[0]) < _DICT_CACHE_TTL:
        return cached[1]

    from app.services.cambridge_dictionary import lookup as cambridge_lookup

    result = DictionaryResult()

    # Primary path: Cambridge Dictionary (parallel en + en-vi)
    try:
        if translate_meaning:
            en_entry, vi_entry = await asyncio.gather(
                cambridge_lookup(word, dictionary="en"),
                cambridge_lookup(word, dictionary="en-vi"),
            )
        else:
            en_entry = await cambridge_lookup(word, dictionary="en")
            vi_entry = None

        entry = en_entry or vi_entry
        if entry:
            # IPA & audio - prefer UK pronunciation
            for pron in entry.pronunciations:
                if not result.phonetic and pron.ipa:
                    result.phonetic = pron.ipa
                if not result.audio_url and pron.audio_url:
                    result.audio_url = pron.audio_url

            # Detect inflected forms ("past simple of swim", "plural of house")
            is_word_form = False
            if en_entry and en_entry.definitions:
                first_def = en_entry.definitions[0].definition.lower()
                if any(
                    marker in first_def
                    for marker in (
                        "past simple of",
                        "past participle of",
                        "present participle of",
                        "plural of",
                        "comparative of",
                        "superlative of",
                        "third person singular of",
                        "short form of",
                    )
                ):
                    is_word_form = True
                    result.meaning_en = en_entry.definitions[0].definition

            # Part of speech with Vietnamese meanings
            if vi_entry and vi_entry.definitions:
                if is_word_form:
                    # Inflected form: only take the first translation
                    d = vi_entry.definitions[0]
                    result.meaning_vi = d.translation
                    result.part_of_speech = d.pos or ""
                else:
                    # Base form: collect unique POS+translation pairs (max 3)
                    parts: list[str] = []
                    seen_trans: set[str] = set()
                    for d in vi_entry.definitions:
                        if d.translation and d.translation not in seen_trans:
                            seen_trans.add(d.translation)
                            pos = d.pos or ""
                            parts.append(f"{pos}: {d.translation}" if pos else d.translation)
                            if not result.meaning_vi:
                                result.meaning_vi = d.translation
                            if len(parts) >= 3:
                                break
                    if parts:
                        result.part_of_speech = "; ".join(parts)
            elif en_entry and en_entry.definitions:
                if not result.meaning_en:
                    result.meaning_en = en_entry.definitions[0].definition
                pos_parts = []
                for d in en_entry.definitions:
                    if d.pos and d.pos not in pos_parts:
                        pos_parts.append(d.pos)
                result.part_of_speech = ", ".join(pos_parts)

            has_data = result.phonetic or result.audio_url or result.meaning_vi
            needs_vi = translate_meaning and not result.meaning_vi
            if has_data and not needs_vi:
                if not result.audio_url:
                    from urllib.parse import quote

                    result.audio_url = f"{_TTS_FALLBACK_PREFIX}{quote(word)}"
                _dict_cache[cache_key] = (_time.monotonic(), result)
                if len(_dict_cache) > _DICT_CACHE_MAX:
                    oldest = min(_dict_cache, key=lambda k: _dict_cache[k][0])
                    _dict_cache.pop(oldest, None)
                return result

            # Cambridge English found but no Vietnamese translation — use Gemini for meaning only
            if en_entry and needs_vi:
                gemini_result = await _fetch_gemini_dictionary(word)
                if gemini_result:
                    if not result.phonetic and gemini_result.phonetic:
                        result.phonetic = gemini_result.phonetic
                    parts: list[str] = []
                    for m in gemini_result.meanings:
                        pos = m.part_of_speech.lower()
                        parts.append(f"{pos}: {m.meaning_vi}")
                        if not result.meaning_en:
                            result.meaning_en = m.meaning_en
                        if not result.meaning_vi:
                            result.meaning_vi = m.meaning_vi
                    if parts:
                        result.part_of_speech = "; ".join(parts)
                if not result.audio_url:
                    from urllib.parse import quote

                    result.audio_url = f"{_TTS_FALLBACK_PREFIX}{quote(word)}"
                _dict_cache[cache_key] = (_time.monotonic(), result)
                if len(_dict_cache) > _DICT_CACHE_MAX:
                    oldest = min(_dict_cache, key=lambda k: _dict_cache[k][0])
                    _dict_cache.pop(oldest, None)
                return result
    except Exception as e:
        logger.warning("Cambridge lookup failed for %r, falling back: %s", word, e)

    # Full fallback: dictionaryapi.dev + Gemini (when Cambridge failed entirely)
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

    _dict_cache[cache_key] = (_time.monotonic(), result)
    if len(_dict_cache) > _DICT_CACHE_MAX:
        oldest = min(_dict_cache, key=lambda k: _dict_cache[k][0])
        _dict_cache.pop(oldest, None)
    return result
