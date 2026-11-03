"""Load test for the three ledgers against the real service: two API processes and two worker
processes on one real Postgres, model calls going to a local fake through LLM_BASE_URL."""
import random
import uuid
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from tests.support import Client, Service, Stream, new_user, silence, speech, split

# Tiny minute budgets so a few seconds of real speech can exhaust them; everything else is the default.
ENV = {
    "QUOTA_FREE_MINUTES_PER_MONTH": "0.15",
    "QUOTA_PRO_MINUTES_PER_24H": "0.15",
    "JOB_LEASE_SECONDS": "3",
    "JOB_MAX_ATTEMPTS": "2",
    "JOB_RETRY_BASE_SECONDS": "1",
}
SENTENCE = "下周三下午三点和张总开预算评审会，报价控制在十八万以内。"


@pytest.fixture(scope="module")
def svc(db, llm):
    s = Service(db, llm, ENV).start()
    yield s
    s.stop()


def parallel(n, fn):
    with ThreadPoolExecutor(n) as pool:
        return list(pool.map(fn, range(n)))


def speech_seconds(samples):
    return len(samples) / 16000


def test_free_user_50_concurrent_recordings_exactly_10_get_ai_and_all_are_saved(svc):
    user = new_user()
    results = parallel(50, lambda i: Client(svc.url, user).start_recording())

    assert sum(r["ai"] == "enabled" for r in results) == 10
    assert sum(r["ai"] == "paused_quota" for r in results) == 40
    state = Client(svc.url, user).state()
    assert state["quota"]["recordings_today"] == {"used": 10, "limit": 10, "day": state["quota"]["recordings_today"]["day"]}


def test_repeated_start_and_duplicate_out_of_order_chunks_are_processed_and_charged_once(svc):
    user = new_user()
    c = Client(svc.url, user)
    stream = Stream(svc.url, user)
    rid = str(uuid.uuid4())
    audio = np.concatenate([speech(SENTENCE), silence(1)])
    chunks = split(audio, 2)

    starts = parallel(20, lambda i: Client(svc.url, user).start_recording(rid))
    uploads = [(seq, data) for seq, data in enumerate(chunks) for _ in range(4)]
    random.Random(7).shuffle(uploads)
    replies = parallel(len(uploads), lambda i: Client(svc.url, user).put_chunk(rid, *uploads[i]).json())
    assert c.finish(rid, len(chunks)) == {"missing": []}
    stream.recording_status(rid, "done")
    again = c.put_chunk(rid, 0, chunks[0]).json()
    c.finish(rid, len(chunks))

    assert {s["ai"] for s in starts} == {"enabled"}
    assert sum(not r["duplicate"] for r in replies) == len(chunks)
    assert again == {"seq": 0, "duplicate": True}
    state = c.state()
    assert state["quota"]["recordings_today"]["used"] == 1
    assert 0 < state["quota"]["minutes"]["used"] <= round(speech_seconds(audio) / 60, 2)
    assert len(state["conversations"]) == 1


def test_finish_reports_missing_chunks_until_they_arrive(svc):
    c = Client(svc.url, new_user())
    rid = c.start_recording()["recording_id"]
    chunks = split(speech(SENTENCE), 2)
    for seq in range(0, len(chunks), 2):
        c.put_chunk(rid, seq, chunks[seq]).raise_for_status()

    first = c.finish(rid, len(chunks))
    for seq in first["missing"]:
        c.put_chunk(rid, seq, chunks[seq]).raise_for_status()

    assert first["missing"] == list(range(1, len(chunks), 2))
    assert c.finish(rid, len(chunks)) == {"missing": []}


def test_same_text_note_submitted_concurrently_is_one_recording_charged_once(svc):
    user = new_user()
    key = str(uuid.uuid4())
    ids = parallel(20, lambda i: Client(svc.url, user).text_note("记一下：周五交周报", key))

    assert len(set(ids)) == 1
    assert Client(svc.url, user).state()["quota"]["recordings_today"]["used"] == 1


def test_minutes_exhausted_keeps_recording_and_only_pauses_ai(svc):
    user = new_user()
    c = Client(svc.url, user)
    stream = Stream(svc.url, user)
    first = c.upload(speech(SENTENCE), 5)
    stream.recording_status(first, "done")
    second = c.upload(speech(SENTENCE), 5)
    stream.recording_status(second, "paused_quota")

    state = c.state()
    assert len(state["conversations"]) == 1
    assert state["quota"]["minutes"]["used"] <= state["quota"]["minutes"]["limit"]


def test_pro_has_no_monthly_limits_but_a_rolling_fair_use_cap(svc):
    user = new_user()
    c = Client(svc.url, user)
    c.state()
    svc.set_plan(user, "pro")
    stream = Stream(svc.url, user)
    first = c.upload(speech(SENTENCE), 5)
    stream.recording_status(first, "done")
    second = c.upload(speech(SENTENCE), 5)
    stream.recording_status(second, "paused_quota")

    quota = c.state()["quota"]
    assert quota["minutes"]["limit"] is None and quota["recordings_today"]["limit"] is None and quota["asks"]["limit"] is None


def test_asks_monthly_limit_holds_under_concurrency_and_replays_by_key(svc):
    user = new_user()
    c = Client(svc.url, user)
    c.text_note("周五和张总开预算评审会")
    results = parallel(40, lambda i: Client(svc.url, user).ask("预算"))
    key = str(uuid.uuid4())
    first = c.ask("预算", key)
    replay = c.ask("预算", key)

    assert sum(status == 200 for status, _ in results) == 30
    assert all(body == {"error": "ask_quota"} for status, body in results if status == 429)
    assert first[0] == 429 and replay[0] == 429
    assert c.state()["quota"]["asks"]["used"] == 30


def test_ask_replays_same_answer_for_same_key(svc):
    c = Client(svc.url, new_user())
    key = str(uuid.uuid4())
    first = c.ask("预算", key)
    again = c.ask("预算", key)

    answer = lambda events: next(d for n, d in events if n == "answer")
    assert answer(again) == answer(first)
    assert c.state()["quota"]["asks"]["used"] == 1


def test_failed_processing_and_failed_ask_refund_every_ledger(svc):
    user = new_user()
    c = Client(svc.url, user)
    stream = Stream(svc.url, user)
    text_rid = c.text_note("#fail 整理一下")
    audio_rid = c.upload(speech("这是一条失败案例，请不要处理。"), 5)
    stream.recording_status(text_rid, "failed")
    stream.recording_status(audio_rid, "failed")
    status, events = c.ask("#fail 问一下")

    assert status == 200 and any(n == "error" for n, _ in events)
    quota = c.state()["quota"]
    assert quota["recordings_today"]["used"] == 0
    assert quota["minutes"]["used"] == 0
    assert quota["asks"]["used"] == 0


def test_job_of_a_killed_worker_is_reclaimed_and_charged_once(svc):
    user = new_user()
    c = Client(svc.url, user)
    stream = Stream(svc.url, user)
    rid = c.upload(speech("这是一条慢速案例，记得周五交报告。"), 5)
    stream.recording_status(rid, "summarizing")
    svc.stop_workers()
    svc.start_workers(1)
    stream.recording_status(rid, "done")

    state = c.state()
    assert len(state["conversations"]) == 1
    assert state["quota"]["recordings_today"]["used"] == 1
    assert 0 < state["quota"]["minutes"]["used"] <= 0.15


def test_day_and_month_reset_at_midnight_in_the_users_timezone_and_paused_work_resumes(svc):
    daily_user, minutes_user = new_user(), new_user()
    # 23:59 on 30 Nov in Asia/Shanghai; the next start is 00:01 on 1 Dec there, still 30 Nov in UTC.
    svc.restart(now="2026-11-30T15:59:00Z")
    daily, minutes = Client(svc.url, daily_user), Client(svc.url, minutes_user)
    daily_stream, minutes_stream = Stream(svc.url, daily_user), Stream(svc.url, minutes_user)
    note_ids = [daily.text_note(f"第 {i} 条") for i in range(11)]
    paused_note = daily_stream.wait_for(lambda n, d: n == "recording" and d["status"] == "paused_quota")["recording_id"]
    first = minutes.upload(speech(SENTENCE), 5)
    minutes_stream.recording_status(first, "done")
    paused_audio = minutes.upload(speech(SENTENCE), 5)
    minutes_stream.recording_status(paused_audio, "paused_quota")
    before = daily.state()["quota"]["recordings_today"]

    svc.stop()
    svc.start_api(now="2026-11-30T16:01:00Z")
    daily_stream, minutes_stream = Stream(svc.url, daily_user), Stream(svc.url, minutes_user)
    svc.start_workers(2, now="2026-11-30T16:01:00Z")
    daily_stream.recording_status(paused_note, "done")
    minutes_stream.recording_status(paused_audio, "done")
    after_daily = daily.state()["quota"]["recordings_today"]
    after_minutes = minutes.state()["quota"]["minutes"]

    assert paused_note in note_ids
    assert before == {"used": 10, "limit": 10, "day": "2026-11-30"}
    assert after_daily == {"used": 1, "limit": 10, "day": "2026-12-01"}
    assert after_minutes["period"] == "2026-12" and 0 < after_minutes["used"] <= 0.15
