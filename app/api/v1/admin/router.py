from fastapi import APIRouter

from app.api.v1.admin import analytics, stats, users, videos

router = APIRouter(prefix="/admin", tags=["Admin"])
router.include_router(stats.router)
router.include_router(analytics.router)
router.include_router(users.router)
router.include_router(videos.router)
