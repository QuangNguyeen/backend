from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.user import (
    ChangePasswordRequest,
    UserProfileResponse,
    UserUpdateRequest,
)
from app.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["Users"])


def get_user_service(db: AsyncSession = Depends(get_db)) -> UserService:
    return UserService(UserRepository(db))


@router.get("/me", response_model=UserProfileResponse)
async def get_my_profile(
    current_user: User = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
):
    """Return the current user merged with aggregated learning stats."""
    return await service.get_profile(current_user)


@router.put("/me", response_model=UserProfileResponse)
async def update_my_profile(
    body: UserUpdateRequest,
    current_user: User = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
):
    return await service.update_profile(current_user, body)


@router.post("/me/change-password")
async def change_password(
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    service: UserService = Depends(get_user_service),
):
    await service.change_password(current_user, body)
    return {"message": "Password changed successfully"}
