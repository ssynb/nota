"""User-facing change feed. Writers publish inside their own transaction (so an event exists only if
the change committed); every API process LISTENs and fans events out to that user's open streams."""
import asyncio
import threading
from collections import defaultdict

from nota import db

CHANNEL = "nota_events"


def publish(conn, user_id: str, type_: str, data: dict):
    row = conn.execute(
        "INSERT INTO events (user_id, type, data) VALUES (%s, %s, %s) RETURNING id", (user_id, type_, db.json(data))
    ).fetchone()
    conn.execute("SELECT pg_notify(%s, %s)", (CHANNEL, str(row["id"])))


class Hub:
    def __init__(self):
        self._subs: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = defaultdict(set)
        self._lock = threading.Lock()
        self._ready = threading.Event()

    def start(self):
        threading.Thread(target=self._listen, daemon=True, name="events-listen").start()
        self._ready.wait(10)

    def _listen(self):
        conn = db.connect()
        conn.execute(f"LISTEN {CHANNEL}")
        self._ready.set()
        for note in conn.notifies():
            # the listening connection is busy inside notifies(); read the event on a pooled one
            with db.tx() as reader:
                row = reader.execute("SELECT user_id, type, data FROM events WHERE id = %s", (int(note.payload),)).fetchone()
            if not row:
                continue
            with self._lock:
                targets = list(self._subs.get(row["user_id"], ()))
            for loop, queue in targets:
                loop.call_soon_threadsafe(queue.put_nowait, (row["type"], row["data"]))

    def subscribe(self, user_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subs[user_id].add((asyncio.get_running_loop(), queue))
        return queue

    def unsubscribe(self, user_id: str, queue: asyncio.Queue):
        with self._lock:
            self._subs[user_id] = {s for s in self._subs[user_id] if s[1] is not queue}
