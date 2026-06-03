"""Cambridge Dictionary scraper service.

Fetches word definitions, pronunciations, and examples from
Cambridge Dictionary (dictionary.cambridge.org).
"""

import asyncio
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

_BASE_URL = "https://dictionary.cambridge.org/dictionary"
_AUDIO_BASE = "https://dictionary.cambridge.org"
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# lxml is ~1.4x faster; installed as optional dependency
try:
    import lxml  # noqa: F401

    _PARSER = "lxml"
except ImportError:
    _PARSER = "html.parser"

# Regex to extract just the dictionary body (~22KB vs ~260KB full page)
_DICT_BODY_RE = re.compile(
    r'(<div class="pr dictionary".*?</div>\s*<!--)',
    re.DOTALL,
)

# ── In-memory LRU cache (O(1) eviction via OrderedDict) ────────────────────
_CACHE_TTL = 3600
_CACHE_MAX_SIZE = 5000
_cache: OrderedDict[tuple[str, str], tuple[float, "DictionaryEntry | None"]] = OrderedDict()
_cache_lock = asyncio.Lock()
_inflight: dict[tuple[str, str], asyncio.Future["DictionaryEntry | None"]] = {}

# ── Shared HTTP client ──────────────────────────────────────────────────────
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _http_client


def _trim_html(html: str) -> str:
    """Extract only the dictionary entry section to reduce parse cost."""
    m = _DICT_BODY_RE.search(html)
    return m.group(0) if m else html


SUPPORTED_DICTS = {
    "en": "english",
    "en-vi": "english-vietnamese",
    "en-cn": "english-chinese-simplified",
    "en-tw": "english-chinese-traditional",
    "en-ja": "english-japanese",
    "en-ko": "english-korean",
    "en-fr": "english-french",
    "en-de": "english-german",
}


@dataclass
class Pronunciation:
    lang: str
    ipa: str
    audio_url: str = ""


@dataclass
class Example:
    text: str
    translation: str = ""


@dataclass
class Definition:
    pos: str
    definition: str
    translation: str = ""
    examples: list[Example] = field(default_factory=list)
    level: str = ""
    grammar: str = ""
    labels: str = ""


@dataclass
class DictionaryEntry:
    word: str
    pos: list[str] = field(default_factory=list)
    pronunciations: list[Pronunciation] = field(default_factory=list)
    definitions: list[Definition] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "word": self.word,
            "pos": self.pos,
            "pronunciations": [
                {"lang": p.lang, "ipa": p.ipa, "audio_url": p.audio_url}
                for p in self.pronunciations
            ],
            "definitions": [
                {
                    "pos": d.pos,
                    "definition": d.definition,
                    "translation": d.translation,
                    "examples": [
                        {"text": e.text, "translation": e.translation} for e in d.examples
                    ],
                    "level": d.level,
                    "grammar": d.grammar,
                    "labels": d.labels,
                }
                for d in self.definitions
            ],
        }


def _text(el: Tag | None, separator: str = " ") -> str:
    if el is None:
        return ""
    return el.get_text(separator, strip=True)


def _abs_audio(src: str) -> str:
    if not src:
        return ""
    return src if src.startswith("http") else f"{_AUDIO_BASE}{src}"


def _parse_pronunciations(soup: BeautifulSoup) -> list[Pronunciation]:
    """Parse pronunciations - handles both English and bilingual page layouts."""
    pronunciations: list[Pronunciation] = []
    seen = set()

    # Layout 1: English dictionary - has .dpron-i blocks with region labels
    for pron_block in soup.select(".dpron-i"):
        region_el = pron_block.select_one(".region.dreg")
        region = _text(region_el).lower() if region_el else ""

        ipa_el = pron_block.select_one(".pron.dpron")
        ipa = _text(ipa_el)

        audio_url = ""
        source_el = pron_block.select_one("source[type='audio/mpeg']")
        if source_el and source_el.get("src"):
            audio_url = _abs_audio(source_el["src"])

        key = (region, ipa)
        if key not in seen and ipa:
            seen.add(key)
            pronunciations.append(Pronunciation(lang=region, ipa=ipa, audio_url=audio_url))

    # Layout 2: Bilingual dictionaries - simpler structure
    if not pronunciations:
        # Extract the single IPA from the header
        shared_ipa = ""
        for header in soup.select(".dpos-h"):
            ipa_el = header.select_one(".pron.dpron")
            shared_ipa = _text(ipa_el)
            if not shared_ipa:
                ipa_inner = header.select_one(".ipa.dipa")
                shared_ipa = f"/{_text(ipa_inner)}/" if ipa_inner else ""
            if shared_ipa:
                break

        # Build pronunciations from audio sources, sharing the IPA
        audio_sources = soup.select(".daud source[type='audio/mpeg']")
        for source in audio_sources:
            src = _abs_audio(source.get("src", ""))
            if not src:
                continue
            lang = "uk" if "uk_pron" in src else "us" if "us_pron" in src else ""
            key = (lang, src)
            if key not in seen:
                seen.add(key)
                pronunciations.append(Pronunciation(lang=lang, ipa=shared_ipa, audio_url=src))

        # If no audio but have IPA, still add it
        if not pronunciations and shared_ipa:
            pronunciations.append(Pronunciation(lang="", ipa=shared_ipa, audio_url=""))

    return pronunciations


def _parse_definitions(soup: BeautifulSoup) -> list[Definition]:
    """Parse definitions - handles both layouts."""
    definitions: list[Definition] = []

    # Layout 1: English dictionary with .entry-body__el containers
    entry_bodies = soup.select(".entry-body__el")

    if entry_bodies:
        for entry_body in entry_bodies:
            pos_el = entry_body.select_one(".pos.dpos")
            pos = _text(pos_el)

            gram_el = entry_body.select_one(".gram.dgram")
            grammar = _text(gram_el)

            for def_block in entry_body.select(".def-block.ddef_block"):
                definitions.append(_parse_def_block(def_block, pos, grammar))
    else:
        # Layout 2: Bilingual - definitions at top level
        pos_el = soup.select_one(".pos.dpos")
        pos = _text(pos_el)

        for def_block in soup.select(".def-block.ddef_block"):
            definitions.append(_parse_def_block(def_block, pos, ""))

    return definitions


def _parse_def_block(def_block: Tag, pos: str, grammar: str) -> Definition:
    """Parse a single definition block."""
    level_el = def_block.select_one(".epp-xref.dxref")
    level = _text(level_el)

    label_el = def_block.select_one(".usage.dusage")
    labels = _text(label_el)

    def_el = def_block.select_one(".def.ddef_d.db") or def_block.select_one(".def.ddef_d")
    definition_text = _text(def_el).rstrip(": ")

    # Translation
    trans_el = def_block.select_one(".trans.dtrans.dtrans-se") or def_block.select_one(
        ".def-body .trans.dtrans"
    )
    translation = _text(trans_el)

    # Examples
    examples: list[Example] = []
    for ex_block in def_block.select(".examp.dexamp"):
        ex_text_el = ex_block.select_one(".eg.deg") or ex_block.select_one(".eg")
        ex_text = _text(ex_text_el)

        ex_trans_el = ex_block.select_one(".trans.dtrans.hdb")
        ex_trans = _text(ex_trans_el)

        if ex_text:
            examples.append(Example(text=ex_text, translation=ex_trans))

    return Definition(
        pos=pos,
        definition=definition_text,
        translation=translation,
        examples=examples,
        level=level,
        grammar=grammar,
        labels=labels,
    )


def _parse_pos(soup: BeautifulSoup) -> list[str]:
    pos_list: list[str] = []
    seen = set()
    for el in soup.select(".pos.dpos"):
        text = _text(el)
        if text and text not in seen:
            seen.add(text)
            pos_list.append(text)
    return pos_list


async def _lookup_uncached(word: str, dictionary: str) -> DictionaryEntry | None:
    """Actual HTTP fetch + parse (no caching)."""
    dict_slug = SUPPORTED_DICTS.get(dictionary, "english-vietnamese")
    url = f"{_BASE_URL}/{dict_slug}/{word}"

    client = _get_http_client()
    try:
        response = await client.get(url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        # Cambridge redirects to the dictionary homepage when a word doesn't exist
        # (302 → /dictionary/{slug}/) instead of returning 404
        final_path = str(response.url).rstrip("/")
        expected_suffix = f"/{dict_slug}/{word}"
        if not final_path.endswith(expected_suffix):
            return None
        html = response.text
    except httpx.HTTPStatusError:
        return None
    except Exception as e:
        logger.warning("[DICT] Request failed for %s: %s", word, e)
        return None

    html = _trim_html(html)
    soup = BeautifulSoup(html, _PARSER)

    hw_el = soup.select_one(".hw.dhw") or soup.select_one(".dhw")
    headword = _text(hw_el) or word

    entry = DictionaryEntry(word=headword)
    entry.pos = _parse_pos(soup)
    entry.pronunciations = _parse_pronunciations(soup)
    entry.definitions = _parse_definitions(soup)

    if not entry.definitions:
        return None

    logger.info(
        "[DICT] '%s' (%s): %d pron, %d defs",
        headword,
        dictionary,
        len(entry.pronunciations),
        len(entry.definitions),
    )
    return entry


async def lookup(word: str, dictionary: str = "en-vi") -> DictionaryEntry | None:
    """Look up a word with in-memory TTL cache and request deduplication."""
    key = (word, dictionary)
    now = time.monotonic()

    # Fast path: cache hit (move to end = most recently used)
    cached = _cache.get(key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        _cache.move_to_end(key)
        return cached[1]

    # Deduplicate concurrent requests for the same word
    async with _cache_lock:
        cached = _cache.get(key)
        if cached and (now - cached[0]) < _CACHE_TTL:
            _cache.move_to_end(key)
            return cached[1]
        if key in _inflight:
            fut = _inflight[key]
        else:
            fut = asyncio.get_running_loop().create_future()
            _inflight[key] = fut

            async def _do_lookup() -> None:
                try:
                    result = await _lookup_uncached(word, dictionary)
                    _cache[key] = (time.monotonic(), result)
                    while len(_cache) > _CACHE_MAX_SIZE:
                        _cache.popitem(last=False)
                    fut.set_result(result)
                except Exception as exc:
                    fut.set_exception(exc)
                finally:
                    _inflight.pop(key, None)

            asyncio.create_task(_do_lookup())

    return await fut
