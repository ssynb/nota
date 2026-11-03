import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:nota_app/api.dart';
import 'package:nota_app/identity.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  test('SSE：事件名、多行 data、注释与跨包切分都按规范解析', () async {
    const raw =
        ': keepalive\n\nevent: recording\ndata: {"status":\n'
        'data: "done"}\n\ndata: plain\n\n';
    final bytes = utf8.encode(raw);
    final events = await parseSse(
      Stream.fromIterable([bytes.sublist(0, 25), bytes.sublist(25)]),
    ).toList();

    expect(events.map((e) => (e.event, e.data)), [
      ('recording', '{"status":\n"done"}'),
      ('message', 'plain'),
    ]);
  });

  test('用户 ID 首次生成 UUID，之后启动沿用同一个', () async {
    SharedPreferences.setMockInitialValues({});
    final prefs = await SharedPreferences.getInstance();
    final id = loadUserId(prefs);

    expect(
      id,
      matches(RegExp(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')),
    );
    expect(loadUserId(prefs), id);
  });
}
