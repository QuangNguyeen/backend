from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_admin_user
from app.database import get_db
from app.models.user import User
from app.repositories.admin_analytics_repository import AdminAnalyticsRepository
from app.schemas.admin import (
    AdminContentHealthResponse,
    AdminEngagementResponse,
    AdminRecentActivityResponse,
    AdminStudyHoursResponse,
    AdminTopLearnersResponse,
    AdminTrafficResponse,
)
from app.services.admin_analytics_service import AdminAnalyticsService

router = APIRouter(prefix="/analytics")


def get_admin_analytics_service(db: AsyncSession = Depends(get_db)) -> AdminAnalyticsService:
    return AdminAnalyticsService(AdminAnalyticsRepository(db))


@router.get("/traffic", response_model=AdminTrafficResponse)
async def get_admin_traffic(
    time_range: str = Query("7d", pattern="^(1d|7d|30d|90d)$"),
    _current_admin: User = Depends(get_admin_user),
    service: AdminAnalyticsService = Depends(get_admin_analytics_service),
):
    return await service.get_traffic(time_range)


@router.get("/study-hours", response_model=AdminStudyHoursResponse)
async def get_admin_study_hours(
    time_range: str = Query("7d", pattern="^(1d|7d|30d|90d)$"),
    _current_admin: User = Depends(get_admin_user),
    service: AdminAnalyticsService = Depends(get_admin_analytics_service),
):
    return await service.get_study_hours(time_range)


@router.get("/top-learners", response_model=AdminTopLearnersResponse)
async def get_admin_top_learners(
    time_range: str = Query("7d", pattern="^(1d|7d|30d|90d)$"),
    limit: int = Query(10, ge=1, le=50),
    _current_admin: User = Depends(get_admin_user),
    service: AdminAnalyticsService = Depends(get_admin_analytics_service),
):
    return await service.get_top_learners(time_range, limit)


@router.get("/content-health", response_model=AdminContentHealthResponse)
async def get_admin_content_health(
    _current_admin: User = Depends(get_admin_user),
    service: AdminAnalyticsService = Depends(get_admin_analytics_service),
):
    return await service.get_content_health()


@router.get("/engagement", response_model=AdminEngagementResponse)
async def get_admin_engagement(
    time_range: str = Query("7d", pattern="^(1d|7d|30d|90d)$"),
    _current_admin: User = Depends(get_admin_user),
    service: AdminAnalyticsService = Depends(get_admin_analytics_service),
):
    return await service.get_engagement(time_range)


@router.get("/recent-activity", response_model=AdminRecentActivityResponse)
async def get_admin_recent_activity(
    limit: int = Query(20, ge=1, le=100),
    _current_admin: User = Depends(get_admin_user),
    service: AdminAnalyticsService = Depends(get_admin_analytics_service),
):
    return await service.get_recent_activity(limit)