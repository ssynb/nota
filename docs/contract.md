# 接口契约（后端 ↔ 网页 / App / 设备模拟器）

后端、网页、Flutter App、设备模拟器都按这份契约实现，改契约先改这里。设计依据见 Part A（T1、T3、T4、T6、T7）。

## 身份

演示用身份：每个请求带 `X-User-Id`（客户端生成并本地保存的 UUID）。首次请求带 `X-Timezone`（IANA 名，如 `Asia/Shanghai`），用户创建后时区固定，之后忽略。计划（free/pro）只能由运维命令修改。

## 音频格式

所有上传音频：WAV，16 kHz，单声道，16-bit PCM。分片时长不超过 60 秒，长度不要求整齐。

## 录音（设备 → 手机 → 云端）

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| POST | `/api/recordings` | `{recording_id, started_at}`；`recording_id` 由客户端生成 UUID | `{recording_id, ai: "enabled" \| "paused_quota"}`。占用当天 1 次（按用户时区）；同一 `recording_id` 重复调用不重复占用 |
| PUT | `/api/recordings/{recording_id}/chunks/{seq}` | 请求体为 WAV 字节；`seq` 从 0 开始 | `{seq, duplicate: bool}`。同一 `(recording_id, seq)` 重复上传返回 `duplicate: true`，不重复处理、不重复计费 |
| POST | `/api/recordings/{recording_id}/finish` | `{total_chunks}` | `{missing: [seq...]}`；缺片时客户端补传后再调一次 |
| POST | `/api/notes/text` | `{idem_key, text}`；文字直接走纪要和 Agent，跳过转写 | `{recording_id}`；占用当天 1 次，不计分钟 |

额度用完时录音照常接收和保存，`ai` 为 `paused_quota`，额度恢复后自动处理。

## 提问

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| POST | `/api/ask` | `{idem_key, question}` | SSE 流：`step`（Agent 每次工具调用）…`answer`（`{text, citations: [{conversation_id, started_at, offset_seconds}]}`）；超额返回 429 `{error: "ask_quota"}` |

## 事件流

`GET /api/stream`：SSE，推送当前用户的所有变化，网页和 App 订阅。事件：

| 事件 | 数据 |
|---|---|
| `recording` | `{recording_id, status: recording\|uploading\|transcribing\|summarizing\|done\|failed\|paused_quota, error?}` |
| `step` | `{recording_id, tool, args, result}`：Agent 的一次工具调用 |
| `conversation` | 一段对话的纪要（见下方纪要结构） |
| `action` | 一条动作（见下方动作结构）的新建或状态变化 |
| `quota` | 同 `/api/state` 中的 `quota` |

## 状态

`GET /api/state` 返回：

```json
{
  "user": {"id": "…", "plan": "free", "timezone": "Asia/Shanghai"},
  "quota": {
    "minutes": {"used": 12.5, "limit": 300, "period": "2026-11"},
    "recordings_today": {"used": 3, "limit": 10, "day": "2026-11-03"},
    "asks": {"used": 4, "limit": 30, "period": "2026-11"}
  },
  "conversations": [],
  "todos": [],
  "actions": [],
  "profile": [],
  "voiceprint_enrolled": false
}
```

Pro 的 `limit` 为 `null`，分钟另受公平使用上限约束（超出时同样 `paused_quota`）。

纪要结构：`{id, recording_id, started_at, ended_at, title, overview, speakers: [{label, person_id?, is_owner}], facts: [{text, speaker_label, offset_seconds}], todos: [...]}`。

## 动作：服务器决策，手机执行

动作结构：`{id, type, args, status, needs_confirmation, created_at, result?}`。

`status`：`pending_confirmation` → `pending` → `done` / `failed`；用户拒绝为 `rejected`；撤销会生成一条新的反向动作。

手机只执行下列类型，其他一律拒绝：

| type | args | 手机做什么 | 是否要确认 |
|---|---|---|---|
| `create_calendar_event` | `{title, start, end, location?, alarm_minutes_before?}`，时间为带时区偏移的 ISO 8601 | 写入系统日历，提醒挂在日程上 | 否 |
| `delete_calendar_event` | `{system_event_id}` | 从系统日历删除 | 否 |
| `share_message` | `{to, text}` | 打开系统分享面板，由用户自己发送 | 是 |

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/actions?status=pending` | 手机取待执行动作 |
| POST | `/api/actions/{id}/result` | `{ok, system_id?, error?}`；同一动作重复回报以第一次为准 |
| POST | `/api/actions/{id}/confirm` | 确认卡片：`pending_confirmation` → `pending` |
| POST | `/api/actions/{id}/reject` | → `rejected` |
| POST | `/api/actions/{id}/undo` | 对已完成的 `create_calendar_event` 生成一条 `delete_calendar_event` |

待办（todo）存在服务端，不需要手机执行。

## 画像与声纹

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/profile` | 画像条目 `[{id, category, content, updated_at}]` |
| PUT | `/api/profile/{id}` | 修改 `{category, content}`，每条不超过 200 字 |
| DELETE | `/api/profile/{id}` | 删除 |
| POST | `/api/voiceprint` | 请求体为 WAV（读几句话，建议 10 秒以上）；返回 `{enrolled: true}` 或 400 说明原因（太短、太吵） |
