"""Receiving recordings: start (takes the day's slot), idempotent chunks, finish (reports gaps, enqueues),
text notes, and resuming recordings whose AI work was paused by quota."""
import io
import uuid
import wave
from datetime import datetime

from nota import events, jobs, quota

RATE = 16000
MAX_CHUNK_SECONDS = 60
TEXT_NAMESPACE = uuid.UUID("6f1f1c4e-9d0b-4a8e-9b1e-3f5a2c7d8e90")


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class BadAudio(ValueError):
    pass


def read_wav(data: bytes, max_seconds: float | None = MAX_CHUNK_SECONDS) -> bytes:
    """Validates the contract format (16 kHz, mono, 16-bit PCM) and returns the raw PCM frames."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getcomptype()) != (RATE, 1, 2, "NONE"):
                raise BadAudio("audio must be WAV, 16 kHz, mono, 16-bit PCM")
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as e:
        raise BadAudio(f"not a WAV file: {e}") from e
    if max_seconds is not None and len(frames) / 2 / RATE > max_seconds:
        raise BadAudio(f"chunk longer than {max_seconds} seconds")
    return frames


def _status(conn, user_id, recording_id, status, **extra):
    conn.execute("UPDATE recordings SET status = %s WHERE id = %s", (status, recording_id))
    events.publish(conn, user_id, "recording", {"recording_id": str(recording_id), "status": status, **extra})


def _create(conn, user, recording_id, kind, started_at, text=None):
    """Creates the recording once; the first creation takes the day's slot or pauses AI. Returns ai."""
    inserted = conn.execute(
        """INSERT INTO recordings (id, user_id, kind, text, started_at, status, ai)
           VALUES (%s, %s, %s, %s, %s, 'recording', 'enabled') ON CONFLICT (id) DO NOTHING RETURNING id""",
        (recording_id, user["id"], kind, text, started_at),
    ).fetchone()
    if not inserted:
        row = conn.execute("SELECT user_id, ai FROM recordings WHERE id = %s", (recording_id,)).fetchone()
        if row["user_id"] != user["id"]:
            raise Conflict("recording_id belongs to another user")
        return row["ai"]
    if quota.take_recording_slot(conn, user, recording_id):
        events.publish(conn, user["id"], "recording", {"recording_id": str(recording_id), "status": "recording"})
        quota.publish(conn, user)
        return "enabled"
    conn.execute("UPDATE recordings SET ai = 'paused_quota', pause_reason = 'recordings_today' WHERE id = %s", (recording_id,))
    _status(conn, user["id"], recording_id, "paused_quota")
    return "paused_quota"


def start(conn, user, recording_id: uuid.UUID, started_at: datetime) -> str:
    return _create(conn, user, recording_id, "audio", started_at)


def _owned(conn, user, recording_id):
    row = conn.execute("SELECT * FROM recordings WHERE id = %s AND user_id = %s", (recording_id, user["id"])).fetchone()
    if not row:
        raise NotFound()
    return row


def put_chunk(conn, user, recording_id, seq: int, data: bytes) -> bool:
    """Stores the chunk unless it is already there. Returns True for a duplicate."""
    rec = _owned(conn, user, recording_id)
    read_wav(data)
    inserted = conn.execute(
        "INSERT INTO chunks (recording_id, seq, audio) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (recording_id, seq, data)
    ).rowcount
    if inserted and rec["status"] == "recording":
        _status(conn, user["id"], recording_id, "uploading")
    return not inserted


def missing(conn, recording_id, total) -> list[int]:
    rows = conn.execute(
        """SELECT s FROM generate_series(0, %s - 1) s
           WHERE NOT EXISTS (SELECT 1 FROM chunks WHERE recording_id = %s AND seq = s) ORDER BY s""",
        (total, recording_id),
    ).fetchall()
    return [r["s"] for r in rows]


def finish(conn, user, recording_id, total: int) -> list[int]:
    rec = _owned(conn, user, recording_id)
    if rec["total_chunks"] is None:
        conn.execute("UPDATE recordings SET total_chunks = %s WHERE id = %s", (total, recording_id))
        rec["total_chunks"] = total
    gaps = missing(conn, recording_id, rec["total_chunks"])
    if not gaps and rec["ai"] == "enabled":
        jobs.enqueue(conn, recording_id)
    return gaps


def text_note(conn, user, idem_key: str, text: str, now: datetime) -> uuid.UUID:
    recording_id = uuid.uuid5(TEXT_NAMESPACE, f"{user['id']}:{idem_key}")
    if _create(conn, user, recording_id, "text", now, text) == "enabled":
        jobs.enqueue(conn, recording_id)
    return recording_id


def resume_paused(conn, user_id=None) -> int:
    """Gives paused recordings another go at the ledger that paused them; resumed ones are queued."""
    rows = conn.execute(
        """SELECT r.id, r.pause_reason, r.kind, r.total_chunks, r.speech_seconds, u.id AS uid, u.plan, u.timezone
           FROM recordings r JOIN users u ON u.id = r.user_id
           WHERE r.ai = 'paused_quota' AND (%s::text IS NULL OR r.user_id = %s) ORDER BY r.created_at
           FOR UPDATE OF r SKIP LOCKED""",
        (user_id, user_id),
    ).fetchall()
    resumed = 0
    for r in rows:
        user = {"id": r["uid"], "plan": r["plan"], "timezone": r["timezone"]}
        if r["pause_reason"] == "recordings_today":
            ok = quota.take_recording_slot(conn, user, r["id"])
        else:
            ok = quota.charge_minutes(conn, user, r["id"], r["speech_seconds"])
        if not ok:
            continue
        conn.execute("UPDATE recordings SET ai = 'enabled', pause_reason = NULL WHERE id = %s", (r["id"],))
        ready = r["kind"] == "text" or (r["total_chunks"] is not None and not missing(conn, r["id"], r["total_chunks"]))
        _status(conn, user["id"], r["id"], "uploading" if r["kind"] == "audio" else "recording")
        if ready:
            jobs.enqueue(conn, r["id"])
        quota.publish(conn, user)
        resumed += 1
    return resumed
