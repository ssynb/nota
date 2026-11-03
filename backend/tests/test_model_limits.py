"""A model call or agent run that does not finish is cut off by a deadline and goes down the normal
failure path: retried, reported on the event stream, and refunded once attempts run out."""
import time

import pytest

from tests.support import Client, Service, Stream, new_user

ENV = {
    "LLM_CALL_TIMEOUT_SECONDS": "3",
    "AGENT_TIMEOUT_SECONDS": "5",
    "JOB_MAX_ATTEMPTS": "2",
    "JOB_RETRY_BASE_SECONDS": "1",
}
BOUND = 40  # seconds: two attempts of a few seconds each plus backoff, nowhere near minutes


@pytest.fixture(scope="module")
def svc(db, llm):
    s = Service(db, llm, ENV).start()
    yield s
    s.stop()


def run_until_failed(svc, text):
    c = Client(svc.url, new_user())
    stream = Stream(svc.url, c.user)
    started = time.monotonic()
    rid = c.text_note(text)
    failed = stream.recording_status(rid, "failed", timeout=BOUND)
    elapsed = time.monotonic() - started
    notices = [d for n, d in stream.events if n == "recording" and d["recording_id"] == rid and d.get("error") and d["status"] != "failed"]
    return c, failed, notices, elapsed


def test_model_call_kept_alive_without_an_answer_is_cut_off_retried_shown_and_refunded(svc):
    c, failed, notices, elapsed = run_until_failed(svc, "#busy 整理一下")

    assert elapsed < BOUND
    assert "超时" in failed["error"]
    assert notices and "超时" in notices[0]["error"], "the retry is visible on the stream before the final failure"
    assert c.state()["quota"]["recordings_today"]["used"] == 0


def test_agent_run_over_its_total_time_limit_fails_even_if_each_call_is_quick(svc):
    c, failed, _, elapsed = run_until_failed(svc, "#loop 下周三下午三点开会")

    assert elapsed < BOUND
    assert "超时" in failed["error"]
    assert c.state()["quota"]["recordings_today"]["used"] == 0


def test_unusable_model_response_is_reported_as_such(svc):
    _, failed, _, _ = run_until_failed(svc, "#null 整理一下")
    assert "无效" in failed["error"]


def test_ask_that_never_gets_an_answer_reports_an_error_in_time_and_is_refunded(svc):
    c = Client(svc.url, new_user())
    started = time.monotonic()
    status, events = c.ask("#busy 张总的报价")
    elapsed = time.monotonic() - started

    assert status == 200 and elapsed < 15
    error = next(d for n, d in events if n == "error")
    assert "超时" in error["message"]
    assert c.state()["quota"]["asks"]["used"] == 0
