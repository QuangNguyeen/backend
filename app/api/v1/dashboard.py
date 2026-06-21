import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.repositories.dashboard_repository import DashboardRepository
from app.schemas.dictation import DashboardFullResponse, HistoryEntryResponse
from app.services.dashboard_service import DashboardService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


def get_dashboard_service(db: AsyncSession = Depends(get_db)) -> DashboardService:
    return DashboardService(DashboardRepository(db))


@router.get("/full", response_model=DashboardFullResponse)
async def get_dashboard(
    current_user: User = Depends(get_current_user),
    service: DashboardService = Depends(get_dashboard_service),
):
    """Single endpoint returning all dashboard data: stats, heatmap, accuracy trend."""
    return await service.get_dashboard(current_user)


@router.get("/weak-words")
async def get_weak_words(
    limit: int = 20,
    current_user: User = Depends(get_current_user),
    service: DashboardService = Depends(get_dashboard_service),
):
    """Aggregate error_summary across completed sessions to find the user's weakest words."""
    return await service.get_weak_words(current_user, limit)


@router.get("/history", response_model=list[HistoryEntryResponse])
async def get_history(
    limit: int = 20,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    service: DashboardService = Depends(get_dashboard_service),
):
    """Recent sessions (both completed and in-progress), with video metadata."""
    return await service.get_history(current_user, limit, offset)