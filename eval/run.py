"""Stage-by-stage evaluation (Part A T5). One command:

    uv run --project backend eval/run.py [asr] [speaker] [actions] [minutes] [qa] [--repeat N]

asr and speaker run the local models on speech synthesised with macOS `say` (reproducible);
actions, minutes and qa call the real model from .env against a throwaway Postgres database.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CASES = Path(__file__).resolve().parent / "cases"
sys.path.insert(0, str(ROOT / "backend"))
ADMIN_URL = os.environ.get("TEST_DATABASE_ADMIN_URL", "postgresql://nota:nota@localhost:55432/nota")
AUDIO = Path(tempfile.gettempdir()) / "nota-eval-audio"
THRESHOLDS = {"asr": 0.10, "speaker": 0.10, "actions": 0.8, "minutes": 0.8, "qa": 0.8}
CONCURRENCY = 4


def load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


# ---------- numbers ----------
DIGITS = "零一二三四五六七八九"


def int_to_cn(n: int) -> str:
    if n < 10:
        return DIGITS[n]
    if n >= 10000:
        high, low = divmod(n, 10000)
        return int_to_cn(high) + "万" + ("" if not low else ("零" if low < 1000 else "") + int_to_cn(low))
    out, zero = "", False
    for value, unit in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
        d, n = divmod(n, value)
        if d:
            out += ("零" if zero and out else "") + ("" if unit == "十" and d == 1 and not out else DIGITS[d]) + unit
            zero = False
        elif out:
            zero = True
    return out


def cn_to_int(s: str) -> int | None:
    total, section, digit = 0, 0, 0
    for ch in s.replace("两", "二"):
        if ch in DIGITS:
            digit = DIGITS.index(ch)
        elif ch in "十百千":
            section += (digit or 1) * {"十": 10, "百": 100, "千": 1000}[ch]
            digit = 0
        elif ch == "万":
            total += (section + digit) * 10000
            section = digit = 0
        else:
            return None
    return total + section + digit


def numbers_in(text: str) -> set[int]:
    found = {int(float(x)) for x in re.findall(r"\d+(?:\.\d+)?", text)}
    for token in re.findall(r"[零一二两三四五六七八九十百千万]+", text):
        if len(token) >= 2 or token in "十百千万":
            value = cn_to_int(token)
            if value is not None:
                found.add(value)
    return found


def normalise_asr(text: str) -> str:
    text = re.sub(r"(\d+)%", lambda m: "百分之" + int_to_cn(int(m.group(1))), text)
    text = re.sub(r"\d+", lambda m: int_to_cn(int(m.group(0))), text)
    return re.sub(r"[\s，。、！？；：,.!?;:“”\"'（）()]", "", text).replace("两", "二")


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# ---------- audio ----------
def synth(text: str, voice: str):
    import numpy as np

    AUDIO.mkdir(exist_ok=True)
    key = uuid.uuid5(uuid.NAMESPACE_URL, voice + text).hex
    wav = AUDIO / f"{key}.wav"
    if not wav.exists():
        aiff = AUDIO / f"{key}.aiff"
        subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)], check=True)
    with wave.open(str(wav)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768


def stage_asr(_args):
    from nota import speech

    suite = json.loads((CASES / "asr.json").read_text())
    per_voice, rows = {}, []
    term_hits = term_total = 0
    for voice in suite["voices"]:
        errors = chars = 0
        for s in suite["sentences"]:
            samples = synth(s["text"], voice)
            started = time.perf_counter()
            spans = speech.utterances(speech.vad(samples))
            hyp = "".join(speech.transcribe(samples[int(a * 16000) : int(b * 16000)]) for a, b in spans)
            elapsed = time.perf_counter() - started
            ref, norm = normalise_asr(s["text"]), normalise_asr(hyp)
            e = edit_distance(ref, norm)
            errors, chars = errors + e, chars + len(ref)
            hits = [t for t in s["terms"] if normalise_asr(t) in norm]
            term_hits, term_total = term_hits + len(hits), term_total + len(s["terms"])
            if e:
                rows.append(f"    {voice} 参考：{s['text']}\n    {' ' * len(voice)} 识别：{hyp}（{e} 处错，{elapsed:.2f}s）")
        per_voice[voice] = errors / chars
    overall = sum(per_voice.values()) / len(per_voice)
    print("  各声音错字率（CER）：" + "，".join(f"{v} {c:.1%}" for v, c in per_voice.items()))
    print(f"  人名/数字等关键词识别率：{term_hits}/{term_total}（{term_hits / term_total:.1%}）")
    if rows:
        print("  有错的句子：\n" + "\n".join(rows))
    mandarin = per_voice[suite["owner_voice"]]
    return {"metric": f"普通话（{suite['owner_voice']}）CER {mandarin:.1%}，三声音平均 {overall:.1%}", "ok": mandarin <= THRESHOLDS["asr"]}


def stage_speaker(_args):
    import numpy as np

    from nota import config, speech

    suite = json.loads((CASES / "asr.json").read_text())
    owner_voice = suite["owner_voice"]
    enrol = np.concatenate([synth(suite["enroll"], owner_voice), np.zeros(8000, np.float32), synth(suite["enroll"], owner_voice)])
    owner = speech.enrollment_embedding(enrol)
    genuine, impostor = [], []
    for voice in suite["voices"]:
        for s in suite["sentences"]:
            samples = synth(s["text"], voice)
            score = speech.cosine(owner, speech.embed(samples))
            (genuine if voice == owner_voice else impostor).append((score, voice, s["text"]))
    t = config.OWNER_THRESHOLD
    frr = [g for g in genuine if g[0] < t]
    far = [i for i in impostor if i[0] >= t]
    print(f"  阈值 {t}：本人 {len(genuine)} 条，被认成别人 {len(frr)} 条（{len(frr) / len(genuine):.1%}）；"
          f"他人 {len(impostor)} 条，被认成本人 {len(far)} 条（{len(far) / len(impostor):.1%}）")
    print(f"  本人得分 {min(g[0] for g in genuine):.2f}–{max(g[0] for g in genuine):.2f}；"
          f"他人得分 {min(i[0] for i in impostor):.2f}–{max(i[0] for i in impostor):.2f}")
    for score, voice, text in frr + far:
        print(f"    错：{voice} {score:.2f} {text}")
    worst = max(len(frr) / len(genuine), len(far) / len(impostor))
    return {"metric": f"误拒 {len(frr)}/{len(genuine)}，误认 {len(far)}/{len(impostor)}", "ok": worst <= THRESHOLDS["speaker"]}


# ---------- database-backed stages ----------
class Sandbox:
    """A throwaway database; users and seeded history for model-backed stages."""

    def __enter__(self):
        import psycopg

        self.name = f"nota_eval_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            conn.execute(f"CREATE DATABASE {self.name}")
        os.environ["DATABASE_URL"] = ADMIN_URL.rsplit("/", 1)[0] + "/" + self.name
        from nota import db

        db.ensure_schema()
        return self

    def __exit__(self, *exc):
        import psycopg

        from nota import db

        db.pool().close()
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {self.name} WITH (FORCE)")

    @staticmethod
    def user(tz: str) -> dict:
        from nota import db

        uid = str(uuid.uuid4())
        with db.tx() as conn:
            conn.execute("INSERT INTO users (id, timezone) VALUES (%s, %s)", (uid, tz))
        return {"id": uid, "plan": "free", "timezone": tz}

    @staticmethod
    def seed(user, started_at: datetime, title: str, speakers: list[str], facts: list[tuple[str, str]]) -> str:
        from nota import db

        rid, cid = uuid.uuid4(), uuid.uuid4()
        sp = [{"label": s, "is_owner": s == "我"} for s in speakers]
        lines = [{"offset": i * 10, "speaker": who, "text": text} for i, (who, text) in enumerate(facts)]
        with db.tx() as conn:
            conn.execute("INSERT INTO recordings (id, user_id, kind, text, started_at, status, ai) VALUES (%s, %s, 'text', %s, %s, 'done', 'enabled')", (rid, user["id"], title, started_at))
            conn.execute(
                """INSERT INTO conversations (id, recording_id, idx, user_id, started_at, ended_at, speakers, lines, title, overview, summarized, agent_done, memory_done)
                   VALUES (%s, %s, 0, %s, %s, %s, %s, %s, %s, %s, true, true, true)""",
                (cid, rid, user["id"], started_at, started_at + timedelta(minutes=10), db.json(sp), db.json(lines), title, title),
            )
            for i, (who, text) in enumerate(facts):
                conn.execute("INSERT INTO facts (conversation_id, text, speaker_label, offset_seconds) VALUES (%s, %s, %s, %s)", (cid, text, who, i * 10))
        return str(cid)


def run_cases(cases, fn, repeat):
    jobs = [(c, r) for c in cases for r in range(repeat)]
    with ThreadPoolExecutor(CONCURRENCY) as pool:
        results = list(pool.map(lambda job: fn(job[0]), jobs))
    by_case: dict[str, list] = {}
    for (c, _), res in zip(jobs, results):
        by_case.setdefault(c["id"], []).append(res)
    return by_case


def report(by_case, label):
    total = sum(len(v) for v in by_case.values())
    passed = sum(not r["problems"] for v in by_case.values() for r in v)
    for cid, runs in by_case.items():
        ok = sum(not r["problems"] for r in runs)
        mark = "✓" if ok == len(runs) else "✗"
        print(f"  {mark} {cid}  {ok}/{len(runs)}")
        for r in runs:
            if r["problems"]:
                print(f"      问题：{'；'.join(r['problems'])}\n      过程：{r['trace'][:300]}")
    rate = passed / total
    return {"metric": f"{label} {passed}/{total}（{rate:.0%}）", "ok": rate >= THRESHOLDS[label_key(label)], "rate": rate}


def label_key(label):
    return {"动作": "actions", "纪要": "minutes", "问答": "qa"}[label]


TIME_FIELDS = {"start", "end", "due", "remind_at"}


def stage_actions(args):
    from nota import agent

    suite = json.loads((CASES / "actions.json").read_text())
    now, tz = datetime.fromisoformat(suite["now"]), suite["tz"]
    local = lambda iso: datetime.fromisoformat(iso).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")

    def effective(step):
        a = dict(step["args"])
        if step["tool"] == "create_calendar_event" and a.get("start") and a.get("alarm_minutes_before") is not None:
            a["remind_at"] = (datetime.fromisoformat(a["start"]) - timedelta(minutes=a["alarm_minutes_before"])).isoformat()
        return a

    def matches(step, expect):
        if step["tool"] != expect["tool"] or "error" in step["result"]:
            return False
        a = effective(step)
        for k, v in (expect.get("args") or {}).items():
            got = a.get(k)
            if got is None:
                return False
            if k in TIME_FIELDS:
                try:
                    if not local(got).startswith(v):
                        return False
                except ValueError:
                    return False
            elif not re.search(v, str(got)):
                return False
        return True

    def one(c):
        user = Sandbox.user(tz)
        for h in c.get("history", []):
            Sandbox.seed(user, now - timedelta(days=h["days_ago"]), h["text"], ["我"], [("我", h["text"])])
        conv = {"started_at": now, "speakers": [{"label": "我", "is_owner": True}], "lines": [{"offset": 0, "speaker": "我", "text": c["input"]}]}
        steps = []
        try:
            agent.process(user, conv, "", "eval", lambda t, a, r: steps.append({"tool": t, "args": a, "result": r}))
        except Exception as e:  # noqa: BLE001 — a failed case is reported, the run goes on
            return {"problems": [f"运行失败：{e}"], "trace": "", "duplicate": False}
        problems = [f"缺 {e['tool']} {json.dumps(e.get('args', {}), ensure_ascii=False)}" for e in c["expect"] if not any(matches(s, e) for s in steps)]
        problems += [f"不应调用 {t}" for t in c.get("forbid", []) if any(s["tool"] == t for s in steps)]
        todos = [s["args"].get("title", "") for s in steps if s["tool"] == "create_todo"]
        events = [s["args"].get("title", "") for s in steps if s["tool"] == "create_calendar_event"]
        duplicate = any(len(set(t) & set(e)) >= 3 for t in todos for e in events)
        trace = " ".join(f"{s['tool']}{json.dumps(s['args'], ensure_ascii=False)}" for s in steps) or "无调用"
        return {"problems": problems, "trace": trace, "duplicate": duplicate}

    by_case = run_cases(suite["cases"], one, args.repeat)
    out = report(by_case, "动作")
    dups = sum(r["duplicate"] for v in by_case.values() for r in v)
    print(f"  同一件事既建日程又建待办（重复动作，扣分项，不计入通过率）：{dups} 次")
    out["metric"] += f"，重复动作 {dups} 次"
    return out


def stage_minutes(args):
    from nota import conversations

    suite = json.loads((CASES / "minutes.json").read_text())

    def one(c):
        conv = {"speakers": [{"label": s, "is_owner": o} for s, o in c["speakers"]],
                "lines": [{"offset": i * 15, "speaker": s, "text": t} for i, (s, t) in enumerate(c["lines"])]}
        try:
            out = conversations.summarize(conv)
        except Exception as e:  # noqa: BLE001 — a failed case is reported, the run goes on
            return {"problems": [f"运行失败：{e}"], "trace": ""}
        facts = out["facts"]
        problems = [f"缺事实 /{f['text']}/ 由 {f['speaker']} 说" for f in c["facts"]
                    if not any(re.search(f["text"], x["text"]) and x["speaker_label"] == f["speaker"] for x in facts)]
        source = numbers_in("".join(t for _, t in c["lines"]))
        invented = {n for x in facts for n in numbers_in(x["text"])} - source
        if invented:
            problems.append(f"原文没有的数字：{sorted(invented)}")
        if "max_facts" in c and len(facts) > c["max_facts"]:
            problems.append(f"闲聊记了 {len(facts)} 条事实")
        trace = "；".join(f"{x['speaker_label']}:{x['text']}" for x in facts)
        return {"problems": problems, "trace": trace}

    return report(run_cases(suite["cases"], one, args.repeat), "纪要")


def stage_qa(args):
    from nota import agent

    suite = json.loads((CASES / "qa.json").read_text())
    now, tz = datetime.fromisoformat(suite["now"]), suite["tz"]

    def one(c):
        user = Sandbox.user(tz)
        keys = {Sandbox.seed(user, now - timedelta(days=h["days_ago"]), h["title"], h["speakers"], [tuple(f) for f in h["facts"]]): h["key"] for h in c["history"]}
        steps = []
        try:
            answer = agent.ask(user, c["question"], now, lambda t, a, r: steps.append(t))
        except Exception as e:  # noqa: BLE001 — a failed case is reported, the run goes on
            return {"problems": [f"运行失败：{e}"], "trace": ""}
        cited = [keys.get(x["conversation_id"], "?") for x in answer["citations"]]
        problems = [f"回答不含 /{p}/" for p in c["reply"] if not re.search(p, answer["text"])]
        if c.get("forbid_reply") and re.search(c["forbid_reply"], answer["text"]):
            problems.append(f"回答含 /{c['forbid_reply']}/")
        problems += [f"没引用 {k}" for k in c["cite"] if k not in cited]
        if c.get("only") and set(cited) - set(c["cite"]):
            problems.append(f"引用了无关对话 {sorted(set(cited) - set(c['cite']))}")
        if "find_notes" not in steps:
            problems.append("没有查纪要")
        return {"problems": problems, "trace": f"{steps} 出处 {cited} 答：{answer['text']}".replace("\n", " ")}

    return report(run_cases(suite["cases"], one, args.repeat), "问答")


STAGES = {"asr": stage_asr, "speaker": stage_speaker, "actions": stage_actions, "minutes": stage_minutes, "qa": stage_qa}
TITLES = {"asr": "转写", "speaker": "声纹", "actions": "动作", "minutes": "纪要", "qa": "问答"}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("stages", nargs="*", metavar="stage", help=f"any of {', '.join(STAGES)} (default: all)")
    p.add_argument("--repeat", type=int, default=1, help="run each model-backed case N times (T5 suggests 3)")
    args = p.parse_args()
    unknown = set(args.stages) - set(STAGES)
    if unknown:
        p.error(f"unknown stage {sorted(unknown)}")
    args.stages = args.stages or list(STAGES)
    load_env()
    summary = {}
    with Sandbox():
        for name in args.stages:
            print(f"\n== {TITLES[name]} ==")
            started = time.perf_counter()
            summary[name] = STAGES[name](args)
            print(f"  （{time.perf_counter() - started:.0f}s）")
    print("\n== 汇总 ==")
    for name, r in summary.items():
        print(f"  {'✓' if r['ok'] else '✗'} {TITLES[name]}：{r['metric']}")
    sys.exit(0 if all(r["ok"] for r in summary.values()) else 1)


if __name__ == "__main__":
    main()
