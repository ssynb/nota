import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;

/// 服务端返回非 2xx。
class ApiException implements Exception {
  ApiException(this.status, this.body);
  final int status;
  final String body;

  /// 契约里的错误码（如 `ask_quota`），响应体不是 `{error}` 时为 null。
  String? get code {
    try {
      final decoded = jsonDecode(body);
      return decoded is Map ? decoded['error']?.toString() : null;
    } on FormatException {
      return null;
    }
  }

  @override
  String toString() => 'HTTP $status $body';
}

class SseEvent {
  const SseEvent(this.event, this.data);
  final String event;
  final String data;

  Map<String, dynamic> get json => jsonDecode(data) as Map<String, dynamic>;
}

/// 按 SSE 规范把字节流解析成事件：`event:` 命名，多行 `data:` 以换行拼接，空行分隔，`:` 开头为注释。
Stream<SseEvent> parseSse(Stream<List<int>> bytes) async* {
  var event = 'message';
  final data = <String>[];
  await for (final line
      in bytes.transform(utf8.decoder).transform(const LineSplitter())) {
    if (line.isEmpty) {
      if (data.isNotEmpty) yield SseEvent(event, data.join('\n'));
      event = 'message';
      data.clear();
      continue;
    }
    if (line.startsWith(':')) continue;
    final colon = line.indexOf(':');
    final field = colon < 0 ? line : line.substring(0, colon);
    var value = colon < 0 ? '' : line.substring(colon + 1);
    if (value.startsWith(' ')) value = value.substring(1);
    if (field == 'event') event = value;
    if (field == 'data') data.add(value);
  }
  if (data.isNotEmpty) yield SseEvent(event, data.join('\n'));
}

/// docs/contract.md 的客户端：每个请求带身份头，每个端点一个方法。
class NotaApi {
  NotaApi({
    required this.base,
    required this.userId,
    required this.timezone,
    http.Client? client,
  }) : _client = client ?? http.Client();

  final Uri base;
  final String userId;
  final String timezone;
  final http.Client _client;

  Map<String, String> get _headers => {
    'X-User-Id': userId,
    'X-Timezone': timezone,
  };

  Uri _uri(String path, [Map<String, String>? query]) =>
      base.replace(path: path, queryParameters: query);

  Future<dynamic> _send(
    String method,
    String path, {
    Object? json,
    Uint8List? bytes,
    Map<String, String>? query,
  }) async {
    final request = http.Request(method, _uri(path, query))
      ..headers.addAll(_headers);
    if (json != null) {
      request.headers['Content-Type'] = 'application/json';
      request.body = jsonEncode(json);
    }
    if (bytes != null) {
      request.headers['Content-Type'] = 'audio/wav';
      request.bodyBytes = bytes;
    }
    final response = await http.Response.fromStream(
      await _client.send(request),
    );
    if (response.statusCode >= 300) {
      throw ApiException(response.statusCode, response.body);
    }
    final body = utf8.decode(response.bodyBytes);
    return body.isEmpty ? null : jsonDecode(body);
  }

  Future<Stream<SseEvent>> _sse(
    String method,
    String path, {
    Object? json,
  }) async {
    final request = http.Request(method, _uri(path))
      ..headers.addAll(_headers)
      ..headers['Accept'] = 'text/event-stream';
    if (json != null) {
      request.headers['Content-Type'] = 'application/json';
      request.body = jsonEncode(json);
    }
    final response = await _client.send(request);
    if (response.statusCode >= 300) {
      throw ApiException(
        response.statusCode,
        await response.stream.bytesToString(),
      );
    }
    return parseSse(response.stream);
  }

  /// 返回 `ai`：`enabled` 或 `paused_quota`。
  Future<String> createRecording(String id, DateTime startedAt) async {
    final r = await _send(
      'POST',
      '/api/recordings',
      json: {
        'recording_id': id,
        'started_at': startedAt.toUtc().toIso8601String(),
      },
    );
    return r['ai'] as String;
  }

  Future<void> putChunk(String id, int seq, Uint8List wav) =>
      _send('PUT', '/api/recordings/$id/chunks/$seq', bytes: wav);

  /// 返回服务端缺的分片序号。
  Future<List<int>> finishRecording(String id, int totalChunks) async {
    final r = await _send(
      'POST',
      '/api/recordings/$id/finish',
      json: {'total_chunks': totalChunks},
    );
    return (r['missing'] as List).cast<int>();
  }

  Future<Map<String, dynamic>> state() async =>
      await _send('GET', '/api/state') as Map<String, dynamic>;

  Future<Stream<SseEvent>> stream() => _sse('GET', '/api/stream');

  Future<Stream<SseEvent>> ask(String idemKey, String question) => _sse(
    'POST',
    '/api/ask',
    json: {'idem_key': idemKey, 'question': question},
  );

  Future<List<Map<String, dynamic>>> pendingActions() async {
    final r = await _send('GET', '/api/actions', query: {'status': 'pending'});
    return (r as List).cast<Map<String, dynamic>>();
  }

  Future<void> reportResult(
    String id, {
    required bool ok,
    String? systemId,
    String? error,
  }) => _send(
    'POST',
    '/api/actions/$id/result',
    json: {'ok': ok, 'system_id': ?systemId, 'error': ?error},
  );

  Future<void> confirmAction(String id) =>
      _send('POST', '/api/actions/$id/confirm');

  Future<void> rejectAction(String id) =>
      _send('POST', '/api/actions/$id/reject');

  Future<void> undoAction(String id) => _send('POST', '/api/actions/$id/undo');

  Future<void> enrollVoiceprint(Uint8List wav) =>
      _send('POST', '/api/voiceprint', bytes: wav);
}
