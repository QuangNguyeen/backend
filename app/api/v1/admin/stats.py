from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_admin_user
from app.database import get_db
from app.models.user import User
from app.repositories.admin_stats_repository import AdminStatsRepository
from app.schemas.admin import AdminStatsResponse
from app.services.admin_stats_service import AdminStatsService

router = APIRouter(prefix="/stats")


def get_admin_stats_service(db: AsyncSession = Depends(get_db)) -> AdminStatsService:
    return AdminStatsService(AdminStatsRepository(db))


@router.get("", response_model=AdminStatsResponse)
async def get_admin_stats(
    _current_admin: User = Depends(get_admin_user),
    service: AdminStatsService = Depends(get_admin_stats_service),
):
    return await service.get_stats()