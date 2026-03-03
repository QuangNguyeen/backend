from datetime import datetime, timedelta, timezone
from dataclasses import dataclass


@dataclass
class SRSResult:
    repetitions: int
    ease_factor: float
    interval_days: int
    next_review_at: datetime


def calculate_next_review(
    quality: int,
    repetitions: int,
    ease_factor: float,
    interval_days: int,
) -> SRSResult:
    """SM-2 Spaced Repetition algorithm.

    Args:
        quality: User's self-assessment (0-5). 3+ means remembered.
        repetitions: Number of consecutive correct reviews.
        ease_factor: Current ease factor (starts at 2.5).
        interval_days: Current interval in days.

    Returns:
        SRSResult with updated SRS parameters and next review datetime.
    """
    quality = max(0, min(5, quality))

    if quality >= 3:
        # Remembered
        if repetitions == 0:
            interval_days = 1
        elif repetitions == 1:
            interval_days = 6
        else:
            interval_days = round(interval_days * ease_factor)
        repetitions += 1
    else:
        # Forgot — reset
        repetitions = 0
        interval_days = 1

    # Update ease factor (minimum 1.3)
    ease_factor = max(
        1.3,
        ease_factor + 0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02),
    )

    next_review_at = datetime.now(timezone.utc) + timedelta(days=interval_days)

    return SRSResult(
        repetitions=repetitions,
        ease_factor=round(ease_factor, 4),
        interval_days=interval_days,
        next_review_at=next_review_at,
    )
