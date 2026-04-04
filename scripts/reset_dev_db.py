"""Hard reset: wipe dictation history + vocabulary, fix dirty video levels.
Preserves users and videos.

Usage:
    cd /path/to/backend
    python -m scripts.reset_dev_db
"""

import asyncio

from sqlalchemy import text

from app.database import engine


async def main():
    async with engine.begin() as conn:
        # Order matters: children first (FK constraints)
        print("[1/4] Deleting dictation_sentences...")
        r = await conn.execute(text("DELETE FROM dictation_sentences"))
        print(f"       Removed {r.rowcount} rows")

        print("[2/4] Deleting dictation_attempts...")
        r = await conn.execute(text("DELETE FROM dictation_attempts"))
        print(f"       Removed {r.rowcount} rows")

        print("[3/4] Deleting saved_words...")
        r = await conn.execute(text("DELETE FROM saved_words"))
        print(f"       Removed {r.rowcount} rows")

        print("[4/4] Cleaning dirty CEFR levels (removing '~' prefix)...")
        r = await conn.execute(
            text("UPDATE videos SET level = REPLACE(level, '~', '') WHERE level LIKE '~%'")
        )
        print(f"       Fixed {r.rowcount} videos")

    print("\nDone. Users and videos are intact.")


if __name__ == "__main__":
    asyncio.run(main())