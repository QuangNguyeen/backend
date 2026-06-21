"""Database access for authentication (user lookup by email / google id)."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class AuthRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_email_ci(self, email: str) -> User | None:
        result = await self.db.execute(select(User).where(func.lower(User.email) == email))
        return result.scalar_one_or_none()

    async def get_by_google_id(self, google_id: str) -> User | None:
        result = await self.db.execute(select(User).where(User.google_id == google_id))
        return result.scalar_one_or_none()

    def add(self, instance) -> None:
        self.db.add(instance)

    async def commit(self) -> None:
        await self.db.commit()

    async def refresh(self, instance) -> None:
        await self.db.refresh(instance)