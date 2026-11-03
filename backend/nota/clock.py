"""Business wall clock: quota days and months, rolling usage windows, "now" given to the agent.
Leases and job scheduling use Postgres now() instead, so every process agrees on them."""
from datetime import UTC, datetime


def now() -> datetime:
    return datetime.now(UTC)
