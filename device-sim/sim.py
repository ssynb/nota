#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# ///
"""Device simulator: uploads a WAV the way the phone relays the device's recording (docs/contract.md),
optionally misbehaving like a real link — drops (network loss,补传 after finish), duplicates and
reordering — and prints evidence that the server processed and charged the recording exactly once.

  uv run device-sim/sim.py speech.wav --chunk 10 --drop 1,3 --dup 3 --shuffle
"""
import argparse
import json
import random
import sys
import urllib.request
import uuid
import wave
from datetime import UTC, datetime
from io import BytesIO

RATE = 16000


class Api:
    def __init__(self, base, user, tz):
        self.base, self.headers = base.rstrip("/"), {"x-user-id": user, "x-timezone": tz}

    def call(self, method, path, body=None, content_type="application/json"):
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={**self.headers, "content-type": content_type})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    def stream(self):
        """Opens /api/stream now (so no event is missed) and returns an iterator of (event, data)."""
        r = urllib.request.urlopen(urllib.request.Request(self.base + "/api/stream", headers=self.headers), timeout=600)

        def events():
            event = None
            for raw in r:
                line = raw.decode().rstrip("\n")
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    yield event, json.loads(line[6:])

        return events()


def chunks_of(path, seconds):
    with wave.open(path) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (RATE, 1, 2):
            sys.exit("WAV must be 16 kHz mono 16-bit (afconvert -f WAVE -d LEI16@16000 -c 1 in.aiff out.wav)")
        frames = w.readframes(w.getnframes())
    step = int(seconds * RATE) * 2
    out = []
    for i in range(0, len(frames), step):
        buf = BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(RATE)
            w.writeframes(frames[i : i + step])
        out.append(buf.getvalue())
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("wav")
    p.add_argument("--server", default="http://localhost:8077")
    p.add_argument("--user", default=None, help="X-User-Id (default: a new one, so the quota starts at zero)")
    p.add_argument("--tz", default="Asia/Shanghai")
    p.add_argument("--chunk", type=float, default=10, help="seconds per chunk (contract: ≤ 60)")
    p.add_argument("--drop", default="", help="seqs lost on the first pass (network loss), resent after finish reports them missing")
    p.add_argument("--dup", type=int, default=1, help="send every chunk this many times")
    p.add_argument("--shuffle", action="store_true", help="send chunks out of order")
    p.add_argument("--restart", type=int, default=1, help="call POST /api/recordings this many times (device retries)")
    a = p.parse_args()

    api = Api(a.server, a.user or str(uuid.uuid4()), a.tz)
    api.call("GET", "/api/state")  # first request registers the user with its timezone
    events = api.stream()
    chunks = chunks_of(a.wav, a.chunk)
    rid = str(uuid.uuid4())
    before = api.call("GET", "/api/state")["quota"]
    print(f"录音 {rid}：{len(chunks)} 片，每片 {a.chunk:g} 秒")

    starts = {api.call("POST", "/api/recordings", {"recording_id": rid, "started_at": datetime.now(UTC).isoformat()})["ai"] for _ in range(a.restart)}
    print(f"开始录音调用 {a.restart} 次 → ai={starts}")

    dropped = {int(x) for x in a.drop.split(",") if x}
    sends = [seq for seq in range(len(chunks)) if seq not in dropped for _ in range(a.dup)]
    if a.shuffle:
        random.shuffle(sends)
    fresh = dup = 0
    for seq in sends:
        r = api.call("PUT", f"/api/recordings/{rid}/chunks/{seq}", chunks[seq], "audio/wav")
        dup += r["duplicate"]
        fresh += not r["duplicate"]
    print(f"第一轮发送 {len(sends)} 次（丢 {sorted(dropped)}，重复 ×{a.dup}，{'乱序' if a.shuffle else '顺序'}）→ 新片 {fresh}，重复 {dup}")

    missing = api.call("POST", f"/api/recordings/{rid}/finish", {"total_chunks": len(chunks)})["missing"]
    print(f"结束 → 服务端报缺片 {missing}")
    for seq in missing:
        api.call("PUT", f"/api/recordings/{rid}/chunks/{seq}", chunks[seq], "audio/wav")
    missing = api.call("POST", f"/api/recordings/{rid}/finish", {"total_chunks": len(chunks)})["missing"]
    print(f"补传后再结束 → 缺片 {missing}")

    for event, data in events:
        if event == "recording" and data["recording_id"] == rid:
            print(f"  状态：{data['status']}")
            if data["status"] in ("done", "failed", "paused_quota"):
                break
    late = api.call("PUT", f"/api/recordings/{rid}/chunks/0", chunks[0], "audio/wav")
    print(f"处理完后再发第 0 片 → duplicate={late['duplicate']}")

    state = api.call("GET", "/api/state")
    after = state["quota"]
    mine = [c for c in state["conversations"] if c["recording_id"] == rid]
    print("\n证明：")
    print(f"  今日录音次数 {before['recordings_today']['used']} → {after['recordings_today']['used']}（只占 1 次）")
    print(f"  本月语音分钟 {before['minutes']['used']} → {after['minutes']['used']}（只按有人说话的时长扣 1 次）")
    print(f"  这段录音产生的对话 {len(mine)} 段：" + "；".join(c["title"] or "" for c in mine))
    ok = after["recordings_today"]["used"] - before["recordings_today"]["used"] == 1 and late["duplicate"] and not missing
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
