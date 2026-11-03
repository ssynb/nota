"""Runs the real service (API processes + worker processes) against a throwaway Postgres database."""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import uuid
import wave
from io import BytesIO
from pathlib import Path

import httpx
import numpy as np
import psycopg

BACKEND = Path(__file__).resolve().parent.parent
ADMIN_URL = os.environ.get("TEST_DATABASE_ADMIN_URL", "postgresql://nota:nota@localhost:55432/nota")
TZ = "Asia/Shanghai"
RATE = 16000
AUDIO_CACHE = Path(os.environ.get("TMPDIR", "/tmp")) / "nota-test-audio"


# ---------- database ----------
def create_database() -> str:
    name = f"nota_t_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    return ADMIN_URL.rsplit("/", 1)[0] + "/" + name


def drop_database(url: str):
    name = url.rsplit("/", 1)[1]
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


# ---------- processes ----------
_children: list[subprocess.Popen] = []


def kill_all():
    for p in _children:
        if p.poll() is None:
            p.kill()
    for p in _children:
        p.wait()
    _children.clear()


class Proc:
    """A child process whose output lines are watched for a readiness marker."""

    def __init__(self, args, env, ready: str, count: int = 1):
        self.lines: list[str] = []
        self._ready = threading.Event()
        self._needed = count
        self._marker = ready
        self.p = subprocess.Popen(args, cwd=BACKEND, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        _children.append(self.p)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.p.stdout:
            self.lines.append(line)
            if os.environ.get("TEST_SERVICE_LOG"):
                sys.stderr.write(line)
            if self._marker in line:
                self._needed -= 1
                if self._needed == 0:
                    self._ready.set()
        self._ready.set()

    def wait_ready(self, timeout=180):
        self._ready.wait(timeout)
        if self.p.poll() is not None or self._needed:
            raise RuntimeError("process failed to start:\n" + "".join(self.lines[-40:]))

    def kill(self):
        if self.p.poll() is None:
            os.killpg(self.p.pid, signal.SIGKILL)
            self.p.wait()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Service:
    """Two API processes behind one port plus N worker processes, all on one database."""

    def __init__(self, db_url: str, llm_url: str, env: dict | None = None):
        self.db_url = db_url
        self.llm_url = llm_url
        self.extra = env or {}
        self.api = None
        self.workers: list[Proc] = []
        self.url = None

    def env(self, now: str | None):
        env = {
            **os.environ,
            "DATABASE_URL": self.db_url,
            "LLM_BASE_URL": self.llm_url,
            "LLM_API_KEY": "test",
            "LLM_MODEL": "fake",
            "PYTHONPATH": f"{BACKEND}:{BACKEND / 'tests' / 'clockpatch'}",
            "PYTHONUNBUFFERED": "1",
            **self.extra,
        }
        env.pop("TEST_NOW", None)
        if now:
            env["TEST_NOW"] = now
        return env

    def start_api(self, now=None):
        port = free_port()
        self.api = Proc([sys.executable, "-m", "uvicorn", "nota.app:app", "--port", str(port), "--workers", "2"], self.env(now), "Application startup complete", count=2)
        self.api.wait_ready()
        self.url = f"http://127.0.0.1:{port}"

    def start_workers(self, n=2, now=None):
        new = [Proc([sys.executable, "-m", "nota.worker"], self.env(now), "worker ready") for _ in range(n)]
        for w in new:
            w.wait_ready()
        self.workers += new

    def stop_workers(self):
        for w in self.workers:
            w.kill()
        self.workers = []

    def start(self, now=None, workers=2):
        self.start_api(now)
        self.start_workers(workers, now)
        return self

    def restart(self, now=None, workers=2):
        self.stop()
        return self.start(now, workers)

    def set_plan(self, user: str, plan: str):
        subprocess.run([sys.executable, "-m", "nota.admin", "set-plan", user, plan], cwd=BACKEND, env=self.env(None), check=True, capture_output=True)

    def stop(self):
        self.stop_workers()
        if self.api:
            self.api.kill()


# ---------- client ----------
def new_user() -> str:
    return str(uuid.uuid4())


def headers(user):
    return {"x-user-id": user, "x-timezone": TZ}


class Client:
    def __init__(self, base: str, user: str):
        self.base = base
        self.user = user
        self.http = httpx.Client(base_url=base, headers=headers(user), timeout=60)

    def state(self):
        r = self.http.get("/api/state")
        r.raise_for_status()
        return r.json()

    def start_recording(self, recording_id=None):
        rid = recording_id or str(uuid.uuid4())
        r = self.http.post("/api/recordings", json={"recording_id": rid, "started_at": "2026-11-03T10:00:00+08:00"})
        r.raise_for_status()
        return r.json()

    def put_chunk(self, rid, seq, data: bytes):
        return self.http.put(f"/api/recordings/{rid}/chunks/{seq}", content=data, headers={"content-type": "audio/wav"})

    def finish(self, rid, total):
        r = self.http.post(f"/api/recordings/{rid}/finish", json={"total_chunks": total})
        r.raise_for_status()
        return r.json()

    def upload(self, samples: np.ndarray, chunk_seconds=30, recording_id=None):
        rec = self.start_recording(recording_id)
        chunks = split(samples, chunk_seconds)
        for seq, data in enumerate(chunks):
            self.put_chunk(rec["recording_id"], seq, data).raise_for_status()
        assert self.finish(rec["recording_id"], len(chunks)) == {"missing": []}
        return rec["recording_id"]

    def text_note(self, text, idem_key=None):
        r = self.http.post("/api/notes/text", json={"idem_key": idem_key or str(uuid.uuid4()), "text": text})
        r.raise_for_status()
        return r.json()["recording_id"]

    def ask(self, question, idem_key=None):
        """Returns (status, events) where events are (name, data) pairs of the SSE answer stream."""
        with self.http.stream("POST", "/api/ask", json={"idem_key": idem_key or str(uuid.uuid4()), "question": question}) as r:
            if not r.headers.get("content-type", "").startswith("text/event-stream"):
                r.read()
                return r.status_code, r.json()
            return r.status_code, list(sse(r.iter_lines()))


def sse(lines):
    event, data = None, []
    for line in lines:
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
        elif line == "" and data:
            yield event or "message", json.loads("\n".join(data))
            event, data = None, []


class Stream:
    """Background subscriber to /api/stream; wait_for blocks on the next matching event."""

    def __init__(self, base, user):
        self.events: list[tuple[str, dict]] = []
        self._cond = threading.Condition()
        self._client = httpx.Client(base_url=base, headers=headers(user), timeout=None)
        self._open = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        assert self._open.wait(30), "stream did not open"

    def _run(self):
        with self._client.stream("GET", "/api/stream") as r:
            self._open.set()
            for item in sse(r.iter_lines()):
                with self._cond:
                    self.events.append(item)
                    self._cond.notify_all()

    def wait_for(self, predicate, timeout=120):
        with self._cond:
            ok = self._cond.wait_for(lambda: any(predicate(n, d) for n, d in self.events), timeout)
        assert ok, f"event not seen; got {[(n, d.get('status')) for n, d in self.events][-20:]}"
        return next(d for n, d in self.events if predicate(n, d))

    def recording_status(self, rid, status, timeout=180):
        return self.wait_for(lambda n, d: n == "recording" and d["recording_id"] == rid and d["status"] == status, timeout)


# ---------- audio ----------
def speech(text: str, voice: str = "Tingting") -> np.ndarray:
    """macOS `say` synthesised speech as 16 kHz mono int16 samples (cached)."""
    AUDIO_CACHE.mkdir(exist_ok=True)
    key = uuid.uuid5(uuid.NAMESPACE_URL, voice + text).hex
    wav = AUDIO_CACHE / f"{key}.wav"
    if not wav.exists():
        aiff = AUDIO_CACHE / f"{key}.aiff"
        subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{RATE}", "-c", "1", str(aiff), str(wav)], check=True)
    with wave.open(str(wav)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * RATE), dtype=np.int16)


def wav_bytes(samples: np.ndarray, rate=RATE, channels=1) -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.astype(np.int16).tobytes())
    return buf.getvalue()


def split(samples: np.ndarray, seconds=30) -> list[bytes]:
    step = int(seconds * RATE)
    return [wav_bytes(samples[i : i + step]) for i in range(0, len(samples), step)]
