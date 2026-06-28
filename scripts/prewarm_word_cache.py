"""
Pre-warm word_cache with 10,000 most common English words.

Standalone script — no backend app dependency.
Calls Gemini API for Vietnamese meanings + IPA, inserts into PostgreSQL directly.

Usage:
    python scripts/prewarm_word_cache.py
    python scripts/prewarm_word_cache.py --batch-size 20 --limit 500
    python scripts/prewarm_word_cache.py --word-file my_words.txt
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path

import asyncpg
import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("prewarm")

# ── Config ───────────────────────────────────────────────────────────────────

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise SystemExit(
        "GEMINI_API_KEY is not set. Export it (e.g. from your .env) before running "
        "this script — do not hardcode API keys in source."
    )
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}"
    f":generateContent?key={GEMINI_API_KEY}"
)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:secret@localhost:5432/dictalearn",
)

DICT_API = "https://api.dictionaryapi.dev/api/v2/entries/en"

TTS_FALLBACK = "/api/v1/vocabulary/tts/"


# ── Word list (top 10,000 English words by frequency) ────────────────────────

WORD_LIST_URL = "https://raw.githubusercontent.com/first20hours/google-10000-english/master/google-10000-english-no-swears.txt"


async def fetch_word_list(limit: int) -> list[str]:
    """Download the 10k frequency list from GitHub."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(WORD_LIST_URL)
        resp.raise_for_status()
    words = [w.strip().lower() for w in resp.text.splitlines() if w.strip().isalpha()]
    return words[:limit]


def load_word_file(path: str) -> list[str]:
    """Load words from a local file (one per line)."""
    return [w.strip().lower() for w in Path(path).read_text().splitlines() if w.strip().isalpha()]


# ── Gemini call ──────────────────────────────────────────────────────────────

GEMINI_PROMPT_TEMPLATE = (
    "You are a professional English-Vietnamese dictionary. "
    "Look up the English word '{word}'. "
    "Return JSON with this exact structure: "
    '{{"phonetic": "<IPA>", "meanings": ['
    '{{"part_of_speech": "<pos>", "meaning_vi": "<Vietnamese, 1-4 words>", "meaning_en": "<English def, under 10 words>"}}'
    "]}}\n"
    "Rules:\n"
    "- 'phonetic': most common IPA (e.g. /wɪnd/ for wind meaning breeze)\n"
    "- 'meaning_vi': concise Vietnamese dictionary equivalents, NOT long sentences\n"
    "- Max 3 parts of speech, most common only\n"
    "- Return ONLY valid JSON, no markdown"
)


async def call_gemini(
    client: httpx.AsyncClient, word: str, retries: int = 2
) -> dict | None:
    """Call Gemini API and parse JSON response."""
    payload = {
        "contents": [{"parts": [{"text": GEMINI_PROMPT_TEMPLATE.format(word=word)}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    for attempt in range(retries + 1):
        try:
            resp = await client.post(GEMINI_URL, json=payload, timeout=20)
            if resp.status_code == 429:
                wait = 2 ** attempt * 5
                log.warning("Rate limited on %r, waiting %ds", word, wait)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except Exception as e:
            if attempt < retries:
                await asyncio.sleep(1)
                continue
            log.warning("Gemini failed for %r: %s", word, e)
            return None
    return None


# ── dictionaryapi.dev ────────────────────────────────────────────────────────

async def fetch_audio_url(client: httpx.AsyncClient, word: str) -> str | None:
    """Fetch audio URL from dictionaryapi.dev."""
    try:
        resp = await client.get(f"{DICT_API}/{word}", timeout=5)
        if resp.status_code != 200:
            return None
        entries = resp.json()
        for entry in entries:
            for phon in entry.get("phonetics", []):
                audio = phon.get("audio", "")
                if audio and word.lower() in audio.lower():
                    return audio
    except Exception:
        pass
    return None


# ── Process one word ─────────────────────────────────────────────────────────

async def process_word(
    client: httpx.AsyncClient, word: str
) -> dict | None:
    """Fetch Gemini meanings + dictionary audio for a single word."""
    gemini_data, audio_url = await asyncio.gather(
        call_gemini(client, word),
        fetch_audio_url(client, word),
    )
    if not audio_url:
        from urllib.parse import quote
        audio_url = f"{TTS_FALLBACK}{quote(word)}"

    if not gemini_data:
        return None

    # Gemini may return a list or a dict depending on response
    if isinstance(gemini_data, list):
        gemini_data = gemini_data[0] if gemini_data else {}
    if not isinstance(gemini_data, dict):
        return None

    phonetic = gemini_data.get("phonetic")
    meanings = gemini_data.get("meanings", [])
    if not meanings:
        return None

    parts = []
    first_vi = None
    for m in meanings:
        pos = m.get("part_of_speech", "").lower()
        vi = m.get("meaning_vi", "")
        if pos and vi:
            parts.append(f"{pos}: {vi}")
            if first_vi is None:
                first_vi = vi

    if not audio_url:
        from urllib.parse import quote
        audio_url = f"{TTS_FALLBACK}{quote(word)}"

    return {
        "id": str(uuid.uuid4()),
        "word": word,
        "context_hash": "__global__",
        "phonetic": phonetic,
        "audio_url": audio_url,
        "vietnamese_meaning": first_vi,
        "part_of_speech": "; ".join(parts) if parts else None,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

async def main(
    batch_size: int = 15,
    limit: int = 10_000,
    word_file: str | None = None,
):
    if word_file:
        words = load_word_file(word_file)[:limit]
        log.info("Loaded %d words from %s", len(words), word_file)
    else:
        log.info("Downloading word list (limit=%d)...", limit)
        words = await fetch_word_list(limit)
        log.info("Got %d words", len(words))

    # Connect to DB and find already-cached words
    conn = await asyncpg.connect(DATABASE_URL)
    existing = set(
        row["word"]
        for row in await conn.fetch(
            "SELECT DISTINCT word FROM word_cache WHERE vietnamese_meaning IS NOT NULL"
        )
    )
    to_process = [w for w in words if w not in existing]
    log.info(
        "Skipping %d already cached. Processing %d words.",
        len(words) - len(to_process),
        len(to_process),
    )

    done = 0
    failed = 0
    t0 = time.time()

    async with httpx.AsyncClient() as client:
        for i in range(0, len(to_process), batch_size):
            batch = to_process[i : i + batch_size]
            results = await asyncio.gather(
                *(process_word(client, w) for w in batch)
            )

            rows = [r for r in results if r is not None]
            failed += len(batch) - len(rows)

            if rows:
                await conn.executemany(
                    """
                    INSERT INTO word_cache (id, word, context_hash, phonetic, audio_url, vietnamese_meaning, part_of_speech)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (word, context_hash) DO NOTHING
                    """,
                    [
                        (
                            r["id"],
                            r["word"],
                            r["context_hash"],
                            r["phonetic"],
                            r["audio_url"],
                            r["vietnamese_meaning"],
                            r["part_of_speech"],
                        )
                        for r in rows
                    ],
                )

            done += len(rows)
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (len(to_process) - done - failed) / rate if rate > 0 else 0
            log.info(
                "Progress: %d/%d done, %d failed | %.1f words/s | ETA %.0fs",
                done,
                len(to_process),
                failed,
                rate,
                eta,
            )

            # Small delay between batches to avoid rate limits
            await asyncio.sleep(0.5)

    await conn.close()
    elapsed = time.time() - t0
    log.info(
        "Finished: %d cached, %d failed, %.1fs total (%.1f words/s)",
        done,
        failed,
        elapsed,
        done / elapsed if elapsed > 0 else 0,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pre-warm word_cache with Gemini")
    parser.add_argument("--batch-size", type=int, default=15, help="Concurrent requests per batch")
    parser.add_argument("--limit", type=int, default=10_000, help="Max words to process")
    parser.add_argument("--word-file", type=str, help="Custom word list file (one word per line)")
    args = parser.parse_args()

    asyncio.run(main(batch_size=args.batch_size, limit=args.limit, word_file=args.word_file))
