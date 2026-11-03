"""HTTP API (docs/contract.md) and the static web page. Route handlers only translate HTTP; the rules
live in the domain modules."""
import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from nota import (
    actions,
    ask,
    clock,
    config,
    db,
    events,
    memory,
    recordings,
    speech,
    state,
)

MAX_ID = 64
MAX_TEXT = 4000
MAX_VOICEPRINT_SECONDS = 120
KEEPALIVE_SECONDS = 15
hub = events.Hub()


@asynccontextmanager
async def lifespan(_):
    db.ensure_schema()
    hub.start()
    yield


app = FastAPI(title="Nota", lifespan=lifespan)


def current_user(x_user_id: str = Header(""), x_timezone: str | None = Header(None)) -> dict:
    """Demo identity: a client-generated id. The timezone is fixed when the user is first seen."""
    if not x_user_id or len(x_user_id) > MAX_ID:
        raise HTTPException(400, "missing or invalid X-User-Id")
    if x_timezone:
        try:
            ZoneInfo(x_timezone)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise HTTPException(400, f"unknown timezone {x_timezone}") from e
    with db.tx() as conn:
        if x_timezone:
            conn.execute("INSERT INTO users (id, timezone) VALUES (%s, %s) ON CONFLICT DO NOTHING", (x_user_id, x_timezone))
        user = conn.execute("SELECT id, plan, timezone FROM users WHERE id = %s", (x_user_id,)).fetchone()
    if not user:
        raise HTTPException(400, "first request must carry X-Timezone")
    return user


User = Depends(current_user)


def sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


# ---------- recordings ----------
class StartRecording(BaseModel):
    recording_id: uuid.UUID
    started_at: datetime


@app.post("/api/recordings")
def start_recording(body: StartRecording, user=User):
    if body.started_at.tzinfo is None:
        raise HTTPException(400, "started_at must carry a timezone offset")
    with db.tx() as conn:
        try:
            ai = recordings.start(conn, user, body.recording_id, body.started_at)
        except recordings.Conflict as e:
            raise HTTPException(409, str(e)) from e
    return {"recording_id": str(body.recording_id), "ai": ai}


@app.put("/api/recordings/{recording_id}/chunks/{seq}")
async def put_chunk(recording_id: uuid.UUID, seq: int, request: Request, user=User):
    data = await request.body()
    if seq < 0:
        raise HTTPException(400, "seq must be >= 0")

    def store():
        with db.tx() as conn:
            return recordings.put_chunk(conn, user, recording_id, seq, data)

    try:
        duplicate = await asyncio.to_thread(store)
    except recordings.NotFound as e:
        raise HTTPException(404, "recording not found") from e
    except recordings.BadAudio as e:
        raise HTTPException(400, str(e)) from e
    return {"seq": seq, "duplicate": duplicate}


class Finish(BaseModel):
    total_chunks: int = Field(ge=0)


@app.post("/api/recordings/{recording_id}/finish")
def finish(recording_id: uuid.UUID, body: Finish, user=User):
    with db.tx() as conn:
        try:
            return {"missing": recordings.finish(conn, user, recording_id, body.total_chunks)}
        except recordings.NotFound as e:
            raise HTTPException(404, "recording not found") from e


class TextNote(BaseModel):
    idem_key: str = Field(min_length=1, max_length=MAX_ID)
    text: str = Field(min_length=1, max_length=MAX_TEXT)


@app.post("/api/notes/text")
def text_note(body: TextNote, user=User):
    with db.tx() as conn:
        rid = recordings.text_note(conn, user, body.idem_key, body.text.strip(), clock.now())
    return {"recording_id": str(rid)}


# ---------- ask ----------
class Ask(BaseModel):
    idem_key: str = Field(min_length=1, max_length=MAX_ID)
    question: str = Field(min_length=1, max_length=MAX_TEXT)


@app.post("/api/ask")
def ask_question(body: Ask, user=User):
    r = ask.reserve(user, body.idem_key)
    if r["kind"] == "quota":
        return JSONResponse({"error": "ask_quota"}, 429)
    if r["kind"] == "busy":
        return JSONResponse({"error": "in_progress"}, 409)
    stream = ask.replay(r["result"]) if r["kind"] == "replay" else ask.run(user, body.idem_key, r["owner"], body.question.strip())
    return StreamingResponse((sse(e, d) for e, d in stream), media_type="text/event-stream", headers={"cache-control": "no-cache"})


# ---------- stream and state ----------
@app.get("/api/stream")
async def stream(user=User):
    queue = hub.subscribe(user["id"])

    async def body():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event, data = await asyncio.wait_for(queue.get(), KEEPALIVE_SECONDS)
                    yield sse(event, data)
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            hub.unsubscribe(user["id"], queue)

    return StreamingResponse(body(), media_type="text/event-stream", headers={"cache-control": "no-cache", "x-accel-buffering": "no"})


@app.get("/api/state")
def get_state(user=User):
    with db.tx() as conn:
        return state.full(conn, user)


# ---------- actions ----------
def _action(fn, *args):
    with db.tx() as conn:
        try:
            return fn(conn, *args)
        except actions.NotFound as e:
            raise HTTPException(404, "action not found") from e
        except actions.Conflict as e:
            raise HTTPException(409, str(e)) from e


@app.get("/api/actions")
def list_actions(status: str | None = None, user=User):
    with db.tx() as conn:
        return actions.list_for(conn, user["id"], status)


class ActionResult(BaseModel):
    ok: bool
    system_id: str | None = None
    error: str | None = None


@app.post("/api/actions/{action_id}/result")
def action_result(action_id: str, body: ActionResult, user=User):
    return _action(actions.report, user["id"], action_id, body.ok, body.system_id, body.error)


@app.post("/api/actions/{action_id}/confirm")
def action_confirm(action_id: str, user=User):
    return _action(actions.confirm, user["id"], action_id)


@app.post("/api/actions/{action_id}/reject")
def action_reject(action_id: str, user=User):
    return _action(actions.reject, user["id"], action_id)


@app.post("/api/actions/{action_id}/undo")
def action_undo(action_id: str, user=User):
    return _action(actions.undo, user["id"], action_id)


# ---------- profile and voiceprint ----------
class ProfileItem(BaseModel):
    category: str
    content: str


def _item_id(item_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(item_id)
    except ValueError as e:
        raise HTTPException(404, "item not found") from e


@app.get("/api/profile")
def get_profile(user=User):
    with db.tx() as conn:
        return memory.items(conn, user["id"])


@app.put("/api/profile/{item_id}")
def put_profile(item_id: str, body: ProfileItem, user=User):
    with db.tx() as conn:
        try:
            item = memory.update(conn, user["id"], _item_id(item_id), body.category, body.content)
        except memory.Invalid as e:
            raise HTTPException(400, str(e)) from e
    if not item:
        raise HTTPException(404, "item not found")
    return item


@app.delete("/api/profile/{item_id}", status_code=204)
def delete_profile(item_id: str, user=User):
    with db.tx() as conn:
        if not memory.delete(conn, user["id"], _item_id(item_id)):
            raise HTTPException(404, "item not found")
    return Response(status_code=204)


@app.post("/api/voiceprint")
async def enroll(request: Request, user=User):
    data = await request.body()

    def store():
        embedding = speech.enrollment_embedding(speech.pcm_to_float(recordings.read_wav(data, MAX_VOICEPRINT_SECONDS)))
        with db.tx() as conn:
            conn.execute(
                """INSERT INTO voiceprints (user_id, embedding) VALUES (%s, %s)
                   ON CONFLICT (user_id) DO UPDATE SET embedding = excluded.embedding, created_at = now()""",
                (user["id"], embedding.tolist()),
            )

    try:
        await asyncio.to_thread(store)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"enrolled": True}


app.mount("/", StaticFiles(directory=config.WEB_DIR, html=True), name="web")
