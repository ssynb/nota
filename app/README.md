# Nota iOS App

Nota 的 Flutter iOS 客户端，按 [接口契约](../docs/contract.md) 对接后端。手机在这里扮演“设备 → 手机 → 云端”里的手机：用自身麦克风模拟设备录音，按分片上传；同时是 Agent 动作的执行端。

## 功能

- **身份**：首次启动生成 UUID 作为 `X-User-Id` 保存在本机，每个请求带 `X-Timezone`（系统时区）。
- **录音**：16 kHz 单声道 16-bit WAV，按设置的分片时长（默认 60 秒）切片。分片先落盘再上传，断网或 App 被杀后重连自动补传；结束时按服务端返回的缺片补传，确认不缺后才删本地文件。
- **实时**：订阅 `/api/stream`，显示录音处理进度、Agent 每一步工具调用、纪要、待办和三本额度账；额度用完显示“录音已保存，等待整理”。断线退避重连，重连后拉全量状态、补执行动作、补传录音。
- **动作执行**：只执行契约白名单的三类——写系统日历（提醒挂在日程上）、删系统日历、打开系统分享面板；其他类型回报失败。执行结果先记在本机再回报，同一动作只执行一次，回报丢失时下次补报。需要确认的动作显示确认卡片；已完成的日程可撤销。
- **提问**：`/api/ask` 流式显示 Agent 步骤、答案与出处。
- **声纹注册**：朗读几句话（至少 10 秒）上传。

## 运行

需要 Flutter（`~/development/flutter/bin` 加入 PATH）和 Xcode。

```sh
cd app
flutter pub get
open -a Simulator
flutter run -d <模拟器 ID> \
  --dart-define=NOTA_API=http://127.0.0.1:8000 \
  --dart-define=NOTA_CHUNK_SECONDS=15
```

两个 `--dart-define` 都可省略（默认 `http://127.0.0.1:8000`、60 秒），运行后也能在“设置”页修改。模拟器的麦克风取 Mac 的系统输入（Simulator 菜单 Device > Sound > Sound Input）。ATS 只放开本地网络（`NSAllowsLocalNetworking`），连局域网外的 HTTP 地址需要改 `ios/Runner/Info.plist`。

## 测试

```sh
flutter analyze && flutter test
```

测试在 HTTP 接缝上用内存假服务端（`test/fake_server.dart`）覆盖断网补传、缺片补传、动作只执行一次与白名单、WAV 与 SSE 协议、身份持久化。系统日历走 `ios/Runner/AppDelegate.swift` 里的 EventKit 通道，只能在模拟器或真机上验证。
