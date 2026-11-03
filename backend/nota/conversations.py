"""Splitting a recording into conversations at long silences, and writing each one's minutes."""
from datetime import timedelta

from nota import config, llm

OWNER = "我"


def split(utterances: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    for u in utterances:
        if groups and u["start"] - groups[-1][-1]["end"] < config.CONVERSATION_GAP_SECONDS:
            groups[-1].append(u)
        else:
            groups.append([u])
    return groups


def build(recording, idx: int, group: list[dict]) -> dict:
    """A conversation row: times are absolute, line offsets are seconds from the conversation's start."""
    base = group[0]["start"]
    speakers, seen = [], set()
    for u in group:
        if u["speaker"] not in seen:
            seen.add(u["speaker"])
            speakers.append({"label": u["speaker"], "is_owner": u["is_owner"]})
    return {
        "idx": idx,
        "started_at": recording["started_at"] + timedelta(seconds=base),
        "ended_at": recording["started_at"] + timedelta(seconds=group[-1]["end"]),
        "speakers": speakers,
        "lines": [{"offset": round(u["start"] - base, 1), "speaker": u["speaker"], "text": u["text"]} for u in group],
    }


def text_note(recording) -> dict:
    return {
        "idx": 0,
        "started_at": recording["started_at"],
        "ended_at": recording["started_at"],
        "speakers": [{"label": OWNER, "is_owner": True}],
        "lines": [{"offset": 0, "speaker": OWNER, "text": recording["text"]}],
    }


def render(conv: dict) -> str:
    """Transcript as the model reads it: a speaker legend, then `[mm:ss] 说话人：原话` lines."""
    legend = "；".join(f"{s['label']}={'用户本人' if s['is_owner'] else '其他人'}" for s in conv["speakers"])
    lines = [f"[{int(l['offset']) // 60:02d}:{int(l['offset']) % 60:02d}] {l['speaker']}：{l['text']}" for l in conv["lines"]]
    return f"说话人：{legend}\n" + "\n".join(lines)


SUMMARY_PROMPT = """你为用户整理一段对话的会议纪要。对话来自录音转写，可能有错别字和口语。
只输出一个 JSON 对象：
{"title": "不超过 20 字的标题", "overview": "2~4 句摘要", "facts": [{"text": "一条关键事实，人名、数字、时间照原话保留", "speaker_label": "说这句话的说话人标签", "offset_seconds": 这句话在对话中的秒数}]}
规则：
- facts 只记对话里明确说出的事实、数字、承诺和决定，不推测、不编造；每条写清是谁说的（speaker_label 用说话人图例里的标签）。
- 同一件事前后数字变了（如报价从 20 万改到 18 万），每次都记一条，按时间顺序。
- 闲聊、没有信息量的话不记。"""


def summarize(conv: dict) -> dict:
    out = llm.json_reply([{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": render(conv)}])
    labels = {s["label"] for s in conv["speakers"]}
    facts = []
    for f in out.get("facts") or []:
        if not isinstance(f, dict) or not str(f.get("text", "")).strip():
            continue
        label = f.get("speaker_label")
        try:
            offset = float(f.get("offset_seconds") or 0)
        except (TypeError, ValueError):
            offset = 0.0
        facts.append({"text": str(f["text"]).strip(), "speaker_label": label if label in labels else None, "offset_seconds": offset})
    return {"title": str(out.get("title") or "")[:60], "overview": str(out.get("overview") or ""), "facts": facts}
