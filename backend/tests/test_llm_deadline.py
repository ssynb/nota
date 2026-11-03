"""The model call's time limit is a hard wall-clock limit on the whole call: headers, body and the
end of the response all have to arrive before it, or the call fails as a timeout."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nota import config, llm

LIMIT = 1.0
SLACK = 0.3  # the call must give up within this much after the limit
BODY = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": "late success"}}]}).encode()

# path -> (seconds before headers, seconds after headers before the body, seconds after the body before EOF)
TIMINGS = {"/slow-body": (0.7, 0.7, 0), "/late-eof": (0, 0.3, 0.9), "/fast": (0, 0, 0)}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # body ends when the connection closes

    def do_POST(self):
        self.rfile.read(int(self.headers["content-length"]))
        before_headers, before_body, before_eof = TIMINGS[self.path.removesuffix("/chat/completions")]
        time.sleep(before_headers)
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.flush()
        time.sleep(before_body)
        self.wfile.write(BODY)
        self.wfile.flush()
        time.sleep(before_eof)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def server():
    s = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_address[1]}"
    s.shutdown()


def call(monkeypatch, server, path):
    monkeypatch.setattr(config, "LLM_BASE_URL", server + path)
    monkeypatch.setattr(config, "LLM_CALL_TIMEOUT_SECONDS", LIMIT)
    monkeypatch.setattr(config, "LLM_API_KEY", "test")
    started = time.monotonic()
    try:
        return llm.chat([{"role": "user", "content": "hi"}]), time.monotonic() - started
    except llm.LLMError as e:
        return e, time.monotonic() - started


@pytest.mark.parametrize("path", ["/slow-body", "/late-eof"])
def test_response_finishing_after_the_limit_is_a_timeout_raised_at_the_limit(monkeypatch, server, path):
    outcome, elapsed = call(monkeypatch, server, path)

    assert isinstance(outcome, llm.LLMError) and "超时" in str(outcome)
    assert elapsed < LIMIT + SLACK


def test_response_within_the_limit_is_returned(monkeypatch, server):
    outcome, _ = call(monkeypatch, server, "/fast")
    assert outcome == {"role": "assistant", "content": "late success"}
