"""Actions decided on the server and executed by the phone. Transitions are conditional updates, so a
repeated or late report never changes an action that already settled."""
import uuid

from nota import db, events

PHONE_TYPES = ("create_calendar_event", "delete_calendar_event", "share_message")


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


def to_dict(row) -> dict:
    return {
        "id": str(row["id"]),
        "type": row["type"],
        "args": row["args"],
        "status": row["status"],
        "needs_confirmation": row["needs_confirmation"],
        "created_at": row["created_at"].isoformat(),
        "result": row["result"],
    }


def list_for(conn, user_id, status=None) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM actions WHERE user_id = %s AND (%s::text IS NULL OR status = %s) ORDER BY created_at, idempotency_key",
        (user_id, status, status),
    ).fetchall()
    return [to_dict(r) for r in rows]


def _get(conn, user_id, action_id) -> dict:
    try:
        uuid.UUID(str(action_id))
    except ValueError as e:
        raise NotFound() from e
    row = conn.execute("SELECT * FROM actions WHERE id = %s AND user_id = %s FOR UPDATE", (action_id, user_id)).fetchone()
    if not row:
        raise NotFound()
    return row


def _move(conn, user_id, action_id, from_status: str, to_status: str, result=None) -> dict:
    row = _get(conn, user_id, action_id)
    if row["status"] != from_status:
        return None
    row = conn.execute(
        "UPDATE actions SET status = %s, result = coalesce(%s, result) WHERE id = %s RETURNING *",
        (to_status, db.json(result) if result is not None else None, action_id),
    ).fetchone()
    action = to_dict(row)
    events.publish(conn, user_id, "action", action)
    return action


def report(conn, user_id, action_id, ok: bool, system_id=None, error=None) -> dict:
    """The first report wins; later ones return the settled action unchanged."""
    result = {"ok": ok, **({"system_id": system_id} if system_id else {}), **({"error": error} if error else {})}
    moved = _move(conn, user_id, action_id, "pending", "done" if ok else "failed", result)
    return moved or to_dict(_get(conn, user_id, action_id))


def confirm(conn, user_id, action_id) -> dict:
    moved = _move(conn, user_id, action_id, "pending_confirmation", "pending")
    if moved is None:
        raise Conflict("action is not waiting for confirmation")
    return moved


def reject(conn, user_id, action_id) -> dict:
    moved = _move(conn, user_id, action_id, "pending_confirmation", "rejected")
    if moved is None:
        raise Conflict("action is not waiting for confirmation")
    return moved


def undo(conn, user_id, action_id) -> dict:
    """A done calendar event gets one reverse action; undoing twice returns that same reverse action."""
    row = _get(conn, user_id, action_id)
    system_id = (row["result"] or {}).get("system_id")
    if row["type"] != "create_calendar_event" or row["status"] != "done" or not system_id:
        raise Conflict("only a calendar event the phone has created can be undone")
    created = conn.execute(
        """INSERT INTO actions (id, user_id, conversation_id, idempotency_key, type, args, status, needs_confirmation)
           VALUES (%s, %s, %s, %s, 'delete_calendar_event', %s, 'pending', false)
           ON CONFLICT (idempotency_key) DO NOTHING RETURNING *""",
        (uuid.uuid4(), user_id, row["conversation_id"], f"undo:{action_id}", db.json({"system_event_id": system_id})),
    ).fetchone()
    if created:
        events.publish(conn, user_id, "action", to_dict(created))
        return to_dict(created)
    return to_dict(conn.execute("SELECT * FROM actions WHERE idempotency_key = %s", (f"undo:{action_id}",)).fetchone())
