"""
migrate_sqlite_dict.py
~~~~~~~~~~~~~~~~~~~~~~
Migrates an English-Vietnamese SQLite dictionary into the existing
`word_cache` table in PostgreSQL using context_hash = '__global__'.

Usage:
    python scripts/migrate_sqlite_dict.py --sqlite /path/to/dict.db

Flags:
    --sqlite    Path to the SQLite dictionary file (required)
    --dsn       PostgreSQL DSN (default: reads DATABASE_URL from .env)
    --batch     Insert batch size (default: 500)
    --dry-run   Print first 10 rows without inserting
"""
import argparse
import asyncio
import logging
import os
import sqlite3
import sys
import uuid
from pathlib import Path

# ── Allow running from repo root without installing the package ───────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import asyncpg

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CONTEXT_HASH = "__global__"


# ── Query SQLite ──────────────────────────────────────────────────────────────

SQLITE_QUERY = """
SELECT
    w.word                              AS word,
    -- IPA: prefer US region, fall back to any
    COALESCE(
        (SELECT p.ipa FROM pronunciations p
         WHERE p.word_id = w.id AND p.region = 'us' LIMIT 1),
        (SELECT p.ipa FROM pronunciations p
         WHERE p.word_id = w.id LIMIT 1)
    )                                   AS phonetic,
    -- Part of speech: comma-separated unique POS values
    (SELECT GROUP_CONCAT(DISTINCT d.pos)
     FROM word_definitions wd
     JOIN definitions d ON d.id = wd.definition_id
     WHERE wd.word_id = w.id AND d.pos IS NOT NULL
    )                                   AS part_of_speech,
    -- Vietnamese meaning: first translation
    (SELECT t.translation
     FROM translations t
     WHERE t.word_id = w.id AND t.lang_code = 'vi'
     LIMIT 1
    )                                   AS vietnamese_meaning
FROM words w
WHERE w.lang_code = 'en'
  AND w.word IS NOT NULL
  AND w.word != ''
  -- Only import words that actually have a Vietnamese translation
  AND EXISTS (
      SELECT 1 FROM translations t
      WHERE t.word_id = w.id AND t.lang_code = 'vi'
  )
ORDER BY w.word;
"""


def read_sqlite(db_path: str) -> list[dict]:
    """Read all English words with their Vietnamese translations from SQLite."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(SQLITE_QUERY)
        rows = [dict(r) for r in cur.fetchall()]
        logger.info("SQLite: read %d rows", len(rows))
        return rows
    finally:
        conn.close()


# ── Upsert into PostgreSQL ────────────────────────────────────────────────────

UPSERT_SQL = """
INSERT INTO word_cache (id, word, context_hash, phonetic, audio_url,
                        vietnamese_meaning, context_translation, part_of_speech)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (word, context_hash) DO UPDATE SET
    phonetic           = COALESCE(EXCLUDED.phonetic,           word_cache.phonetic),
    vietnamese_meaning = COALESCE(EXCLUDED.vietnamese_meaning, word_cache.vietnamese_meaning),
    part_of_speech     = COALESCE(EXCLUDED.part_of_speech,     word_cache.part_of_speech)
WHERE word_cache.vietnamese_meaning IS NULL
   OR word_cache.phonetic           IS NULL;
"""


async def upsert_batch(conn: asyncpg.Connection, batch: list[dict]) -> int:
    """Upsert a batch and return number of rows attempted."""
    records = [
        (
            str(uuid.uuid4()),          # id
            row["word"].lower().strip(),# word (normalised)
            CONTEXT_HASH,               # context_hash
            row.get("phonetic"),        # phonetic (nullable)
            None,                       # audio_url — edge-tts handles this at runtime
            row.get("vietnamese_meaning"),
            None,                       # context_translation — not applicable for dict entries
            row.get("part_of_speech"),
        )
        for row in batch
        # Skip rows where meaning came back empty after strip
        if row.get("vietnamese_meaning") and row["vietnamese_meaning"].strip()
    ]
    await conn.executemany(UPSERT_SQL, records)
    return len(records)


async def run(sqlite_path: str, dsn: str, batch_size: int, dry_run: bool) -> None:
    rows = read_sqlite(sqlite_path)

    if dry_run:
        logger.info("DRY RUN — first 10 rows:")
        for r in rows[:10]:
            print(r)
        return

    logger.info("Connecting to PostgreSQL...")
    conn = await asyncpg.connect(dsn)

    try:
        total_inserted = 0
        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            inserted = await upsert_batch(conn, batch)
            total_inserted += inserted
            logger.info(
                "Progress: %d / %d  (batch %d inserted)",
                min(i + batch_size, len(rows)),
                len(rows),
                inserted,
            )

        logger.info("✅ Done. Total rows upserted: %d", total_inserted)
    finally:
        await conn.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate SQLite EN-VI dict → word_cache")
    parser.add_argument("--sqlite", required=True, help="Path to .db SQLite file")
    parser.add_argument(
        "--dsn",
        default=os.getenv("DATABASE_URL", ""),
        help="PostgreSQL DSN (default: DATABASE_URL env var)",
    )
    parser.add_argument("--batch", type=int, default=500, help="Insert batch size")
    parser.add_argument("--dry-run", action="store_true", help="Preview without inserting")
    args = parser.parse_args()

    if not args.sqlite or not Path(args.sqlite).exists():
        parser.error(f"SQLite file not found: {args.sqlite}")

    if not args.dry_run and not args.dsn:
        parser.error("--dsn or DATABASE_URL env var is required")

    # asyncpg expects postgresql:// not postgresql+asyncpg://
    dsn = args.dsn.replace("postgresql+asyncpg://", "postgresql://")

    asyncio.run(run(args.sqlite, dsn, args.batch, args.dry_run))


if __name__ == "__main__":
    main()
