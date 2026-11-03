import 'package:flutter_test/flutter_test.dart';
import 'package:nota_app/actions.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'fake_server.dart';

class FakeCalendar implements DeviceCalendar {
  final events = <String, Map<String, Object?>>{};
  var created = 0;

  @override
  Future<String> createEvent({
    required String title,
    required DateTime start,
    required DateTime end,
    String? location,
    int? alarmMinutesBefore,
  }) async {
    await Future<void>.delayed(Duration.zero);
    final id = 'EK-${++created}';
    events[id] = {
      'title': title,
      'start': start,
      'end': end,
      'location': location,
      'alarm': alarmMinutesBefore,
    };
    return id;
  }

  @override
  Future<bool> deleteEvent(String systemEventId) async =>
      events.remove(systemEventId) != null;
}

class FakeSharer implements Sharer {
  FakeSharer(this.userSends);
  final bool userSends;
  final shared = <String>[];

  @override
  Future<bool> share(String text) async {
    shared.add(text);
    return userSends;
  }
}

Map<String, dynamic> action(String id, String type, Map<String, dynamic> args) =>
    {
      'id': id,
      'type': type,
      'args': args,
      'status': 'pending',
      'needs_confirmation': false,
      'created_at': '2026-11-03T09:00:00+08:00',
    };

const dinner = {
  'title': '和张总吃饭',
  'start': '2026-11-11T15:00:00+08:00',
  'end': '2026-11-11T16:00:00+08:00',
  'location': '国贸',
  'alarm_minutes_before': 1440,
};

void main() {
  late FakeServer server;
  late FakeCalendar calendar;
  late SharedPreferences prefs;

  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    prefs = await SharedPreferences.getInstance();
    server = FakeServer();
    calendar = FakeCalendar();
  });

  ActionRunner runner() =>
      ActionRunner(server.api(), calendar, FakeSharer(true), prefs);

  test('建日程动作写入系统日历（提醒挂在日程上），回报系统日程 ID', () async {
    server.pending.add(action('a1', 'create_calendar_event', dinner));
    await runner().runPending();

    expect(calendar.events.values.single, {
      'title': '和张总吃饭',
      'start': DateTime.utc(2026, 11, 11, 7),
      'end': DateTime.utc(2026, 11, 11, 8),
      'location': '国贸',
      'alarm': 1440,
    });
    expect(server.results['a1'], {'ok': true, 'system_id': 'EK-1'});
  });

  test('同一动作只执行一次：并发拉取、回报丢失后重启都不重复写日历', () async {
    server.pending.add(action('a1', 'create_calendar_event', dinner));
    server.failResults = true;
    final first = runner();
    await Future.wait([first.runPending(), first.runPending()]);
    expect(calendar.created, 1);
    expect(server.results, isEmpty);

    server.failResults = false;
    await runner().runPending();

    expect(calendar.created, 1);
    expect(server.results['a1'], {'ok': true, 'system_id': 'EK-1'});
  });

  test('白名单外的动作类型不执行，回报失败', () async {
    server.pending.add(action('a2', 'send_email', {'to': 'x@y.z'}));
    await runner().runPending();

    expect(calendar.created, 0);
    expect(server.results['a2']?['ok'], false);
  });

  test('删日程动作按系统日程 ID 删除；日程已不存在时如实回报失败', () async {
    final id = await calendar.createEvent(
      title: 't',
      start: DateTime.utc(2026),
      end: DateTime.utc(2026),
    );
    server.pending.addAll([
      action('d1', 'delete_calendar_event', {'system_event_id': id}),
      action('d2', 'delete_calendar_event', {'system_event_id': 'gone'}),
    ]);
    await runner().runPending();

    expect(calendar.events, isEmpty);
    expect(server.results['d1'], {'ok': true});
    expect(server.results['d2']?['ok'], false);
  });

  test('发消息动作打开分享面板带上收件人与正文，用户取消则回报失败', () async {
    final msg = action('s1', 'share_message', {'to': '张总', 'text': '周三三点见'});
    server.pending.add(msg);
    final sharer = FakeSharer(false);
    await ActionRunner(server.api(), calendar, sharer, prefs).runPending();

    expect(sharer.shared.single, allOf(contains('张总'), contains('周三三点见')));
    expect(server.results['s1']?['ok'], false);
  });
}
