FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# System deps: ffmpeg (yt-dlp audio extraction), build/pg headers, curl
# (healthcheck), unzip (Deno installer).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg build-essential libpq-dev curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Deno — JS runtime yt-dlp needs to solve YouTube's JS challenges and expose all
# formats. Without it yt-dlp warns "No supported JavaScript runtime" and fails
# with "Requested format is not available". Installed system-wide for appuser.
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && deno --version

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt \
    && python -m spacy download en_core_web_sm

# App code
COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini .
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Non-root user
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
ENTRYPOINT ["docker-entrypoint.sh"]
