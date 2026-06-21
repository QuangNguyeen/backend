"""Business logic for admin analytics dashboards."""

from datetime import date, datetime, timedelta

from app.repositories.admin_analytics_repository import AdminAnalyticsRepository
from app.schemas.admin import (
    AdminContentHealthResponse,
    AdminEngagementResponse,
    AdminRecentActivity,
    AdminRecentActivityResponse,
    AdminStudyHoursPoint,
    AdminStudyHoursResponse,
    AdminTopLearner,
    AdminTopLearnersResponse,
    AdminTrafficPoint,
    AdminTrafficResponse,
)

TIME_RANGE_DAYS = {"1d": 1, "7d": 7, "30d": 30, "90d": 90}


def _range_start(time_range: str) -> date:
    days = TIME_RANGE_DAYS.get(time_range, 7)
    return date.today() - timedelta(days=days - 1)


def _day_series(start: date, end: date) -> list[date]:
    days: list[date] = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


def _hour_series_for_today() -> list[int]:
    return list(range(datetime.now().hour + 1))


def _hour_point_date(day: date, hour: int) -> str:
    return f"{day.isoformat()}T{hour:02d}:00:00"


def _add_active_user_bucket(buckets, bucket, user_id) -> None:
    if bucket is None or not user_id:
        return
    buckets.setdefault(bucket, set()).add(str(user_id))


class AdminAnalyticsService:
    def __init__(self, repo: AdminAnalyticsRepository):
        self.repo = repo

    async def _active_users_by_hour(self, day: date) -> dict[int, int]:
        buckets: dict[int, set[str]] = {}
        for row in await self.repo.login_hours(day):
            _add_active_user_bucket(buckets, int(row.hour), row.id)
        for row in await self.repo.attempt_hours(day):
            _add_active_user_bucket(buckets, int(row.hour), row.user_id)
        return {hour: len(user_ids) for hour, user_ids in buckets.items()}

    async def _active_users_by_day(self, start: date) -> dict[date, int]:
        buckets: dict[date, set[str]] = {}
        for row in await self.repo.login_days(start):
            _add_active_user_bucket(buckets, row.day, row.id)
        for row in await self.repo.attempt_days(start):
            _add_active_user_bucket(buckets, row.day, row.user_id)
        return {day: len(user_ids) for day, user_ids in buckets.items()}

    async def get_traffic(self, time_range: str) -> AdminTrafficResponse:
        start = _range_start(time_range)
        today = date.today()

        if time_range == "1d":
            active_by_hour = await self._active_users_by_hour(today)
            new_rows = await self.repo.new_users_by_hour(today)
            new_by_hour = {
                int(row.hour): int(row.new_users or 0) for row in new_rows if row.hour is not None
            }
            points = [
                AdminTrafficPoint(
                    date=_hour_point_date(today, hour),
                    active_users=active_by_hour.get(hour, 0),
                    new_users=new_by_hour.get(hour, 0),
                )
                for hour in _hour_series_for_today()
            ]
            return AdminTrafficResponse(points=points, time_range=time_range)

        active_by_day = await self._active_users_by_day(start)
        new_rows = await self.repo.new_users_by_day(start)
        new_by_day = {row.day: int(row.new_users or 0) for row in new_rows if row.day}
        points = [
            AdminTrafficPoint(
                date=day.isoformat(),
                active_users=active_by_day.get(day, 0),
                new_users=new_by_day.get(day, 0),
            )
            for day in _day_series(start, today)
        ]
        return AdminTrafficResponse(points=points, time_range=time_range)

    async def get_study_hours(self, time_range: str) -> AdminStudyHoursResponse:
        start = _range_start(time_range)
        today = date.today()

        if time_range == "1d":
            rows = await self.repo.study_seconds_by_hour(today)
            by_hour = {
                int(row.hour): {
                    "total_seconds": float(row.total_seconds or 0),
                    "active_users": int(row.active_users or 0),
                }
                for row in rows
                if row.hour is not None
            }
            points: list[AdminStudyHoursPoint] = []
            for hour in _hour_series_for_today():
                item = by_hour.get(hour, {"total_seconds": 0.0, "active_users": 0})
                total_minutes = round(item["total_seconds"] / 60, 1)
                avg_minutes = (
                    round(total_minutes / item["active_users"], 1)
                    if item["active_users"]
                    else 0.0
                )
                points.append(
                    AdminStudyHoursPoint(
                        date=_hour_point_date(today, hour),
                        total_minutes=total_minutes,
                        avg_minutes_per_user=avg_minutes,
                    )
                )
            return AdminStudyHoursResponse(points=points, time_range=time_range)

        rows = await self.repo.study_seconds_by_day(start)
        by_day = {
            row.day: {
                "total_seconds": float(row.total_seconds or 0),
                "active_users": int(row.active_users or 0),
            }
            for row in rows
            if row.day
        }
        points = []
        for day in _day_series(start, today):
            item = by_day.get(day, {"total_seconds": 0.0, "active_users": 0})
            total_minutes = round(item["total_seconds"] / 60, 1)
            avg_minutes = (
                round(total_minutes / item["active_users"], 1) if item["active_users"] else 0.0
            )
            points.append(
                AdminStudyHoursPoint(
                    date=day.isoformat(),
                    total_minutes=total_minutes,
                    avg_minutes_per_user=avg_minutes,
                )
            )
        return AdminStudyHoursResponse(points=points, time_range=time_range)

    async def get_top_learners(self, time_range: str, limit: int) -> AdminTopLearnersResponse:
        start = _range_start(time_range)
        rows = await self.repo.top_learners(start, limit)
        learners = [
            AdminTopLearner(
                user_id=row.id,
                display_name=row.display_name,
                email=row.email,
                study_minutes=round(float(row.study_seconds or 0) / 60, 1),
                sessions=int(row.sessions or 0),
                avg_accuracy=round(float(row.avg_accuracy or 0) * 100, 1),
                streak=int(row.streak_days or 0),
            )
            for row in rows
        ]
        return AdminTopLearnersResponse(learners=learners, time_range=time_range)

    async def get_content_health(self) -> AdminContentHealthResponse:
        status_rows = await self.repo.video_status_counts()
        status_counts = {status or "ready": int(count or 0) for status, count in status_rows}

        level_rows = await self.repo.video_level_counts()
        levels = {level: int(count or 0) for level, count in level_rows if level}

        total_videos = await self.repo.total_videos()
        curated = await self.repo.curated_videos()

        return AdminContentHealthResponse(
            total_videos=int(total_videos),
            ready=status_counts.get("ready", 0),
            pending=status_counts.get("pending", 0),
            processing=status_counts.get("processing", 0),
            failed=status_counts.get("failed", 0),
            curated=int(curated),
            levels=levels,
        )

    async def get_engagement(self, time_range: str) -> AdminEngagementResponse:
        start = _range_start(time_range)

        total_attempts = await self.repo.count_attempts_since(start)
        completed_attempts = await self.repo.count_completed_since(start)
        total_duration = await self.repo.sum_completed_duration_since(start)
        active_users = await self.repo.count_active_users_since(start)
        repeat_users = await self.repo.count_repeat_users_since(start)
        saved_words = await self.repo.count_saved_words_since(start)

        completion_rate = (completed_attempts / total_attempts * 100) if total_attempts else 0.0
        avg_session_duration = (
            (float(total_duration) / 60 / completed_attempts) if completed_attempts else 0.0
        )
        repeat_rate = (repeat_users / active_users * 100) if active_users else 0.0
        vocab_save_rate = (saved_words / completed_attempts) if completed_attempts else 0.0

        return AdminEngagementResponse(
            completion_rate=round(completion_rate, 1),
            avg_session_duration=round(avg_session_duration, 1),
            repeat_rate=round(repeat_rate, 1),
            vocab_save_rate=round(vocab_save_rate, 1),
        )

    async def get_recent_activity(self, limit: int) -> AdminRecentActivityResponse:
        activities: list[AdminRecentActivity] = []

        activities.extend(
            AdminRecentActivity(
                id=f"user_signup:{row.id}",
                type="user_signup",
                description=f"{row.display_name} joined DictaLearn",
                user_name=row.display_name,
                timestamp=row.created_at,
            )
            for row in await self.repo.recent_signups(limit)
        )

        activities.extend(
            AdminRecentActivity(
                id=f"user_login:{row.id}:{row.last_login_at.isoformat()}",
                type="user_login",
                description=f"{row.display_name} signed in",
                user_name=row.display_name,
                timestamp=row.last_login_at,
            )
            for row in await self.repo.recent_logins(limit)
            if row.last_login_at is not None
        )

        activities.extend(
            AdminRecentActivity(
                id=f"session_completed:{row.id}",
                type="session_completed",
                description=(
                    f"{row.display_name} completed {row.title}"
                    + (
                        f" with {round(float(row.score) * 100, 1)}%"
                        if row.score is not None
                        else ""
                    )
                ),
                user_name=row.display_name,
                timestamp=row.updated_at,
            )
            for row in await self.repo.recent_sessions(limit)
        )

        activities.extend(
            AdminRecentActivity(
                id=f"video_added:{row.id}",
                type="video_added",
                description=f"Video added: {row.title}",
                user_name=row.display_name,
                timestamp=row.created_at,
            )
            for row in await self.repo.recent_videos(limit)
        )

        activities.extend(
            AdminRecentActivity(
                id=f"transcription_failed:{row.id}",
                type="transcription_failed",
                description=(
                    f"Transcription failed for {row.title}"
                    + (f": {row.transcription_error[:90]}" if row.transcription_error else "")
                ),
                user_name=None,
                timestamp=row.created_at,
            )
            for row in await self.repo.recent_failed_transcriptions(limit)
        )

        activities.sort(key=lambda item: item.timestamp, reverse=True)
        return AdminRecentActivityResponse(activities=activities[:limit])