"""Nuclear option: drop ALL tables and recreate from models.
WARNING: This deletes EVERYTHING — users, videos, all data.

Usage:
    cd /path/to/backend
    python -m scripts.nuke_and_rebuild
"""

import asyncio

from app.database import Base, engine


async def main():
    confirm = input("This will DELETE ALL DATA. Type 'yes' to confirm: ")
    if confirm.strip().lower() != "yes":
        print("Aborted.")
        return

    async with engine.begin() as conn:
        print("Dropping all tables...")
        await conn.run_sync(Base.metadata.drop_all)
        print("Recreating all tables...")
        await conn.run_sync(Base.metadata.create_all)

    print("Done. All tables recreated (empty).")


if __name__ == "__main__":
    asyncio.run(main())