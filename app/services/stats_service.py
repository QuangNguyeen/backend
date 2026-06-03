from datetime import date, timedelta


def compute_streaks(active_dates: set[date], today: date) -> tuple[int, int]:
    """Return (current_streak, longest_streak) from a set of active dates.

    The current streak counts consecutive active days ending today (or yesterday,
    if the user hasn't practiced today yet).
    """
    if not active_dates:
        return 0, 0

    sorted_dates = sorted(active_dates)

    longest = 1
    run = 1
    for i in range(1, len(sorted_dates)):
        if sorted_dates[i] - sorted_dates[i - 1] == timedelta(days=1):
            run += 1
            longest = max(longest, run)
        else:
            run = 1

    current = 0
    d = today
    while d in active_dates:
        current += 1
        d -= timedelta(days=1)
    if current == 0:
        d = today - timedelta(days=1)
        while d in active_dates:
            current += 1
            d -= timedelta(days=1)

    return current, longest
