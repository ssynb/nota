"""Worker process: `python -m nota.worker`. Takes jobs from the Postgres queue one at a time; wakes on
NOTIFY when work is enqueued, otherwise when the next retry or lease expiry is due. Every
RESUME_INTERVAL_SECONDS (and at start) recordings paused by quota get another go: quota windows reset
with time, so waiting is the business rule here."""
import logging
import time
import traceback

from nota import config, db, jobs, llm, pipeline, recordings, speech

log = logging.getLogger("nota.worker")


def sweep():
    with db.tx() as conn:
        resumed = recordings.resume_paused(conn)
    if resumed:
        log.info("resumed %d paused recordings", resumed)


def run_one() -> bool:
    lease = jobs.claim()
    if lease is None:
        return False
    with lease:
        run, fail = (pipeline.learn_profile, pipeline.fail_profile) if lease.kind == jobs.PROFILE else (pipeline.process, pipeline.fail)
        try:
            run(lease)
        except jobs.LeaseLost:
            log.warning("lease lost on %s; another worker owns it now", lease.recording_id)
        except Exception as e:  # noqa: BLE001 — any failure is retried; a process job that runs out is refunded
            log.error("%s job %s failed: %s", lease.kind, lease.recording_id, traceback.format_exc())
            try:
                fail(lease, str(e) if isinstance(e, llm.LLMError) else f"{type(e).__name__}: {e}")
            except jobs.LeaseLost:
                pass
    return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    db.ensure_schema()
    speech.load()
    listen = db.connect()
    listen.execute(f"LISTEN {jobs.CHANNEL}")
    print("nota worker ready", flush=True)
    next_sweep = 0.0
    while True:
        if time.monotonic() >= next_sweep:
            sweep()
            next_sweep = time.monotonic() + config.RESUME_INTERVAL_SECONDS
        if run_one():
            continue
        due = jobs.seconds_until_due()
        timeout = min(x for x in (due, next_sweep - time.monotonic(), config.RESUME_INTERVAL_SECONDS) if x is not None)
        for note in listen.notifies(timeout=max(0.05, timeout), stop_after=1):
            if note.payload == "sweep":
                next_sweep = 0.0


if __name__ == "__main__":
    main()
