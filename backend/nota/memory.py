"""User profile ("画像"), after yuanzhuoai's memory module: short items in fixed categories, written
after each conversation; a new fact replaces the old one instead of piling up; over budget the model
is told to compress; the user can edit or delete any item and an edit wins over a concurrent rewrite."""
import uuid

from nota import llm

MAX_CONTENT = 200
MERGE_WINDOW = 60      # items the model compares against, most recently updated first
BUDGET_CHARS = 2400    # the whole profile has to stay small enough to hand to the model at once
BUDGET_ITEMS = 40
CATEGORIES = {
    "person": "人物：用户身边的人是谁、什么关系、怎么称呼（张总=张伟）",
    "background": "背景：用户的角色、公司、长期处境",
    "preference": "偏好：用户的习惯和偏好",
    "ongoing": "近期：用户近期在推进的事，过期就删",
}


class Invalid(ValueError):
    pass


class _Edited(Exception):
    pass


def validate(category: str, content: str) -> str:
    content = (content or "").strip()
    if category not in CATEGORIES:
        raise Invalid(f"category 必须是 {', '.join(CATEGORIES)} 之一")
    if not content or len(content) > MAX_CONTENT:
        raise Invalid(f"每条 1 到 {MAX_CONTENT} 字")
    return content


def to_dict(row) -> dict:
    return {"id": str(row["id"]), "category": row["category"], "content": row["content"], "updated_at": row["updated_at"].isoformat()}


def items(conn, user_id) -> list[dict]:
    rows = conn.execute("SELECT * FROM profile_items WHERE user_id = %s ORDER BY updated_at DESC", (user_id,)).fetchall()
    return [to_dict(r) for r in rows]


def update(conn, user_id, item_id, category, content) -> dict | None:
    row = conn.execute(
        "UPDATE profile_items SET category = %s, content = %s, updated_at = now() WHERE id = %s AND user_id = %s RETURNING *",
        (category, validate(category, content), item_id, user_id),
    ).fetchone()
    return to_dict(row) if row else None


def delete(conn, user_id, item_id) -> bool:
    return conn.execute("DELETE FROM profile_items WHERE id = %s AND user_id = %s", (item_id, user_id)).rowcount == 1


PROMPT = """你维护用户的个人画像：少量长期有用的事实，之后每次都会整份交给 AI 用来认人、理解用户。
读完下面这段对话，对照既有条目给出改动。只输出一个 JSON 对象：
{{"items": [{{"category": "person", "content": "一句话事实", "replaces": ["m1"]}}], "deletes": ["m3"]}}
类别只能是：
{categories}
规则：
- content 用中文陈述句，一条只讲一件事，不超过 80 字；只记用户本人和他身边的人，不记一次性的会议细节（那些另有会议记录）。
- 同一件事已经记过，必须用 replaces 取代或合并，不得并排新增；新说法取代旧说法（团队 8 人 → 12 人）时写 replaces。
- deletes 写明确过期、又不需要新条目顶替的条目。replaces 和 deletes 只能写既有条目的记号。
- 没有值得长期记住的事实时输出 {{"items": [], "deletes": []}}。{budget}

既有条目：
{roster}"""


def plan(conn, user_id, conversation_text: str) -> dict:
    """Asks the model for a merge plan against the current items (no writes)."""
    rows = conn.execute("SELECT * FROM profile_items WHERE user_id = %s ORDER BY updated_at DESC", (user_id,)).fetchall()
    window = rows[:MERGE_WINDOW]
    marks = {f"m{i + 1}": r for i, r in enumerate(window)}
    over = sum(len(r["content"]) for r in rows) > BUDGET_CHARS or len(rows) > BUDGET_ITEMS
    prompt = PROMPT.format(
        categories="\n".join(f"- {k}：{v}" for k, v in CATEGORIES.items()),
        budget="\n- 画像已超出预算：这次请把重复、过时、琐碎的条目合并或删除，总量明显变少。" if over else "",
        roster="\n".join(f"{m} {r['category']} {' '.join(r['content'].split())}" for m, r in marks.items()) or "（无）",
    )
    draft = llm.json_reply([{"role": "system", "content": prompt}, {"role": "user", "content": conversation_text}])
    return {"draft": draft, "marks": {m: (r["id"], r["updated_at"]) for m, r in marks.items()}}


def apply(conn, user_id, learned: dict) -> int:
    """Applies a merge plan. A replaced or deleted item counts only if it is unchanged since the model
    read it; otherwise the user edited it meanwhile and that part of the plan is dropped."""
    marks, draft = learned["marks"], learned["draft"]
    written = 0
    for item in draft.get("items") or []:
        try:
            content = validate(item.get("category"), item.get("content"))
        except Invalid:
            continue
        refs = item.get("replaces") or []
        if any(m not in marks for m in refs):
            continue
        try:
            with conn.transaction():  # savepoint: a half-applied replacement is undone
                removed = sum(conn.execute("DELETE FROM profile_items WHERE id = %s AND updated_at = %s", marks[m]).rowcount for m in refs)
                if removed != len(refs):
                    raise _Edited()
                conn.execute(
                    "INSERT INTO profile_items (id, user_id, category, content) VALUES (%s, %s, %s, %s)",
                    (uuid.uuid4(), user_id, item["category"], content),
                )
        except _Edited:
            continue
        written += 1
    for m in draft.get("deletes") or []:
        if m in marks:
            written += conn.execute("DELETE FROM profile_items WHERE id = %s AND updated_at = %s", marks[m]).rowcount
    return written
