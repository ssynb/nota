"""History lookup for the agent: conversations filtered by person, local date range and keywords,
returned with their source (conversation, start time, offset) so answers can cite them."""
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MAX_RESULTS = 8
MAX_FACTS = 8
MAX_LINES = 4
MAX_OVERVIEW = 300
SCAN_LIMIT = 500


def _terms(*values) -> list[str]:
    return [t for v in values if v for t in re.split(r"[\s,，、。;；]+", v) if t]


def _local_bound(day: str | None, tz: ZoneInfo, end: bool) -> datetime | None:
    if not day:
        return None
    d = date.fromisoformat(day[:10]) + (timedelta(days=1) if end else timedelta())
    return datetime.combine(d, time(), tz)


def find_notes(conn, user, person=None, date_from=None, date_to=None, keyword=None) -> list[dict]:
    """Every result carries `ref`; the model cites results as [ref]. A miss means nothing matched in
    titles, summaries, facts or transcript lines of this user's conversations in that window."""
    tz = ZoneInfo(user["timezone"])
    start, end = _local_bound(date_from, tz, False), _local_bound(date_to, tz, True)
    if start and end and start >= end:
        raise ValueError("date_from 晚于 date_to")
    rows = conn.execute(
        """SELECT c.id, c.started_at, c.title, c.overview, c.lines,
                  coalesce(json_agg(json_build_object('text', f.text, 'speaker', f.speaker_label, 'offset', f.offset_seconds)
                           ORDER BY f.offset_seconds) FILTER (WHERE f.id IS NOT NULL), '[]') AS facts
           FROM conversations c LEFT JOIN facts f ON f.conversation_id = c.id
           WHERE c.user_id = %s AND c.summarized AND (%s::timestamptz IS NULL OR c.started_at >= %s) AND (%s::timestamptz IS NULL OR c.started_at < %s)
           GROUP BY c.id ORDER BY c.started_at DESC LIMIT %s""",
        (user["id"], start, start, end, end, SCAN_LIMIT),
    ).fetchall()
    people, words = _terms(person), _terms(keyword)
    hits = []
    for r in rows:
        texts = [r["title"] or "", r["overview"] or ""] + [f["text"] for f in r["facts"]] + [l["text"] for l in r["lines"]]
        blob = "\n".join(texts)
        if people and not any(p in blob for p in people):
            continue
        score = sum(w in blob for w in words) if words else 1
        if words and not score:
            continue
        hits.append((score, r))
    hits.sort(key=lambda h: (-h[0], -h[1]["started_at"].timestamp()))
    results = []
    for ref, (_, r) in enumerate(hits[:MAX_RESULTS], 1):
        match = lambda text: any(t in text for t in people + words)
        facts = sorted(r["facts"], key=lambda f: not match(f["text"]))[:MAX_FACTS]
        lines = [l for l in r["lines"] if match(l["text"])][:MAX_LINES]
        results.append({
            "ref": ref,
            "conversation_id": str(r["id"]),
            "started_at": r["started_at"].astimezone(tz).isoformat(timespec="minutes"),
            "title": r["title"],
            "overview": (r["overview"] or "")[:MAX_OVERVIEW],
            "facts": [{"text": f["text"], "speaker": f["speaker"], "offset_seconds": f["offset"]} for f in facts],
            "lines": [{"text": l["text"], "speaker": l["speaker"], "offset_seconds": l["offset"]} for l in lines],
        })
    return results


def citation(result: dict) -> dict:
    evidence = result["facts"][:1] or result["lines"][:1]
    return {
        "conversation_id": result["conversation_id"],
        "started_at": result["started_at"],
        "offset_seconds": evidence[0]["offset_seconds"] if evidence else 0,
    }


def get_profile(conn, user, person=None) -> list[dict]:
    rows = conn.execute(
        "SELECT category, content FROM profile_items WHERE user_id = %s ORDER BY updated_at DESC", (user["id"],)
    ).fetchall()
    names = _terms(person)
    return [dict(r) for r in rows if not names or any(n in r["content"] for n in names)]
