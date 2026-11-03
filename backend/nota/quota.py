"""Three ledgers, each checked and charged in one statement (or under the user's row lock for Pro's
rolling windows), keyed by recording so retries, duplicates and requeues never charge twice.

- recordings_today: one per recording (press to stop), per calendar day in the user's timezone.
- minutes: speech seconds after VAD, per calendar month; Pro has rolling fair-use windows instead.
- asks: per calendar month.
"""
from datetime import timedelta
from zoneinfo import ZoneInfo

from nota import clock, config, events


def local_now(user):
    return clock.now().astimezone(ZoneInfo(user["timezone"]))


def today(user):
    return local_now(user).date()


def month(user):
    return local_now(user).strftime("%Y-%m")


def _free(user) -> bool:
    return user["plan"] == "free"


def take_recording_slot(conn, user, recording_id) -> bool:
    limit = config.FREE_RECORDINGS_PER_DAY if _free(user) else None
    day = today(user)
    taken = conn.execute(
        """INSERT INTO daily_usage (user_id, day, used) VALUES (%(u)s, %(d)s, 1)
           ON CONFLICT (user_id, day) DO UPDATE SET used = daily_usage.used + 1
           WHERE %(limit)s::int IS NULL OR daily_usage.used < %(limit)s::int""",
        {"u": user["id"], "d": day, "limit": limit},
    ).rowcount
    if taken:
        conn.execute(
            """INSERT INTO recording_charges (recording_id, user_id, day, status) VALUES (%s, %s, %s, 'reserved')
               ON CONFLICT (recording_id) DO UPDATE SET day = excluded.day""",
            (recording_id, user["id"], day),
        )
    return bool(taken)


def charge_minutes(conn, user, recording_id, seconds: float) -> bool:
    """Charges a recording's speech once. Returns False when the ledger has no room (AI pauses)."""
    charged = conn.execute("SELECT period FROM recording_charges WHERE recording_id = %s", (recording_id,)).fetchone()
    if charged and charged["period"]:
        return True
    period = month(user)
    if _free(user):
        ok = conn.execute(
            # The SELECT guard covers the month's first charge, which never reaches the conflict branch.
            """INSERT INTO monthly_usage (user_id, period, speech_seconds)
               SELECT %(u)s, %(p)s, %(s)s WHERE %(s)s <= %(limit)s
               ON CONFLICT (user_id, period) DO UPDATE SET speech_seconds = monthly_usage.speech_seconds + %(s)s
               WHERE monthly_usage.speech_seconds + %(s)s <= %(limit)s""",
            {"u": user["id"], "p": period, "s": seconds, "limit": config.FREE_MINUTES_PER_MONTH * 60},
        ).rowcount
    else:
        ok = _charge_pro(conn, user, period, seconds)
    if not ok:
        return False
    conn.execute(
        """INSERT INTO recording_charges (recording_id, user_id, period, speech_seconds, charged_at, status)
           VALUES (%(r)s, %(u)s, %(p)s, %(s)s, %(at)s, 'reserved')
           ON CONFLICT (recording_id) DO UPDATE SET period = excluded.period, speech_seconds = excluded.speech_seconds,
             charged_at = excluded.charged_at""",
        {"r": recording_id, "u": user["id"], "p": period, "s": seconds, "at": clock.now()},
    )
    return True


def _charge_pro(conn, user, period, seconds) -> bool:
    # Rolling windows are sums, not counters: the user's row lock serialises concurrent charges.
    conn.execute("SELECT 1 FROM users WHERE id = %s FOR UPDATE", (user["id"],))
    now = clock.now()
    for hours, minutes in config.PRO_FAIR_USE_MINUTES.items():
        used = conn.execute(
            """SELECT coalesce(sum(speech_seconds), 0) AS s FROM recording_charges
               WHERE user_id = %s AND status <> 'refunded' AND charged_at > %s""",
            (user["id"], now - timedelta(hours=hours)),
        ).fetchone()["s"]
        if used + seconds > minutes * 60:
            return False
    conn.execute(
        """INSERT INTO monthly_usage (user_id, period, speech_seconds) VALUES (%s, %s, %s)
           ON CONFLICT (user_id, period) DO UPDATE SET speech_seconds = monthly_usage.speech_seconds + excluded.speech_seconds""",
        (user["id"], period, seconds),
    )
    return True


def take_ask(conn, user) -> str | None:
    """Charges one ask; returns the period charged, or None when the month is used up."""
    period = month(user)
    limit = config.FREE_ASKS_PER_MONTH if _free(user) else None
    ok = conn.execute(
        """INSERT INTO monthly_usage (user_id, period, asks) VALUES (%(u)s, %(p)s, 1)
           ON CONFLICT (user_id, period) DO UPDATE SET asks = monthly_usage.asks + 1
           WHERE %(limit)s::int IS NULL OR monthly_usage.asks < %(limit)s::int""",
        {"u": user["id"], "p": period, "limit": limit},
    ).rowcount
    return period if ok else None


def refund_ask(conn, user_id, period):
    conn.execute("UPDATE monthly_usage SET asks = asks - 1 WHERE user_id = %s AND period = %s", (user_id, period))


def refund_recording(conn, recording_id):
    row = conn.execute(
        "SELECT * FROM recording_charges WHERE recording_id = %s AND status <> 'refunded' FOR UPDATE", (recording_id,)
    ).fetchone()
    if not row:
        return
    if row["day"]:
        conn.execute("UPDATE daily_usage SET used = used - 1 WHERE user_id = %s AND day = %s", (row["user_id"], row["day"]))
    if row["period"]:
        conn.execute(
            "UPDATE monthly_usage SET speech_seconds = speech_seconds - %s WHERE user_id = %s AND period = %s",
            (row["speech_seconds"], row["user_id"], row["period"]),
        )
    conn.execute("UPDATE recording_charges SET status = 'refunded' WHERE recording_id = %s", (recording_id,))


def settle(conn, recording_id):
    conn.execute("UPDATE recording_charges SET status = 'charged' WHERE recording_id = %s AND status = 'reserved'", (recording_id,))


def snapshot(conn, user) -> dict:
    day, period = today(user), month(user)
    daily = conn.execute("SELECT used FROM daily_usage WHERE user_id = %s AND day = %s", (user["id"], day)).fetchone()
    monthly = conn.execute("SELECT * FROM monthly_usage WHERE user_id = %s AND period = %s", (user["id"], period)).fetchone()
    free = _free(user)
    return {
        "minutes": {
            "used": round((monthly["speech_seconds"] if monthly else 0) / 60, 2),
            "limit": config.FREE_MINUTES_PER_MONTH if free else None,
            "period": period,
        },
        "recordings_today": {"used": daily["used"] if daily else 0, "limit": config.FREE_RECORDINGS_PER_DAY if free else None, "day": day.isoformat()},
        "asks": {"used": monthly["asks"] if monthly else 0, "limit": config.FREE_ASKS_PER_MONTH if free else None, "period": period},
    }


def publish(conn, user):
    events.publish(conn, user["id"], "quota", snapshot(conn, user))
