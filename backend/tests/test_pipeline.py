"""Audio and text through the real pipeline (VAD, SenseVoice, CAM++, Postgres queue) with the model faked."""
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from tests.support import Client, Service, Stream, new_user, silence, speech, split, wav_bytes

OWNER_ENROL = "我是这台设备的主人，现在录一段声音用来注册声纹，请记住我说话的声音特点。"
OWNER_MEETING = "下周三下午三点和张总开预算评审会，提前一天提醒我准备报表。"
GUEST = "好的，我这边会准备好合同，周五之前发给你。"
OWNER_LATER = "那就这么定了，周五见。"


@pytest.fixture(scope="module")
def svc(db, llm):
    s = Service(db, llm).start()
    yield s
    s.stop()


def enrol(c: Client):
    voice = np.concatenate([speech(OWNER_ENROL), silence(0.5), speech(OWNER_ENROL)])
    return c.http.post("/api/voiceprint", content=wav_bytes(voice), headers={"content-type": "audio/wav"})


def test_audio_is_split_at_long_silence_and_the_owner_is_recognised_by_voiceprint(svc):
    user = new_user()
    c = Client(svc.url, user)
    stream = Stream(svc.url, user)
    assert enrol(c).json() == {"enrolled": True}
    audio = np.concatenate([
        speech(OWNER_MEETING), silence(125),
        speech(GUEST, "Meijia"), silence(1), speech(OWNER_LATER),
    ])
    rid = c.upload(audio, 30)
    stream.recording_status(rid, "done")

    conversations = sorted(c.state()["conversations"], key=lambda x: x["started_at"])
    assert len(conversations) == 2
    first, second = conversations
    assert [s["is_owner"] for s in first["speakers"]] == [True]
    assert sorted(s["is_owner"] for s in second["speakers"]) == [False, True]
    owner_label = first["speakers"][0]["label"]
    assert any("评审会" in f["text"] and f["speaker_label"] == owner_label for f in first["facts"])
    guest_label = next(s["label"] for s in second["speakers"] if not s["is_owner"])
    assert any("合同" in f["text"] and f["speaker_label"] == guest_label for f in second["facts"])


def test_without_an_enrolled_voiceprint_nobody_is_marked_as_owner(svc):
    user = new_user()
    c = Client(svc.url, user)
    stream = Stream(svc.url, user)
    rid = c.upload(speech(OWNER_MEETING), 30)
    stream.recording_status(rid, "done")

    conversation = c.state()["conversations"][0]
    assert all(not s["is_owner"] for s in conversation["speakers"])
    assert c.state()["voiceprint_enrolled"] is False


def test_voiceprint_too_short_is_rejected(svc):
    c = Client(svc.url, new_user())
    r = c.http.post("/api/voiceprint", content=wav_bytes(speech("你好")), headers={"content-type": "audio/wav"})
    assert r.status_code == 400


def test_chunk_not_in_the_contract_audio_format_is_rejected(svc):
    c = Client(svc.url, new_user())
    rid = c.start_recording()["recording_id"]
    stereo_44k = wav_bytes(np.zeros(44100 * 2, dtype=np.int16), rate=44100, channels=2)
    assert c.put_chunk(rid, 0, stereo_44k).status_code == 400
    assert c.put_chunk(rid, 0, wav_bytes(silence(61))).status_code == 400


def test_text_note_streams_status_agent_steps_and_resulting_actions(svc):
    user = new_user()
    c = Client(svc.url, user)
    stream = Stream(svc.url, user)
    rid = c.text_note("下周三下午三点和张总开预算评审会，提前一天提醒我，再告诉张总一声")
    stream.recording_status(rid, "done")

    statuses = [d["status"] for n, d in stream.events if n == "recording" and d["recording_id"] == rid]
    assert statuses.index("summarizing") < statuses.index("done")
    steps = [d["tool"] for n, d in stream.events if n == "step" and d["recording_id"] == rid]
    assert set(steps) >= {"create_calendar_event", "create_todo", "share_message"}
    assert any(n == "conversation" for n, _ in stream.events)
    assert any(n == "quota" for n, _ in stream.events)

    state = c.state()
    by_type = {a["type"]: a for a in state["actions"]}
    event = by_type["create_calendar_event"]
    assert (event["status"], event["needs_confirmation"]) == ("pending", False)
    assert event["args"]["alarm_minutes_before"] == 1440
    share = by_type["share_message"]
    assert (share["status"], share["needs_confirmation"]) == ("pending_confirmation", True)
    assert [t["text"] for t in state["todos"]] == ["准备报表"]
    assert [t["text"] for t in state["conversations"][0]["todos"]] == ["准备报表"]


def test_phone_runs_actions_once_confirmation_gates_sharing_and_undo_deletes_the_event(svc):
    user = new_user()
    c = Client(svc.url, user)
    stream = Stream(svc.url, user)
    stream.recording_status(c.text_note("下周三下午三点和张总开预算评审会"), "done")
    actions = {a["type"]: a for a in c.state()["actions"]}
    event, share = actions["create_calendar_event"], actions["share_message"]

    pending = c.http.get("/api/actions", params={"status": "pending"}).json()
    first = c.http.post(f"/api/actions/{event['id']}/result", json={"ok": True, "system_id": "EK-1"})
    second = c.http.post(f"/api/actions/{event['id']}/result", json={"ok": False, "error": "late duplicate"})
    c.http.post(f"/api/actions/{share['id']}/confirm").raise_for_status()
    pending_after_confirm = c.http.get("/api/actions", params={"status": "pending"}).json()
    undo = c.http.post(f"/api/actions/{event['id']}/undo").json()

    assert [a["id"] for a in pending] == [event["id"]]
    assert first.status_code == 200 and second.status_code == 200
    final = {a["id"]: a for a in c.state()["actions"]}
    assert final[event["id"]]["status"] == "done" and final[event["id"]]["result"]["system_id"] == "EK-1"
    assert share["id"] in [a["id"] for a in pending_after_confirm]
    assert (undo["type"], undo["args"], undo["status"]) == ("delete_calendar_event", {"system_event_id": "EK-1"}, "pending")


def test_rejected_confirmation_is_never_handed_to_the_phone(svc):
    c = Client(svc.url, new_user())
    stream = Stream(svc.url, c.user)
    stream.recording_status(c.text_note("下周三下午三点和张总开预算评审会"), "done")
    share = next(a for a in c.state()["actions"] if a["type"] == "share_message")

    c.http.post(f"/api/actions/{share['id']}/reject").raise_for_status()
    confirm_after_reject = c.http.post(f"/api/actions/{share['id']}/confirm")

    assert confirm_after_reject.status_code == 409
    assert share["id"] not in [a["id"] for a in c.http.get("/api/actions", params={"status": "pending"}).json()]


def test_another_users_actions_are_invisible_and_untouchable(svc):
    owner = Client(svc.url, new_user())
    stream = Stream(svc.url, owner.user)
    stream.recording_status(owner.text_note("下周三下午三点和张总开预算评审会"), "done")
    action = owner.state()["actions"][0]
    other = Client(svc.url, new_user())

    assert other.http.get("/api/actions", params={"status": "pending"}).json() == []
    assert other.http.post(f"/api/actions/{action['id']}/result", json={"ok": True}).status_code == 404


def test_ask_answers_from_history_with_citation_and_says_when_nothing_is_found(svc):
    c = Client(svc.url, new_user())
    stream = Stream(svc.url, c.user)
    rid = c.text_note("下周三下午三点和张总开预算评审会")
    stream.recording_status(rid, "done")
    conversation = c.state()["conversations"][0]

    status, events = c.ask("预算")
    _, missing = c.ask("日料店")

    assert status == 200
    assert [d["tool"] for n, d in events if n == "step"] == ["find_notes"]
    answer = next(d for n, d in events if n == "answer")
    assert [x["conversation_id"] for x in answer["citations"]] == [conversation["id"]]
    assert next(d for n, d in missing if n == "answer") == {"text": "没有找到相关记录。", "citations": []}


def test_profile_is_learned_from_conversations_and_user_editable_within_200_chars(svc):
    c = Client(svc.url, new_user())
    stream = Stream(svc.url, c.user)
    stream.recording_status(c.text_note("下周三下午三点和张总开预算评审会"), "done")
    item = c.http.get("/api/profile").json()[0]

    edited = c.http.put(f"/api/profile/{item['id']}", json={"category": "person", "content": "张总：张伟，客户方财务负责人"})
    too_long = c.http.put(f"/api/profile/{item['id']}", json={"category": "person", "content": "长" * 201})
    deleted = c.http.delete(f"/api/profile/{item['id']}")

    assert item["content"] == "张总：预算评审会参会人"
    assert edited.status_code == 200 and edited.json()["content"] == "张总：张伟，客户方财务负责人"
    assert too_long.status_code == 400
    assert deleted.status_code in (200, 204)
    assert c.http.get("/api/profile").json() == []


def test_state_has_the_contract_shape(svc):
    state = Client(svc.url, new_user()).state()
    assert set(state) == {"user", "quota", "conversations", "todos", "actions", "profile", "voiceprint_enrolled"}
    assert state["user"]["timezone"] == "Asia/Shanghai" and state["user"]["plan"] == "free"
    assert set(state["quota"]) == {"minutes", "recordings_today", "asks"}
    assert state["quota"]["minutes"]["limit"] == 300 and state["quota"]["asks"]["limit"] == 30
    uuid.UUID(state["user"]["id"])


def test_conversation_closes_after_120s_of_silence_while_the_recording_is_still_open(svc):
    user = new_user()
    c = Client(svc.url, user)
    stream = Stream(svc.url, user)
    rid = c.start_recording()["recording_id"]
    # 3-second chunks cut the sentence mid-word; the transcript must still be whole
    first = split(np.concatenate([speech(OWNER_MEETING), silence(125)]), 3)
    for seq, data in enumerate(first):
        c.put_chunk(rid, seq, data).raise_for_status()

    closed = stream.wait_for(lambda n, d: n == "conversation" and d["recording_id"] == rid and d["facts"], timeout=180)
    action = stream.wait_for(lambda n, d: n == "action" and d["type"] == "create_calendar_event")
    finished_before = any(n == "recording" and d["recording_id"] == rid and d["status"] == "done" for n, d in stream.events)

    rest = split(np.concatenate([speech(GUEST, "Meijia"), silence(1)]), 3)
    for seq, data in enumerate(rest, len(first)):
        c.put_chunk(rid, seq, data).raise_for_status()
    assert c.finish(rid, len(first) + len(rest)) == {"missing": []}
    stream.recording_status(rid, "done")

    assert not finished_before
    assert any("评审会" in f["text"] for f in closed["facts"])
    assert action["status"] == "pending"
    conversations = sorted((x for x in c.state()["conversations"] if x["recording_id"] == rid), key=lambda x: x["started_at"])
    assert len(conversations) == 2
    assert conversations[0]["id"] == closed["id"]
    assert any("合同" in f["text"] for f in conversations[1]["facts"])


def test_todo_due_given_as_a_bare_date_means_the_end_of_that_day_in_the_users_timezone(svc):
    c = Client(svc.url, new_user())
    stream = Stream(svc.url, c.user)
    stream.recording_status(c.text_note("#due 周五之前交周报"), "done")

    due = datetime.fromisoformat(c.state()["todos"][0]["due_at"])
    assert due == datetime(2026, 11, 6, 23, 59, tzinfo=ZoneInfo("Asia/Shanghai"))
