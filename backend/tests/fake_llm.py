"""Local OpenAI-compatible model double, reached through LLM_BASE_URL.

Which call it is follows from the request: tools offered (agent vs ask) or the system prompt
(meeting-minutes vs profile JSON). Behaviour is driven by markers in the user's material:
  评审会   -> agent calls create_calendar_event + create_todo + share_message
  #fail / 失败案例 -> HTTP 500 on every call for that material (the second one can be spoken)
  #slow    -> the summary call takes SLOW_SECONDS
  #busy    -> like DeepSeek under load: 200 headers at once, then only keep-alive blank lines, never a body
  #null    -> 200 with the body `null`
  #due     -> the agent creates a todo whose due is a bare date
  #loop    -> the agent asks for get_profile on every turn, each turn taking LOOP_TURN_SECONDS
Like DeepSeek thinking mode, follow-ups that drop reasoning_content are rejected.
"""
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SLOW_SECONDS = 8
LOOP_TURN_SECONDS = 1
BUSY = object()
LINE = re.compile(r"^\[(\d+):(\d+)\] ([^：]+)：(.*)$", re.M)


def _tool_turn(calls):
    return {
        "role": "assistant",
        "content": None,
        "reasoning_content": "thinking",
        "tool_calls": [
            {"id": f"call_{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}
            for i, (n, a) in enumerate(calls)
        ],
    }


def _summary(material):
    lines = LINE.findall(material)
    facts = [
        {"text": text, "speaker_label": label, "offset_seconds": int(m) * 60 + int(s)}
        for m, s, label, text in lines
    ]
    first = lines[0][3] if lines else material
    return {"title": first[:12], "overview": "纪要：" + first, "facts": facts}


def _agent(messages, material):
    if not any(m["role"] == "tool" for m in messages):
        if "#due" in material:
            return _tool_turn([("create_todo", {"title": "交周报", "due": "2026-11-06"})])
        if "评审会" in material:
            return _tool_turn([
                ("create_calendar_event", {"title": "预算评审会", "start": "2026-11-11T15:00:00+08:00", "end": "2026-11-11T16:00:00+08:00", "alarm_minutes_before": 1440}),
                ("create_todo", {"title": "准备报表"}),
                ("share_message", {"to": "张总", "text": "下周三下午三点开预算评审会"}),
            ])
        return {"role": "assistant", "content": "无需操作"}
    return {"role": "assistant", "content": "已处理"}


def _ask(messages, question):
    results = [m for m in messages if m["role"] == "tool"]
    if not results:
        return _tool_turn([("find_notes", {"keyword": question})])
    found = json.loads(results[0]["content"])
    notes = found.get("results", [])
    if not notes:
        return {"role": "assistant", "content": "没有找到相关记录。"}
    return {"role": "assistant", "content": f"找到了：{notes[0]['title']} [{notes[0]['ref']}]"}


def respond(body):
    messages = body["messages"]
    system = messages[0]["content"]
    material = "\n".join(m.get("content") or "" for m in messages if m["role"] == "user")
    if "#fail" in material or "失败案例" in material:
        return 500, {"error": {"message": "fake failure"}}
    if "#busy" in material:
        return 200, BUSY
    if "#null" in material:
        return 200, None
    if any(m["role"] == "assistant" and m.get("tool_calls") and not m.get("reasoning_content") for m in messages):
        return 400, {"error": {"message": "reasoning_content must be passed back"}}
    tools = {t["function"]["name"] for t in body.get("tools", [])}
    if "#loop" in material and tools:
        time.sleep(LOOP_TURN_SECONDS)
        return 200, {"choices": [{"index": 0, "message": _tool_turn([("get_profile", {})]), "finish_reason": "tool_calls"}]}
    if "create_todo" in tools:
        message = _agent(messages, material)
    elif "find_notes" in tools:
        message = _ask(messages, material.strip())
    elif "纪要" in system:
        if "#slow" in material:
            time.sleep(SLOW_SECONDS)
        message = {"role": "assistant", "content": json.dumps(_summary(material), ensure_ascii=False)}
    elif "画像" in system:
        message = {"role": "assistant", "content": json.dumps({"items": [{"category": "person", "content": "张总：预算评审会参会人"}], "deletes": []}, ensure_ascii=False)}
    else:
        return 400, {"error": {"message": "unrecognised call"}}
    return 200, {"choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        status, payload = respond(body)
        if payload is BUSY:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            try:
                for _ in range(600):
                    self.wfile.write(b"\n")
                    self.wfile.flush()
                    time.sleep(0.5)
            except OSError:
                pass
            return
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def start():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"
