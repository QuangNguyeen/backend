#!/usr/bin/env bash
# DictaLearn Backend - Docker Entrypoint (shared by web + worker).
# Waits for the DB, optionally runs migrations, then execs the container command
# if one was provided (e.g. the Celery worker), otherwise starts uvicorn.
set -e

echo "Starting DictaLearn Backend..."

echo "Waiting for database..."
python - <<'PY'
import asyncio
import os
import sys
import time

import asyncpg

url = os.environ["DATABASE_URL"].replace("+asyncpg", "")


async def check() -> bool:
    try:
        conn = await asyncpg.connect(url)
        await conn.close()
        return True
    except Exception:
        return False


for _ in range(60):
    if asyncio.run(check()):
        print("Database is ready!")
        sys.exit(0)
    print("DB not ready, waiting...")
    time.sleep(2)

print("Database never became ready")
sys.exit(1)
PY

# Only the web container runs migrations (worker sets RUN_MIGRATIONS=0 to avoid a race).
if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
    echo "Running alembic upgrade head..."
    alembic upgrade head
fi

# If a command was given (e.g. the Celery worker), run it instead of uvicorn.
if [ "$#" -gt 0 ]; then
    echo "Starting: $*"
    exec "$@"
fi

echo "Starting uvicorn server..."
exec uvicorn app.main:app \
    --host 0.0.0.0 --port 8000 \
    --workers "${UVICORN_WORKERS:-4}" \
    --proxy-headers --forwarded-allow-ips '*'