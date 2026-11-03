import 'package:flutter/services.dart';
import 'package:share_plus/share_plus.dart';

import 'actions.dart';

const _channel = MethodChannel('nota/device');

/// 系统时区的 IANA 名（如 `Asia/Shanghai`）。
Future<String> deviceTimezone() async =>
    (await _channel.invokeMethod<String>('timezone'))!;

Future<void> openAppSettings() => _channel.invokeMethod('openSettings');

/// 申请通知权限（用于 App 在后台时提醒待确认动作）。
Future<void> requestNotifications() =>
    _channel.invokeMethod('requestNotifications');

/// 发一条本地通知；同一 [id] 重复发送会替换旧的。
Future<void> notify(String id, String title, String body) =>
    _channel.invokeMethod('notify', {'id': id, 'title': title, 'body': body});

Future<void> clearNotification(String id) =>
    _channel.invokeMethod('clearNotification', {'id': id});

/// iOS EventKit 日历，实现在 ios/Runner/AppDelegate.swift。
class EventKitCalendar implements DeviceCalendar {
  @override
  Future<String> createEvent({
    required String title,
    required DateTime start,
    required DateTime end,
    String? location,
    int? alarmMinutesBefore,
  }) async => (await _channel.invokeMethod<String>('createEvent', {
    'title': title,
    'startMs': start.millisecondsSinceEpoch,
    'endMs': end.millisecondsSinceEpoch,
    'location': location,
    'alarmMinutesBefore': alarmMinutesBefore,
  }))!;

  @override
  Future<bool> deleteEvent(String systemEventId) async => (await _channel
      .invokeMethod<bool>('deleteEvent', {'id': systemEventId}))!;
}

class SystemSharer implements Sharer {
  @override
  Future<bool> share(String text) async {
    final r = await SharePlus.instance.share(ShareParams(text: text));
    return r.status == ShareResultStatus.success;
  }
}
