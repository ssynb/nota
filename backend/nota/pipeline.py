"""One job per recording, run again whenever audio arrives (Part A T7: transcribe while recording).

Each run takes the contiguous prefix of chunks that is not yet transcribed, VADs it, and transcribes
the utterances that are certainly complete; speech touching the end of what has arrived is left for
the next chunk (or finish), so a chunk boundary never cuts a sentence. Speech is charged per batch.
When 120 seconds of silence follow the last utterance, the open conversation closes and gets its
minutes, agent run and profile update right away, while the recording goes on.

Every result is a checkpoint written under the job lease (see jobs.Lease.owned): a retry or a
takeover resumes where the last owner stopped, and a superseded owner cannot write anything.
"""
import uuid

import numpy as np

from nota import (
    agent,
    config,
    conversations,
    db,
    events,
    jobs,
    memory,
    quota,
    speech,
    state,
)
from nota.recordings import read_wav

TAIL_HOLD = speech.MERGE_GAP  # speech ending this close to the end of the received audio may continue


def _publish_status(conn, rec, status, **extra):
    conn.execute("UPDATE recordings SET status = %s WHERE id = %s", (status, rec["id"]))
    events.publish(conn, rec["user_id"], "recording", {"recording_id": str(rec["id"]), "status": status, **extra})


def _load(lease):
    with db.tx() as conn:
        rec = conn.execute("SELECT * FROM recordings WHERE id = %s", (lease.recording_id,)).fetchone()
        user = conn.execute("SELECT id, plan, timezone FROM users WHERE id = %s", (rec["user_id"],)).fetchone()
    return rec, user


def _prefix(rec) -> tuple[list[dict], bool]:
    """Chunks 0..k-1 that have all arrived, with their start offsets; and whether that is the whole recording."""
    with db.tx() as conn:
        rows = conn.execute("SELECT seq, duration_seconds FROM chunks WHERE recording_id = %s ORDER BY seq", (rec["id"],)).fetchall()
    prefix, offset = [], 0.0
    for expected, row in enumerate(rows):
        if row["seq"] != expected:
            break
        prefix.append({"seq": row["seq"], "start": offset, "end": offset + row["duration_seconds"]})
        offset += row["duration_seconds"]
    complete = rec["total_chunks"] is not None and len(prefix) == rec["total_chunks"]
    return prefix, complete


def _audio(rec, chunks: list[dict]) -> np.ndarray:
    with db.tx() as conn:
        rows = conn.execute(
            "SELECT audio FROM chunks WHERE recording_id = %s AND seq = ANY(%s) ORDER BY seq", (rec["id"], [c["seq"] for c in chunks])
        ).fetchall()
    return speech.pcm_to_float(b"".join(read_wav(bytes(r["audio"])) for r in rows))


def _owner_voice(user):
    with db.tx() as conn:
        row = conn.execute("SELECT embedding FROM voiceprints WHERE user_id = %s", (user["id"],)).fetchone()
    return np.array(row["embedding"], dtype=np.float32) if row else None


def _pause(lease, rec, seconds):
    with db.tx() as conn:
        lease.owned(conn)
        conn.execute("UPDATE recordings SET ai = 'paused_quota', pause_reason = 'minutes', pending_seconds = %s WHERE id = %s", (seconds, rec["id"]))
        _publish_status(conn, rec, "paused_quota")
        jobs.done(conn, lease)


def _transcribe_new_audio(lease, rec, user) -> bool | None:
    """Transcribes the newly arrived prefix. Returns None if paused by quota, else whether the whole
    recording has now been transcribed."""
    prefix, complete = _prefix(rec)
    cursor = rec["cursor_seconds"]
    window = [c for c in prefix if c["end"] > cursor]
    window_end = prefix[-1]["end"] if prefix else 0.0
    if not window:
        if complete:  # everything was transcribed before finish arrived: close what is still open
            with db.tx() as conn:
                lease.owned(conn)
                _close_conversations(conn, rec, window_end, True)
        return complete
    with db.tx() as conn:
        lease.owned(conn)
        _publish_status(conn, rec, "transcribing")
    samples = _audio(rec, window)
    base = window[0]["start"]
    samples = samples[int((cursor - base) * speech.RATE) :]
    segments = [(cursor + a, cursor + b) for a, b in speech.vad(samples)]
    if complete:
        final, new_cursor = segments, window_end
    else:
        final = [s for s in segments if s[1] <= window_end - TAIL_HOLD]
        held = segments[len(final) :]
        new_cursor = held[0][0] if held else window_end
    seconds = speech.speech_seconds(final)
    with db.tx() as conn:
        room = quota.has_room(conn, user, seconds)
    if not room:  # do not spend transcription on audio that cannot be charged
        _pause(lease, rec, seconds)
        return None
    utterances = speech.transcribe_spans(samples, cursor, speech.utterances(final))
    with db.tx() as conn:
        lease.owned(conn)
        current = conn.execute("SELECT cursor_seconds, clusters FROM recordings WHERE id = %s", (rec["id"],)).fetchone()
        if not quota.charge_minutes(conn, user, rec["id"], seconds):
            raise _NoRoom(seconds)
        previous = conn.execute(
            "SELECT cluster FROM utterances WHERE recording_id = %s ORDER BY idx DESC LIMIT 1", (rec["id"],)
        ).fetchone()
        clusters_of, clusters = speech.assign_clusters([u["embedding"] for u in utterances], current["clusters"], previous["cluster"] if previous else None)
        count = conn.execute("SELECT count(*) AS n FROM utterances WHERE recording_id = %s", (rec["id"],)).fetchone()["n"]
        for i, (u, k) in enumerate(zip(utterances, clusters_of)):
            conn.execute(
                "INSERT INTO utterances (recording_id, idx, start_seconds, end_seconds, text, cluster) VALUES (%s, %s, %s, %s, %s, %s)",
                (rec["id"], count + i, u["start"], u["end"], u["text"], k),
            )
        conn.execute("UPDATE recordings SET cursor_seconds = %s, clusters = %s WHERE id = %s", (new_cursor, db.json(clusters), rec["id"]))
        _close_conversations(conn, rec, new_cursor, complete)
        quota.publish(conn, user)
    return complete


class _NoRoom(Exception):
    def __init__(self, seconds):
        self.seconds = seconds


def _close_conversations(conn, rec, silent_until: float, complete: bool):
    """Closes every open conversation followed by at least CONVERSATION_GAP_SECONDS of silence
    (or by the end of the recording); the utterances after it start a new one."""
    head = conn.execute("SELECT closed_utterances, clusters FROM recordings WHERE id = %s", (rec["id"],)).fetchone()
    rows = conn.execute(
        "SELECT * FROM utterances WHERE recording_id = %s AND idx >= %s ORDER BY idx", (rec["id"], head["closed_utterances"])
    ).fetchall()
    if not rows:
        return
    voice = conn.execute("SELECT embedding FROM voiceprints WHERE user_id = %s", (rec["user_id"],)).fetchone()
    names = speech.cluster_names(head["clusters"], np.array(voice["embedding"], dtype=np.float32) if voice else None)
    items = [{"start": r["start_seconds"], "end": r["end_seconds"], "text": r["text"], "speaker": names.get(r["cluster"], "A"), "is_owner": names.get(r["cluster"]) == "我"} for r in rows]
    groups = conversations.split(items)
    last_open = not complete and silent_until - groups[-1][-1]["end"] < config.CONVERSATION_GAP_SECONDS
    closing = groups[:-1] if last_open else groups
    closed = head["closed_utterances"]
    idx = conn.execute("SELECT count(*) AS n FROM conversations WHERE recording_id = %s", (rec["id"],)).fetchone()["n"]
    for group in closing:
        _insert_conversation(conn, rec, conversations.build(rec, idx, group))
        idx += 1
        closed += len(group)
    conn.execute("UPDATE recordings SET closed_utterances = %s WHERE id = %s", (closed, rec["id"]))


def _insert_conversation(conn, rec, conv):
    conn.execute(
        """INSERT INTO conversations (id, recording_id, idx, user_id, started_at, ended_at, speakers, lines)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (recording_id, idx) DO NOTHING""",
        (uuid.uuid4(), rec["id"], conv["idx"], rec["user_id"], conv["started_at"], conv["ended_at"], db.json(conv["speakers"]), db.json(conv["lines"])),
    )


def _pending_conversations(rec):
    with db.tx() as conn:
        return conn.execute(
            "SELECT * FROM conversations WHERE recording_id = %s AND NOT (summarized AND agent_done AND memory_done) ORDER BY idx", (rec["id"],)
        ).fetchall()


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


def _finish_conversations(lease, rec, user):
    for conv in _pending_conversations(rec):
        if not conv["summarized"]:
            with db.tx() as conn:
                lease.owned(conn)
                _publish_status(conn, rec, "summarizing")
            conv = _summarize(lease, rec, conv)
        if not conv["agent_done"]:
            _act(lease, rec, user, conv)
        if not conv["memory_done"]:
            _learn(lease, user, conv)


def process(lease: jobs.Lease):
    rec, user = _load(lease)
    if rec["kind"] == "text":
        with db.tx() as conn:
            lease.owned(conn)
            _insert_conversation(conn, rec, conversations.text_note(rec))
        complete = True
    else:
        try:
            complete = _transcribe_new_audio(lease, rec, user)
        except _NoRoom as e:  # lost a race for the last minutes between the check and the charge
            _pause(lease, rec, e.seconds)
            return
        if complete is None:
            return
    _finish_conversations(lease, rec, user)
    with db.tx() as conn:
        lease.owned(conn)
        if complete:
            quota.settle(conn, rec["id"])
            _publish_status(conn, rec, "done")
        else:
            _publish_status(conn, rec, "uploading")  # waiting for more audio
        jobs.done(conn, lease)


def fail(lease: jobs.Lease, error: str):
    """Retries with backoff; after the last attempt the recording fails and every ledger is refunded."""
    rec, user = _load(lease)
    with db.tx() as conn:
        lease.owned(conn)
        delay = jobs.retry_or_fail(conn, lease, error[:500])
        if delay is not None:  # tell the user what happened instead of a silent wait
            notice = f"{error[:200]}；{delay:.0f} 秒后重试（第 {lease.attempts + 1}/{config.JOB_MAX_ATTEMPTS} 次）"
            events.publish(conn, rec["user_id"], "recording", {"recording_id": str(rec["id"]), "status": rec["status"], "error": notice})
            return
        quota.refund_recording(conn, rec["id"])
        conn.execute("UPDATE recordings SET error = %s WHERE id = %s", (error[:500], rec["id"]))
        _publish_status(conn, rec, "failed", error=error[:300])
        quota.publish(conn, user)
