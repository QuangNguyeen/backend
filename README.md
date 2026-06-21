# DictaLearn Backend

FastAPI backend for DictaLearn — a YouTube-based dictation, cloze, and
sentence-reorder learning platform with SRS vocabulary, multiplayer rooms, and
an admin analytics dashboard.

## Stack

- **API:** FastAPI + async SQLAlchemy 2.0 (asyncpg) on PostgreSQL
- **Background jobs:** Celery + Redis (transcription, vocabulary enrichment)
- **Realtime:** WebSockets (rooms) + Redis pub/sub (SSE video events)
- **Auth:** JWT (access/refresh) + Google OAuth
- **External:** AssemblyAI / Google STT, Gemini, Google Translate, YouTube, spaCy

## Architecture

Layered, with thin routers:

```
app/api/v1/*        Routers — HTTP/WS only: validate, call a service, return a schema
app/services/*      Services — business logic; coordinate repos, providers, Celery
app/repositories/*  Repositories — all database access (queries only)
app/schemas/*       Pydantic request/response DTOs
app/models/*        SQLAlchemy ORM entities
app/core/*          Config, security, logging, error handling, middleware
app/tasks/*         Celery tasks
```

Each router exposes a `get_<feature>_service` dependency that wires
`Service(Repository(db))`. Domain errors (`app/core/exceptions.py`) map to HTTP
status codes; unhandled errors return a sanitized 500 with an `X-Request-ID`.

## Prerequisites

- Python 3.11+
- PostgreSQL 16 and Redis 7 (or use Docker Compose, below)
- `ffmpeg` and `yt-dlp` on PATH (audio extraction for STT)

## Local setup (venv)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # runtime deps (pinned)
pip install -e ".[dev]"                   # + pytest/ruff for development
python -m spacy download en_core_web_sm   # NLP model

cp .env.example .env                       # then fill in API keys / secrets
```

`requirements.txt` is the single source of truth for runtime dependencies;
`pyproject.toml` reads it at build time, so the two never drift.

### Configuration

All config is environment-driven via `app/config.py` (see `.env.example`). Key
variables: `ENVIRONMENT`, `DEBUG`, `DATABASE_URL`, `REDIS_URL`, `SECRET_KEY`,
`CORS_ORIGINS` (JSON list or comma-separated), and provider keys
(`ASSEMBLYAI_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_CLIENT_ID`, …).

In production (`ENVIRONMENT=production`) the app **fails fast** if `SECRET_KEY`
is left at its insecure default, `DEBUG` defaults off, and `/docs` is hidden.

## Run

```bash
# 1. Migrate the database
alembic upgrade head

# 2. API (http://localhost:8000, docs at /docs)
uvicorn app.main:app --reload

# 3. Celery worker (separate terminal)
#    On macOS the worker auto-selects the non-forking "solo" pool; Linux uses prefork.
celery -A app.celery_app.celery worker --loglevel=info
```

Health probes: `GET /health` (liveness) and `GET /health/ready` (DB + Redis).

## Run with Docker Compose (local)

Brings up Postgres, Redis, the API, and a Celery worker:

```bash
cp .env.example .env          # fill in API keys
docker compose up --build     # API on http://localhost:8000
```

The web container runs `alembic upgrade head` on start; the worker skips
migrations to avoid a race. `DATABASE_URL`/`REDIS_URL` are overridden to the
compose service hostnames, so your `.env` (localhost) needs no changes.

## Tests & lint

```bash
pytest -q          # unit tests (no DB / network — providers are faked)
ruff check app tests
```

## Migrations

```bash
alembic revision --autogenerate -m "describe change"   # create
alembic upgrade head                                    # apply
alembic downgrade -1                                    # roll back one
```

## Deployment

Production runs from `docker-compose.prod.yml` (Caddy TLS, GHCR images,
Postgres, Redis, observability). Copy `.env.production.example` → `.env` on the
server and supply real secrets; mount the GCP credentials JSON and `cookies.txt`
as files rather than baking them into the image. See `DEPLOY_PLAN*.md`.

## Project layout

```
app/            application code (see Architecture above)
alembic/        migration environment + versions
tests/          unit tests (fake repositories, mocked providers)
scripts/        operational/dev scripts
Dockerfile      single image used for web + worker
docker-compose.yml         local dev stack
docker-compose.prod.yml    production stack
```