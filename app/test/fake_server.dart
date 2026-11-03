import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:nota_app/api.dart';

/// 按 docs/contract.md 行为的内存服务端，只覆盖测试用到的端点。
class FakeServer {
  bool online = true;
  final created = <String>{};
  final chunks = <String, Map<int, List<int>>>{};
  final finishCalls = <String>[];

  /// finish 时假装没收到这些分片（模拟服务端丢片）。
  final dropOnFinish = <int>{};
  final pending = <Map<String, dynamic>>[];
  final results = <String, Map<String, dynamic>>{};

  /// 回报结果的请求失败（模拟回报途中断网）。
  bool failResults = false;
  final headers = <Map<String, String>>[];

  NotaApi api({String userId = 'u1'}) => NotaApi(
    base: Uri.parse('http://nota.test'),
    userId: userId,
    timezone: 'Asia/Shanghai',
    client: MockClient(_handle),
  );

  Future<http.Response> _handle(http.Request r) async {
    if (!online) throw http.ClientException('offline', r.url);
    headers.add(r.headers);
    final p = r.url.pathSegments;
    http.Response ok(Object body) => http.Response(jsonEncode(body), 200);
    if (r.method == 'POST' && r.url.path == '/api/recordings') {
      final id = jsonDecode(r.body)['recording_id'] as String;
      created.add(id);
      return ok({'recording_id': id, 'ai': 'enabled'});
    }
    if (r.method == 'PUT' && p.length == 5 && p[3] == 'chunks') {
      if (!created.contains(p[2])) return http.Response('no recording', 404);
      final got = chunks.putIfAbsent(p[2], () => {});
      final seq = int.parse(p[4]);
      final duplicate = got.containsKey(seq);
      got[seq] = r.bodyBytes;
      return ok({'seq': seq, 'duplicate': duplicate});
    }
    if (r.method == 'POST' && p.length == 4 && p[3] == 'finish') {
      finishCalls.add(p[2]);
      final total = jsonDecode(r.body)['total_chunks'] as int;
      final got = chunks[p[2]] ?? {};
      for (final seq in dropOnFinish) {
        got.remove(seq);
      }
      dropOnFinish.clear();
      return ok({
        'missing': [
          for (var i = 0; i < total; i++)
            if (!got.containsKey(i)) i,
        ],
      });
    }
    if (r.method == 'GET' && r.url.path == '/api/actions') {
      return ok(pending.where((a) => !results.containsKey(a['id'])).toList());
    }
    if (r.method == 'POST' && p.length == 4 && p[3] == 'result') {
      if (failResults) throw http.ClientException('offline', r.url);
      results.putIfAbsent(p[2], () => jsonDecode(r.body));
      return ok({});
    }
    return http.Response('not found', 404);
  }
}
