from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_admin_user
from app.database import get_db
from app.models.user import User
from app.repositories.admin_user_repository import AdminUserRepository
from app.schemas.admin import (
    AdminPatchUserRequest,
    AdminUserDetailResponse,
    AdminUserListResponse,
)
from app.services.admin_user_service import AdminUserService

router = APIRouter(prefix="/users")


def get_admin_user_service(db: AsyncSession = Depends(get_db)) -> AdminUserService:
    return AdminUserService(AdminUserRepository(db))


@router.get("", response_model=AdminUserListResponse)
async def list_admin_users(
    is_admin: bool | None = None,
    is_active: bool | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _current_admin: User = Depends(get_admin_user),
    service: AdminUserService = Depends(get_admin_user_service),
):
    return await service.list_users(
        is_admin=is_admin,
        is_active=is_active,
        search=search,
        page=page,
        page_size=page_size,
    )


@router.get("/{user_id}", response_model=AdminUserDetailResponse)
async def get_admin_user_detail(
    user_id: str,
    _current_admin: User = Depends(get_admin_user),
    service: AdminUserService = Depends(get_admin_user_service),
):
    return await service.get_user_detail(user_id)


@router.patch("/{user_id}", response_model=AdminUserDetailResponse)
async def patch_admin_user(
    user_id: str,
    body: AdminPatchUserRequest,
    current_admin: User = Depends(get_admin_user),
    service: AdminUserService = Depends(get_admin_user_service),
):
    return await service.patch_user(current_admin, user_id, body)