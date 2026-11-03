"""Questions about history: one ask charged once per idempotency key, under a lease so a crashed API
process cannot keep a charge forever (the next request of that user refunds expired asks)."""
import queue
import threading
import uuid

from nota import agent, clock, config, db, quota


def _release_expired(conn, user_id):
    rows = conn.execute(
        """UPDATE asks SET status = 'failed' WHERE user_id = %s AND status = 'running' AND lease_until < now()
           RETURNING period""",
        (user_id,),
    ).fetchall()
    for r in rows:
        quota.refund_ask(conn, user_id, r["period"])


def reserve(user, idem_key: str) -> dict:
    """kind: run (with owner) | replay (with result) | busy | quota."""
    with db.tx() as conn:
        _release_expired(conn, user["id"])
        owner = uuid.uuid4()
        claimed = conn.execute(
            """INSERT INTO asks (user_id, idem_key, period, status, owner, lease_until)
               VALUES (%(u)s, %(k)s, '', 'running', %(o)s, now() + make_interval(secs => %(l)s))
               ON CONFLICT (user_id, idem_key) DO UPDATE SET status = 'running', owner = excluded.owner, lease_until = excluded.lease_until
               WHERE asks.status = 'failed'
               RETURNING idem_key""",
            {"u": user["id"], "k": idem_key, "o": owner, "l": config.ASK_LEASE_SECONDS},
        ).fetchone()
        if not claimed:
            row = conn.execute("SELECT status, result FROM asks WHERE user_id = %s AND idem_key = %s", (user["id"], idem_key)).fetchone()
            return {"kind": "replay", "result": row["result"]} if row["status"] == "done" else {"kind": "busy"}
        period = quota.take_ask(conn, user)
        if period is None:
            conn.execute("DELETE FROM asks WHERE user_id = %s AND idem_key = %s", (user["id"], idem_key))
            return {"kind": "quota"}
        conn.execute("UPDATE asks SET period = %s WHERE user_id = %s AND idem_key = %s", (period, user["id"], idem_key))
        quota.publish(conn, user)
    return {"kind": "run", "owner": owner}


def _owned(conn, user_id, idem_key, owner) -> bool:
    return conn.execute(
        "SELECT 1 FROM asks WHERE user_id = %s AND idem_key = %s AND owner = %s AND status = 'running' FOR UPDATE",
        (user_id, idem_key, owner),
    ).fetchone() is not None


def run(user, idem_key: str, owner, question: str):
    """Yields (event, data) pairs as they happen: step…, then answer or error. The result is stored for replay."""
    out: queue.Queue = queue.Queue()
    stop = threading.Event()
    threading.Thread(target=_renew, args=(user["id"], idem_key, owner, stop), daemon=True).start()
    threading.Thread(target=_answer, args=(user, idem_key, owner, question, out, stop), daemon=True).start()
    while True:
        event = out.get()
        yield event
        if event[0] in ("answer", "error"):
            return


def _renew(user_id, idem_key, owner, stop):
    while not stop.wait(config.ASK_LEASE_SECONDS / 3):
        with db.tx() as conn:
            conn.execute(
                "UPDATE asks SET lease_until = now() + make_interval(secs => %s) WHERE user_id = %s AND idem_key = %s AND owner = %s",
                (config.ASK_LEASE_SECONDS, user_id, idem_key, owner),
            )


def _answer(user, idem_key, owner, question, out, stop):
    steps: list[dict] = []

    def on_step(tool, args, result):
        steps.append({"tool": tool, "args": args, "result": result})
        out.put(("step", steps[-1]))

    try:
        answer = agent.ask(user, question, clock.now(), on_step)
    except Exception as e:  # noqa: BLE001 — any failure of the ask is refunded and reported to the client
        stop.set()
        with db.tx() as conn:
            if _owned(conn, user["id"], idem_key, owner):
                row = conn.execute(
                    "UPDATE asks SET status = 'failed' WHERE user_id = %s AND idem_key = %s RETURNING period", (user["id"], idem_key)
                ).fetchone()
                quota.refund_ask(conn, user["id"], row["period"])
                quota.publish(conn, user)
        out.put(("error", {"message": str(e)}))
        return
    stop.set()
    with db.tx() as conn:
        if _owned(conn, user["id"], idem_key, owner):
            conn.execute(
                "UPDATE asks SET status = 'done', result = %s WHERE user_id = %s AND idem_key = %s",
                (db.json({"steps": steps, "answer": answer}), user["id"], idem_key),
            )
    out.put(("answer", answer))


def replay(result: dict):
    yield from (("step", s) for s in result["steps"])
    yield "answer", result["answer"]
