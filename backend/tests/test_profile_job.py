"""Learning the profile is its own job: once minutes and actions are delivered the recording is done
and charged, whatever happens to the profile update afterwards."""
import time

import pytest

from tests.support import Client, Service, Stream, new_user

ENV = {"JOB_MAX_ATTEMPTS": "2", "JOB_RETRY_BASE_SECONDS": "1"}
TEXT = "下周三下午三点和张总开预算评审会"


@pytest.fixture(scope="module")
def svc(db, llm):
    s = Service(db, llm, ENV).start()
    yield s
    s.stop()


def profile_eventually(c: Client, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        items = c.http.get("/api/profile").json()
        if items:
            return items
        time.sleep(0.3)
    return []


def test_profile_failure_does_not_fail_or_refund_a_delivered_recording_and_is_retried(svc):
    c = Client(svc.url, new_user())
    stream = Stream(svc.url, c.user)
    rid = c.text_note(f"#profile503 {TEXT}")
    stream.recording_status(rid, "done")
    notice = stream.wait_for(lambda n, d: n == "recording" and d["recording_id"] == rid and "画像" in (d.get("error") or ""))
    items = profile_eventually(c)

    state = c.state()
    assert "稍后重试" in notice["error"]
    assert not any(n == "recording" and d["recording_id"] == rid and d["status"] == "failed" for n, d in stream.events)
    assert state["quota"]["recordings_today"]["used"] == 1
    assert any(a["type"] == "create_calendar_event" for a in state["actions"])
    assert [i["content"] for i in items] == ["张总：预算评审会参会人"]


def test_profile_that_keeps_failing_leaves_the_recording_done_and_charged(svc):
    c = Client(svc.url, new_user())
    stream = Stream(svc.url, c.user)
    rid = c.text_note(f"#profilefail {TEXT}")
    stream.recording_status(rid, "done")
    stream.wait_for(lambda n, d: n == "recording" and d["recording_id"] == rid and "画像更新失败" in (d.get("error") or "") and "稍后重试" not in d["error"])

    assert not any(n == "recording" and d["recording_id"] == rid and d["status"] == "failed" for n, d in stream.events)
    assert c.state()["quota"]["recordings_today"]["used"] == 1
    assert c.http.get("/api/profile").json() == []
