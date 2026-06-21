"""Business logic for the admin stats summary."""

from datetime import date

from app.repositories.admin_stats_repository import AdminStatsRepository
from app.schemas.admin import AdminStatsResponse


class AdminStatsService:
    def __init__(self, repo: AdminStatsRepository):
        self.repo = repo

    async def get_stats(self) -> AdminStatsResponse:
        today = date.today()
        return AdminStatsResponse(
            total_users=await self.repo.count_users(),
            total_videos=await self.repo.count_videos(),
            total_sessions=await self.repo.count_sessions(),
            total_vocabulary_words=await self.repo.count_vocabulary(),
            pending_transcriptions=await self.repo.count_transcriptions_with_status("pending"),
            failed_transcriptions=await self.repo.count_transcriptions_with_status("failed"),
            new_users_today=await self.repo.count_new_users(today),
            sessions_today=await self.repo.count_active_users(today),
        )