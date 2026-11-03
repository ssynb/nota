"""Postgres job queue: one job per recording, claimed with SKIP LOCKED under a renewable lease.
Every write a job makes goes through `owned(conn)`, which locks the job row and proves the lease is
still ours in the same transaction, so a worker whose lease was taken over cannot write anything."""
import random
import threading
import uuid

from nota import config, db

CHANNEL = "nota_jobs"


class LeaseLost(Exception):
    pass


def enqueue(conn, recording_id):
    """Asks for the recording to be (re)processed: an idle job is queued again; a running one is marked
    to run once more when it ends, so audio that arrives mid-run is never left waiting."""
    conn.execute(
        """INSERT INTO jobs (recording_id, status) VALUES (%s, 'queued')
           ON CONFLICT (recording_id) DO UPDATE SET
             rerun = jobs.rerun OR jobs.status = 'running',
             status = CASE WHEN jobs.status = 'done' THEN 'queued' ELSE jobs.status END,
             attempts = CASE WHEN jobs.status = 'done' THEN 0 ELSE jobs.attempts END,
             run_after = CASE WHEN jobs.status = 'done' THEN now() ELSE jobs.run_after END,
             owner = CASE WHEN jobs.status = 'done' THEN NULL ELSE jobs.owner END""",
        (recording_id,),
    )
    conn.execute("SELECT pg_notify(%s, '')", (CHANNEL,))


def wake_workers(conn):
    conn.execute("SELECT pg_notify(%s, 'sweep')", (CHANNEL,))


class Lease:
    def __init__(self, recording_id, attempts):
        self.recording_id = recording_id
        self.attempts = attempts
        self.owner = None
        self._stop = threading.Event()

    def owned(self, conn):
        # Lock order everywhere is recording row, then job row (the API locks in the same order).
        conn.execute("SELECT 1 FROM recordings WHERE id = %s FOR UPDATE", (self.recording_id,))
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE recording_id = %s AND owner = %s AND status = 'running' FOR UPDATE", (self.recording_id, self.owner)
        ).fetchone()
        if not row:
            raise LeaseLost(f"job {self.recording_id} is no longer ours")

    def _renew(self):
        while not self._stop.wait(config.JOB_LEASE_SECONDS / 3):
            with db.tx() as conn:
                conn.execute(
                    "UPDATE jobs SET lease_until = now() + make_interval(secs => %s) WHERE recording_id = %s AND owner = %s AND status = 'running'",
                    (config.JOB_LEASE_SECONDS, self.recording_id, self.owner),
                )

    def __enter__(self):
        threading.Thread(target=self._renew, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._stop.set()


def claim() -> Lease | None:
    """Takes the next due job, or one whose owner stopped renewing its lease (crashed worker)."""
    owner = uuid.uuid4()
    with db.tx() as conn:
        row = conn.execute(
            """UPDATE jobs SET status = 'running', owner = %(o)s, attempts = attempts + 1,
                 lease_until = now() + make_interval(secs => %(l)s)
               WHERE recording_id = (
                 SELECT recording_id FROM jobs
                 WHERE (status = 'queued' AND run_after <= now()) OR (status = 'running' AND lease_until < now())
                 ORDER BY run_after FOR UPDATE SKIP LOCKED LIMIT 1)
               RETURNING recording_id, attempts""",
            {"o": owner, "l": config.JOB_LEASE_SECONDS},
        ).fetchone()
    if not row:
        return None
    lease = Lease(row["recording_id"], row["attempts"])
    lease.owner = owner
    return lease


def seconds_until_due() -> float | None:
    with db.tx() as conn:
        row = conn.execute(
            """SELECT extract(epoch FROM min(CASE WHEN status = 'queued' THEN run_after ELSE lease_until END) - now()) AS s
               FROM jobs WHERE status IN ('queued', 'running')"""
        ).fetchone()
    return None if row["s"] is None else max(0.0, float(row["s"]))


def done(conn, lease: Lease):
    """Ends this run: idle, or queued again if new audio arrived meanwhile."""
    conn.execute(
        """UPDATE jobs SET status = CASE WHEN rerun THEN 'queued' ELSE 'done' END,
             attempts = CASE WHEN rerun THEN 0 ELSE attempts END, run_after = now(), rerun = false, owner = NULL
           WHERE recording_id = %s""",
        (lease.recording_id,),
    )
    conn.execute("SELECT pg_notify(%s, '')", (CHANNEL,))


def retry_or_fail(conn, lease: Lease, error: str) -> bool:
    """Schedules a retry with exponential backoff and jitter; returns True when attempts are exhausted."""
    if lease.attempts >= config.JOB_MAX_ATTEMPTS:
        conn.execute("UPDATE jobs SET status = 'failed', owner = NULL, last_error = %s WHERE recording_id = %s", (error, lease.recording_id))
        return True
    delay = config.JOB_RETRY_BASE_SECONDS * 2 ** (lease.attempts - 1) * random.uniform(1, 1.5)
    conn.execute(
        """UPDATE jobs SET status = 'queued', owner = NULL, last_error = %s, run_after = now() + make_interval(secs => %s)
           WHERE recording_id = %s""",
        (error, delay, lease.recording_id),
    )
    return False
