"""One job = one recording: assemble chunks → VAD → charge speech minutes → SenseVoice + CAM++ →
split at long silences → per conversation: minutes, agent, profile. Every step's result is a checkpoint
written under the job lease, so a retry or a takeover resumes where the last owner stopped."""
import uuid

import numpy as np

from nota import agent, conversations, db, events, jobs, memory, quota, speech, state
from nota.recordings import read_wav


def _publish_status(conn, rec, status, **extra):
    conn.execute("UPDATE recordings SET status = %s WHERE id = %s", (status, rec["id"]))
    events.publish(conn, rec["user_id"], "recording", {"recording_id": str(rec["id"]), "status": status, **extra})


def _load(lease):
    with db.tx() as conn:
        rec = conn.execute("SELECT * FROM recordings WHERE id = %s", (lease.recording_id,)).fetchone()
        user = conn.execute("SELECT id, plan, timezone FROM users WHERE id = %s", (rec["user_id"],)).fetchone()
    return rec, user


def _samples(rec) -> np.ndarray:
    with db.tx() as conn:
        rows = conn.execute("SELECT audio FROM chunks WHERE recording_id = %s ORDER BY seq", (rec["id"],)).fetchall()
    return speech.pcm_to_float(b"".join(read_wav(bytes(r["audio"])) for r in rows))


def _transcribe(lease, rec, user) -> bool:
    """Returns False when speech minutes are exhausted and AI work pauses."""
    with db.tx() as conn:
        lease.owned(conn)
        _publish_status(conn, rec, "transcribing")
    samples = _samples(rec)
    segments = rec["vad_segments"]
    if segments is None:
        segments = speech.vad(samples)
        with db.tx() as conn:
            lease.owned(conn)
            conn.execute(
                "UPDATE recordings SET vad_segments = %s, speech_seconds = %s WHERE id = %s",
                (db.json(segments), speech.speech_seconds(segments), rec["id"]),
            )
    with db.tx() as conn:
        lease.owned(conn)
        if not quota.charge_minutes(conn, user, rec["id"], speech.speech_seconds(segments)):
            conn.execute("UPDATE recordings SET ai = 'paused_quota', pause_reason = 'minutes' WHERE id = %s", (rec["id"],))
            _publish_status(conn, rec, "paused_quota")
            jobs.done(conn, lease)
            return False
        quota.publish(conn, user)
        voice = conn.execute("SELECT embedding FROM voiceprints WHERE user_id = %s", (user["id"],)).fetchone()
    owner = np.array(voice["embedding"], dtype=np.float32) if voice else None
    utterances = speech.transcribe_recording(samples, [tuple(s) for s in segments], owner)
    with db.tx() as conn:
        lease.owned(conn)
        conn.execute("UPDATE recordings SET segments = %s WHERE id = %s", (db.json(utterances), rec["id"]))
        for idx, group in enumerate(conversations.split(utterances)):
            _insert_conversation(conn, rec, conversations.build(rec, idx, group))
    return True


def _insert_conversation(conn, rec, conv):
    conn.execute(
        """INSERT INTO conversations (id, recording_id, idx, user_id, started_at, ended_at, speakers, lines)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (recording_id, idx) DO NOTHING""",
        (uuid.uuid4(), rec["id"], conv["idx"], rec["user_id"], conv["started_at"], conv["ended_at"], db.json(conv["speakers"]), db.json(conv["lines"])),
    )


def _conversations(rec):
    with db.tx() as conn:
        return conn.execute("SELECT * FROM conversations WHERE recording_id = %s ORDER BY idx", (rec["id"],)).fetchall()


def _summarize(lease, rec, conv):
    summary = conversations.summarize(conv)
    with db.tx() as conn:
        lease.owned(conn)
        conn.execute(
            "UPDATE conversations SET title = %s, overview = %s, summarized = true WHERE id = %s",
            (summary["title"], summary["overview"], conv["id"]),
        )
        for f in summary["facts"]:
            conn.execute(
                "INSERT INTO facts (conversation_id, text, speaker_label, offset_seconds) VALUES (%s, %s, %s, %s)",
                (conv["id"], f["text"], f["speaker_label"], f["offset_seconds"]),
            )
        row = conn.execute("SELECT * FROM conversations WHERE id = %s", (conv["id"],)).fetchone()
        events.publish(conn, rec["user_id"], "conversation", state.conversation(conn, row))
    return row


def _act(lease, rec, user, conv):
    def on_step(tool, args, result):
        with db.tx() as conn:
            events.publish(conn, user["id"], "step", {"recording_id": str(rec["id"]), "tool": tool, "args": args, "result": result})

    plan = agent.process(user, conv, conv["overview"], f"{rec['id']}:{conv['idx']}", on_step)
    with db.tx() as conn:
        lease.owned(conn)
        for action in agent.commit_plan(conn, user, conv["id"], plan):
            events.publish(conn, user["id"], "action", action)
        conn.execute("UPDATE conversations SET agent_done = true WHERE id = %s", (conv["id"],))
        row = conn.execute("SELECT * FROM conversations WHERE id = %s", (conv["id"],)).fetchone()
        events.publish(conn, user["id"], "conversation", state.conversation(conn, row))


def _learn(lease, user, conv):
    with db.tx() as conn:
        learned = memory.plan(conn, user["id"], conversations.render(conv))
    with db.tx() as conn:
        lease.owned(conn)
        memory.apply(conn, user["id"], learned)
        conn.execute("UPDATE conversations SET memory_done = true WHERE id = %s", (conv["id"],))


def process(lease: jobs.Lease):
    rec, user = _load(lease)
    if rec["kind"] == "audio" and rec["segments"] is None and not _transcribe(lease, rec, user):
        return
    if rec["kind"] == "text":
        with db.tx() as conn:
            lease.owned(conn)
            _insert_conversation(conn, rec, conversations.text_note(rec))
    with db.tx() as conn:
        lease.owned(conn)
        _publish_status(conn, rec, "summarizing")
    for conv in _conversations(rec):
        if not conv["summarized"]:
            conv = _summarize(lease, rec, conv)
        if not conv["agent_done"]:
            _act(lease, rec, user, conv)
        if not conv["memory_done"]:
            _learn(lease, user, conv)
    with db.tx() as conn:
        lease.owned(conn)
        quota.settle(conn, rec["id"])
        jobs.done(conn, lease)
        _publish_status(conn, rec, "done")


def fail(lease: jobs.Lease, error: str):
    """Retries with backoff; after the last attempt the recording fails and every ledger is refunded."""
    rec, user = _load(lease)
    with db.tx() as conn:
        lease.owned(conn)
        if jobs.retry_or_fail(conn, lease, error[:500]):
            quota.refund_recording(conn, rec["id"])
            conn.execute("UPDATE recordings SET error = %s WHERE id = %s", (error[:500], rec["id"]))
            _publish_status(conn, rec, "failed", error=error[:300])
            quota.publish(conn, user)
