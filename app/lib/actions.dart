import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter/services.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'api.dart';

/// 系统日历（iOS EventKit）。
abstract interface class DeviceCalendar {
  /// 返回系统日程 ID。
  Future<String> createEvent({
    required String title,
    required DateTime start,
    required DateTime end,
    String? location,
    int? alarmMinutesBefore,
  });

  /// 日程不存在时返回 false。
  Future<bool> deleteEvent(String systemEventId);
}

/// 系统分享面板；用户完成分享返回 true，取消返回 false。
abstract interface class Sharer {
  Future<bool> share(String text);
}

const _executedKey = 'executed_actions';

typedef ActionResult = ({bool ok, String? systemId, String? error});

/// 取服务端待执行动作，在手机上执行白名单内的类型并回报结果。
///
/// 执行结果先记在本地再回报：同一动作无论被拉到几次、回报是否丢失、App 是否重启，
/// 都只执行一次；没回报成功的结果在下次运行时补报。
///
/// 日历动作拉到就执行；`share_message` 要弹系统分享面板，只在用户点“继续发送”时打开
/// （[sendShare]），面板里取消则保持待发送，用户点“放弃”（[abandonShare]）才回报失败。
class ActionRunner {
  ActionRunner(this.api, this.calendar, this.sharer, this.prefs);
  final NotaApi api;
  final DeviceCalendar calendar;
  final Sharer sharer;
  final SharedPreferences prefs;

  /// 最近一次运行的失败原因；成功后清空。
  final error = ValueNotifier<String?>(null);

  Future<void> _queue = Future.value();

  Future<void> runPending() => _serial(_run);

  /// 用户点“继续发送”：打开分享面板，发完才记为已执行并回报；取消则什么都不记。
  Future<void> sendShare(Map<String, dynamic> action) => _serial(() async {
    final executed = _executed;
    final id = action['id'] as String;
    if (!executed.containsKey(id)) {
      final args = (action['args'] as Map).cast<String, dynamic>();
      if (!await sharer.share(_message(args))) return;
      executed[id] = _record((ok: true, systemId: null, error: null));
      await _save(executed);
    }
    await _reportUnreported(executed);
  });

  /// 用户点“放弃”：不发了，回报失败。
  Future<void> abandonShare(String id) => _serial(() async {
    final executed = _executed;
    executed.putIfAbsent(
      id,
      () => _record((ok: false, systemId: null, error: 'user_cancelled')),
    );
    await _save(executed);
    await _reportUnreported(executed);
  });

  /// 所有执行串行进行，并发触发也不会重复执行；失败记在 [error]，等下次触发重试。
  Future<void> _serial(Future<void> Function() body) =>
      _queue = _queue.then((_) async {
        try {
          await body();
          error.value = null;
        } catch (e) {
          error.value = e.toString();
        }
      });

  static Map<String, dynamic> _record(ActionResult r) => {
    'ok': r.ok,
    'system_id': r.systemId,
    'error': r.error,
    'reported': false,
  };

  Map<String, dynamic> get _executed =>
      jsonDecode(prefs.getString(_executedKey) ?? '{}') as Map<String, dynamic>;

  Future<void> _save(Map<String, dynamic> executed) =>
      prefs.setString(_executedKey, jsonEncode(executed));

  Future<void> _run() async {
    final executed = _executed;
    for (final action in await api.pendingActions()) {
      final id = action['id'] as String;
      if (executed.containsKey(id) || action['status'] != 'pending') continue;
      if (action['type'] == 'share_message') continue; // 等用户点继续发送
      executed[id] = _record(await _execute(action));
      await _save(executed);
    }
    await _reportUnreported(executed);
  }

  Future<void> _reportUnreported(Map<String, dynamic> executed) async {
    for (final MapEntry(key: id, value: r) in executed.entries) {
      if (r['reported'] == true) continue;
      await api.reportResult(
        id,
        ok: r['ok'] as bool,
        systemId: r['system_id'] as String?,
        error: r['error'] as String?,
      );
      r['reported'] = true;
      await _save(executed);
    }
  }

  /// 正文已经称呼了收件人就原样分享，否则在开头加上称呼。
  static String _message(Map<String, dynamic> args) {
    final to = args['to'] as String;
    final text = args['text'] as String;
    return text.contains(to) ? text : '$to，$text';
  }

  Future<ActionResult> _execute(Map<String, dynamic> action) async {
    final args = (action['args'] as Map).cast<String, dynamic>();
    try {
      switch (action['type']) {
        case 'create_calendar_event':
          final id = await calendar.createEvent(
            title: args['title'] as String,
            start: DateTime.parse(args['start'] as String),
            end: DateTime.parse(args['end'] as String),
            location: args['location'] as String?,
            alarmMinutesBefore: args['alarm_minutes_before'] as int?,
          );
          return (ok: true, systemId: id, error: null);
        case 'delete_calendar_event':
          final deleted = await calendar.deleteEvent(
            args['system_event_id'] as String,
          );
          return deleted
              ? (ok: true, systemId: null, error: null)
              : (ok: false, systemId: null, error: 'event_not_found');
        default:
          return (ok: false, systemId: null, error: 'unsupported_type');
      }
    } on PlatformException catch (e) {
      // 回报系统给的错误码（如 calendar_denied），界面据此引导用户去设置。
      return (ok: false, systemId: null, error: e.code);
    } catch (e) {
      return (ok: false, systemId: null, error: e.toString());
    }
  }
}
