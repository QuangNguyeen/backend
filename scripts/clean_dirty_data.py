"""Surgical cleanup: fix dirty data without wiping valid records.

Fixes:
  1. Stuck in_progress attempts where progress == total (should be completed)
  2. Orphan dictation_sentences with no matching attempt
  3. Duplicate attempts per user+video (keeps newest, deletes older)
  4. Dirty CEFR levels with '~' prefix

Usage:
    cd /path/to/backend
    python -m scripts.clean_dirty_data
"""

import asyncio

from sqlalchemy import text

from app.database import engine


async def main():
    async with engine.begin() as conn:
        # 1. Fix stuck attempts: in_progress but progress == total
        print("[1/5] Fixing stuck in_progress attempts...")
        r = await conn.execute(text("""
            UPDATE dictation_attempts
            SET status = 'completed',
                completed_at = updated_at
            WHERE status = 'in_progress'
              AND total_sentences IS NOT NULL
              AND current_sentence_index >= total_sentences
        """))
        print(f"       Fixed {r.rowcount} stuck attempts")

        # 2. Compute missing scores for newly-completed attempts
        print("[2/5] Computing missing scores for completed attempts...")
        r = await conn.execute(text("""
            UPDATE dictation_attempts a
            SET score = sub.avg_score
            FROM (
                SELECT attempt_id, AVG(score) AS avg_score
                FROM dictation_sentences
                WHERE score IS NOT NULL
                GROUP BY attempt_id
            ) sub
            WHERE a.id = sub.attempt_id
              AND a.status = 'completed'
              AND a.score IS NULL
        """))
        print(f"       Scored {r.rowcount} attempts")

        # 3. Delete orphan sentences (no matching attempt)
        print("[3/5] Removing orphan dictation_sentences...")
        r = await conn.execute(text("""
            DELETE FROM dictation_sentences
            WHERE attempt_id NOT IN (SELECT id FROM dictation_attempts)
        """))
        print(f"       Removed {r.rowcount} orphans")

        # 4. Deduplicate attempts (keep newest per user+video)
        print("[4/5] Deduplicating attempts (keeping newest per user+video)...")
        r = await conn.execute(text("""
            DELETE FROM dictation_sentences
            WHERE attempt_id IN (
                SELECT id FROM dictation_attempts a
                WHERE EXISTS (
                    SELECT 1 FROM dictation_attempts b
                    WHERE b.user_id = a.user_id
                      AND b.video_id = a.video_id
                      AND b.updated_at > a.updated_at
                )
            )
        """))
        print(f"       Removed {r.rowcount} duplicate sentences")
        r = await conn.execute(text("""
            DELETE FROM dictation_attempts a
            WHERE EXISTS (
                SELECT 1 FROM dictation_attempts b
                WHERE b.user_id = a.user_id
                  AND b.video_id = a.video_id
                  AND b.updated_at > a.updated_at
            )
        """))
        print(f"       Removed {r.rowcount} duplicate attempts")

        # 5. Clean dirty CEFR levels
        print("[5/5] Cleaning dirty CEFR levels...")
        r = await conn.execute(
            text("UPDATE videos SET level = REPLACE(level, '~', '') WHERE level LIKE '~%'")
        )
        print(f"       Fixed {r.rowcount} videos")

    print("\nDone. Only dirty data was removed.")


if __name__ == "__main__":
    asyncio.run(main())