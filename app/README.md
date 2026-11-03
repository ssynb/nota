# Nota iOS App

Nota 的 Flutter iOS 客户端，按 [接口契约](../docs/contract.md) 对接后端。手机在这里扮演“设备 → 手机 → 云端”里的手机：用自身麦克风模拟设备录音，按分片上传；同时是 Agent 动作的执行端。

## 功能

- **身份**：首次启动生成 UUID 作为 `X-User-Id` 保存在本机，每个请求带 `X-Timezone`（系统时区）。
- **录音**：16 kHz 单声道 16-bit WAV，按设置的分片时长（默认 60 秒）切片。分片先落盘再上传，断网或 App 被杀后重连自动补传，录音中途被杀的在下次启动时按已存分片收尾；结束时按服务端返回的缺片补传，确认不缺后才删本地文件。
- **实时**：订阅 `/api/stream`，显示录音处理进度、Agent 每一步工具调用、纪要、待办和三本额度账；额度用完显示“录音已保存，等待整理”。断线退避重连，重连后拉全量状态、补执行动作、补传录音。
- **动作执行**：只执行契约白名单的三类——写系统日历（提醒挂在日程上）、删系统日历、打开系统分享面板；其他类型回报失败。执行结果先记在本机再回报，同一动作只执行一次；拉取或回报失败时按 2 秒起、封顶 60 秒退避自动重试补报。日历动作拉到就执行；发消息确认后在首页和动作页显示“待发送”卡片，用户点“继续发送”才打开分享面板：发完回报成功，面板里取消则保持待发送可再发，点“放弃”回报失败。需要确认的动作显示确认卡片（确认/拒绝）；App 在后台（如锁屏录音）时有动作待确认，用本地通知提醒，处理后收回。已完成的日程可撤销。
- **权限时机**：麦克风在第一次录音时申请，通知在第一次开始录音时申请，日历在第一次执行日历动作时申请（需要完全访问，因为撤销要按日程 ID 删除）。日历被拒时动作如实回报失败，动作页给出“去设置”入口。
- **提问**：`/api/ask` 流式显示 Agent 步骤、答案与出处。
- **声纹注册**：朗读几句话（至少 10 秒）上传。

## 运行

需要 Flutter（`~/development/flutter/bin` 加入 PATH）和 Xcode。

```sh
cd app
flutter pub get
open -a Simulator
flutter run -d <模拟器 ID> \
  --dart-define=NOTA_API=http://127.0.0.1:8077 \
  --dart-define=NOTA_CHUNK_SECONDS=15
```

两个 `--dart-define` 都可省略（默认 `http://127.0.0.1:8077`（后端 start.sh 的默认端口）、60 秒），运行后也能在“设置”页修改：服务器地址重启 App 后生效，分片时长下次开始录音时生效。模拟器的麦克风取 Mac 的系统输入（Simulator 菜单 Device > Sound > Sound Input）。

### 装到真机

手机和 Mac 连同一 Wi-Fi，后端监听 `0.0.0.0`。Bundle ID 为 `com.ssynb.nota`，工程用自动签名、团队 `82SF99ZDUT`（个人免费团队）；换人构建时在 Xcode 的 Signing & Capabilities 里改成自己的 Team，免费团队还需改一个全局唯一的 Bundle ID。之后：

```sh
flutter run --release -d <设备 UDID> --dart-define=NOTA_API=http://<Mac 局域网 IP>:8077
```

首次打开前要在手机“设置 > 通用 > VPN 与设备管理”里信任开发者证书；首次联网时 iOS 会询问是否允许 Nota 访问本地网络，需要允许。App 的 HTTP 走 Dart 自己的 socket，不受 ATS 约束；真机上拦局域网访问的是“本地网络”权限（`NSLocalNetworkUsageDescription`）。

## 测试

```sh
flutter analyze && flutter test
```

测试在 HTTP 接缝上用内存假服务端（`test/fake_server.dart`）覆盖断网补传、缺片补传、动作只执行一次与白名单、WAV 与 SSE 协议、身份持久化。系统日历走 `ios/Runner/AppDelegate.swift` 里的 EventKit 通道，只能在模拟器或真机上验证。
