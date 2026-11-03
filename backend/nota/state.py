"""Read models shared by /api/state and the event stream."""
from nota import actions, memory, quota


def conversation(conn, row) -> dict:
    facts = conn.execute(
        "SELECT text, speaker_label, offset_seconds FROM facts WHERE conversation_id = %s ORDER BY offset_seconds, id", (row["id"],)
    ).fetchall()
    todos = conn.execute("SELECT * FROM todos WHERE conversation_id = %s ORDER BY created_at, id", (row["id"],)).fetchall()
    return {
        "id": str(row["id"]),
        "recording_id": str(row["recording_id"]),
        "started_at": row["started_at"].isoformat(),
        "ended_at": row["ended_at"].isoformat(),
        "title": row["title"],
        "overview": row["overview"],
        "speakers": row["speakers"],
        "facts": [dict(f) for f in facts],
        "todos": [todo(t) for t in todos],
    }


def todo(row) -> dict:
    return {
        "id": str(row["id"]),
        "conversation_id": str(row["conversation_id"]) if row["conversation_id"] else None,
        "text": row["text"],
        "due_at": row["due_at"].isoformat() if row["due_at"] else None,
        "done": row["done"],
    }


def full(conn, user) -> dict:
    convs = conn.execute(
        "SELECT * FROM conversations WHERE user_id = %s AND summarized ORDER BY started_at DESC LIMIT 100", (user["id"],)
    ).fetchall()
    todos = conn.execute("SELECT * FROM todos WHERE user_id = %s ORDER BY created_at DESC LIMIT 200", (user["id"],)).fetchall()
    enrolled = conn.execute("SELECT 1 FROM voiceprints WHERE user_id = %s", (user["id"],)).fetchone() is not None
    return {
        "user": {"id": user["id"], "plan": user["plan"], "timezone": user["timezone"]},
        "quota": quota.snapshot(conn, user),
        "conversations": [conversation(conn, c) for c in convs],
        "todos": [todo(t) for t in todos],
        "actions": actions.list_for(conn, user["id"]),
        "profile": memory.items(conn, user["id"]),
        "voiceprint_enrolled": enrolled,
    }
