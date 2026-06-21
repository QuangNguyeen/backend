import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.config import get_settings
from app.core.error_handlers import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestIdMiddleware
from app.database import check_database
from app.events import close_publisher, init_publisher

logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info(
        "Starting %s (env=%s, debug=%s)",
        settings.APP_NAME,
        settings.ENVIRONMENT,
        settings.DEBUG,
    )
    await init_publisher()
    yield
    await close_publisher()
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)

    # Hide interactive docs in production.
    docs_enabled = not settings.is_production
    app = FastAPI(
        title=settings.APP_NAME,
        version="0.1.0",
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        lifespan=lifespan,
    )

    # Correlation id (set before CORS so all responses carry the header).
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    # Routers
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    @app.get("/health", tags=["health"])
    async def health_check():
        """Liveness: process is up. Cheap and dependency-free."""
        return {"status": "ok", "app": settings.APP_NAME}

    @app.get("/health/ready", tags=["health"])
    async def readiness_check():
        """Readiness: verifies DB and Redis connectivity."""
        checks: dict[str, str] = {}
        healthy = True

        try:
            await check_database()
            checks["database"] = "ok"
        except Exception as exc:
            healthy = False
            checks["database"] = "error"
            logger.error("Readiness DB check failed: %s", exc)

        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(settings.REDIS_URL)
            try:
                await client.ping()
                checks["redis"] = "ok"
            finally:
                await client.aclose()
        except Exception as exc:
            healthy = False
            checks["redis"] = "error"
            logger.error("Readiness Redis check failed: %s", exc)

        status_code = 200 if healthy else 503
        return JSONResponse(
            status_code=status_code,
            content={"status": "ready" if healthy else "degraded", "checks": checks},
        )

    return app


app = create_app()