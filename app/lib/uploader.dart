import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';

import 'api.dart';

typedef UploadState = ({int unsent, String? error});

const _minBackoff = Duration(seconds: 2);
const _maxBackoff = Duration(seconds: 60);
const _maxFinishRounds = 3;

/// 录音分片的本地落盘与上传，模拟“设备经手机中转”。
///
/// 每段录音一个目录：`meta.json`（开始时间）、`created`（服务端已建）、
/// `<seq>.wav`（未确认）/ `<seq>.sent`（服务端已收）、`total`（已结束及总片数）。
/// 文件先落盘再上传，服务端 finish 确认不缺片后才删整个目录；进程被杀后重启照常续传。
class ChunkUploader {
  ChunkUploader(this.api, this.root, {this.onCreated});

  final NotaApi api;
  final Directory root;

  /// 服务端建好录音后回调，`ai` 为 `enabled` 或 `paused_quota`。
  final void Function(String recordingId, String ai)? onCreated;

  final state = ValueNotifier<UploadState>((unsent: 0, error: null));

  Future<void> _queue = Future.value();
  Timer? _retry;
  Duration _backoff = _minBackoff;
  bool _disposed = false;

  Directory _dir(String id) => Directory('${root.path}/$id');
  File _file(String id, String name) => File('${root.path}/$id/$name');

  Future<void> start(String recordingId, DateTime startedAt) async {
    await _dir(recordingId).create(recursive: true);
    await _write(
      _file(recordingId, 'meta.json'),
      utf8.encode(jsonEncode({'started_at': startedAt.toIso8601String()})),
    );
    unawaited(sync());
  }

  Future<void> addChunk(String recordingId, int seq, Uint8List wav) async {
    await _write(_file(recordingId, '$seq.wav'), wav);
    unawaited(sync());
  }

  Future<void> finish(String recordingId, int totalChunks) async {
    await _write(_file(recordingId, 'total'), utf8.encode('$totalChunks'));
    unawaited(sync());
  }

  /// 把本地未确认的内容尽量送到服务端；网络失败时保留在本地并退避重试。
  Future<void> sync() => _queue = _queue.then((_) => _pass());

  void dispose() {
    _disposed = true;
    _retry?.cancel();
  }

  /// 先写临时文件再改名，进程被杀时不会留下半个分片。
  Future<void> _write(File file, List<int> bytes) async {
    final tmp = File('${file.path}.tmp');
    await tmp.writeAsBytes(bytes, flush: true);
    await tmp.rename(file.path);
  }

  Future<void> _pass() async {
    if (_disposed) return;
    String? error;
    if (root.existsSync()) {
      for (final dir in root.listSync().whereType<Directory>()) {
        try {
          await _syncRecording(
            dir.uri.pathSegments.lastWhere((s) => s.isNotEmpty),
          );
        } catch (e) {
          error = e.toString();
        }
      }
    }
    state.value = (unsent: _countUnsent(), error: error);
    _scheduleRetry(failed: error != null);
  }

  Future<void> _syncRecording(String id) async {
    final meta = _file(id, 'meta.json');
    if (!meta.existsSync()) return; // start 尚未写完
    if (!_file(id, 'created').existsSync()) {
      final startedAt = DateTime.parse(
        jsonDecode(await meta.readAsString())['started_at'] as String,
      );
      final ai = await api.createRecording(id, startedAt);
      await _write(_file(id, 'created'), utf8.encode(ai));
      onCreated?.call(id, ai);
    }
    await _uploadUnsent(id);
    final total = _file(id, 'total');
    if (!total.existsSync()) return;
    final count = int.parse(await total.readAsString());
    for (var round = 0; round < _maxFinishRounds; round++) {
      final missing = await api.finishRecording(id, count);
      if (missing.isEmpty) {
        await _dir(id).delete(recursive: true);
        return;
      }
      for (final seq in missing) {
        final sent = _file(id, '$seq.sent');
        if (sent.existsSync()) await sent.rename(_file(id, '$seq.wav').path);
      }
      await _uploadUnsent(id);
    }
    throw Exception('录音 $id 补传后服务端仍缺分片');
  }

  Future<void> _uploadUnsent(String id) async {
    final unsent =
        _dir(id)
            .listSync()
            .whereType<File>()
            .map((f) => f.uri.pathSegments.last)
            .where((n) => n.endsWith('.wav'))
            .map((n) => int.parse(n.substring(0, n.length - 4)))
            .toList()
          ..sort();
    for (final seq in unsent) {
      final file = _file(id, '$seq.wav');
      await api.putChunk(id, seq, await file.readAsBytes());
      await file.rename(_file(id, '$seq.sent').path);
    }
  }

  int _countUnsent() {
    if (!root.existsSync()) return 0;
    return root
        .listSync(recursive: true)
        .where((f) => f is File && f.path.endsWith('.wav'))
        .length;
  }

  void _scheduleRetry({required bool failed}) {
    _retry?.cancel();
    if (!failed || _disposed) {
      _backoff = _minBackoff;
      return;
    }
    _retry = Timer(_backoff, sync);
    final next = _backoff * 2;
    _backoff = next > _maxBackoff ? _maxBackoff : next;
  }
}
