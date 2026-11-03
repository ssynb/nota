import 'dart:async';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'actions.dart';
import 'api.dart';
import 'device.dart';
import 'identity.dart';
import 'recorder.dart';
import 'uploader.dart';

const defaultBaseUrl = String.fromEnvironment(
  'NOTA_API',
  defaultValue: 'http://127.0.0.1:8000',
);
const defaultChunkSeconds = int.fromEnvironment(
  'NOTA_CHUNK_SECONDS',
  defaultValue: 60,
);
const _baseUrlKey = 'base_url';
const _chunkSecondsKey = 'chunk_seconds';
const _maxReconnectDelay = Duration(seconds: 30);

/// 一段录音在服务端的处理进度（来自事件流）。
class LiveRecording {
  String status = 'recording';
  String? error;
  final steps = <Map<String, dynamic>>[];
}

/// App 的唯一状态源：服务端状态（`/api/state`）+ 事件流里的实时进度 + 本地录音。
class NotaController extends ChangeNotifier {
  NotaController._(this._prefs, this._timezone, this._uploadRoot);

  final SharedPreferences _prefs;
  final String _timezone;
  final Directory _uploadRoot;

  late NotaApi api;
  late ChunkUploader uploader;
  late ActionRunner runner;

  Map<String, dynamic>? state;
  bool connected = false;
  String? connectionError;
  final live = <String, LiveRecording>{};
  RecordingSession? session;

  StreamSubscription<SseEvent>? _events;
  Completer<void>? _streamClosed;
  bool _disposed = false;
  Future<void>? _refreshing;
  bool _refreshAgain = false;

  static Future<NotaController> create(Directory supportDir) async {
    final c = NotaController._(
      await SharedPreferences.getInstance(),
      await deviceTimezone(),
      Directory('${supportDir.path}/recordings'),
    );
    c._connect();
    unawaited(c._listen());
    return c;
  }

  String get baseUrl => _prefs.getString(_baseUrlKey) ?? defaultBaseUrl;
  int get chunkSeconds =>
      _prefs.getInt(_chunkSecondsKey) ?? defaultChunkSeconds;

  Map<String, dynamic>? get quota => state?['quota'] as Map<String, dynamic>?;
  List<Map<String, dynamic>> list(String key) =>
      ((state?[key] as List?) ?? const []).cast<Map<String, dynamic>>();

  void _connect() {
    api = NotaApi(
      base: Uri.parse(baseUrl),
      userId: loadUserId(_prefs),
      timezone: _timezone,
    );
    uploader = ChunkUploader(api, _uploadRoot, onCreated: _onCreated)
      ..state.addListener(notifyListeners);
    runner = ActionRunner(api, EventKitCalendar(), SystemSharer(), _prefs)
      ..error.addListener(notifyListeners);
  }

  Future<void> updateSettings(String baseUrl, int chunkSeconds) async {
    await _prefs.setString(_baseUrlKey, baseUrl);
    await _prefs.setInt(_chunkSecondsKey, chunkSeconds);
    uploader.dispose();
    _connect();
    await _events?.cancel();
    _streamClosed?.complete();
    notifyListeners();
  }

  /// 事件流断开后退避重连；每次连上先拉全量状态、补执行动作、补传录音。
  Future<void> _listen() async {
    var delay = const Duration(seconds: 1);
    while (!_disposed) {
      final closed = _streamClosed = Completer<void>();
      try {
        final events = await api.stream();
        _events = events.listen(
          _apply,
          onError: (Object e) => connectionError = e.toString(),
          onDone: () => closed.isCompleted ? null : closed.complete(),
        );
        connected = true;
        connectionError = null;
        delay = const Duration(seconds: 1);
        notifyListeners();
        unawaited(refresh());
        unawaited(runner.runPending());
        unawaited(uploader.sync());
        await closed.future;
      } catch (e) {
        connectionError = e.toString();
      }
      connected = false;
      notifyListeners();
      if (_disposed) return;
      await Future<void>.delayed(delay);
      delay = delay * 2 > _maxReconnectDelay ? _maxReconnectDelay : delay * 2;
    }
  }

  void _apply(SseEvent e) {
    final data = e.json;
    switch (e.event) {
      case 'recording':
        _live(data['recording_id'] as String)
          ..status = data['status'] as String
          ..error = data['error'] as String?;
      case 'step':
        final id = data['recording_id'] as String?;
        if (id != null) _live(id).steps.add(data);
      case 'action':
        if (data['status'] == 'pending') unawaited(runner.runPending());
        unawaited(refresh());
      case 'conversation' || 'quota':
        unawaited(refresh());
    }
    notifyListeners();
  }

  LiveRecording _live(String id) => live.putIfAbsent(id, LiveRecording.new);

  void _onCreated(String id, String ai) {
    if (ai == 'paused_quota') _live(id).status = 'paused_quota';
    notifyListeners();
  }

  /// 拉 `/api/state`；并发调用合并成至多再拉一次。失败记在 [connectionError]，等下一次事件或重连再拉。
  Future<void> refresh() {
    _refreshAgain = true;
    return _refreshing ??= _refreshLoop().whenComplete(
      () => _refreshing = null,
    );
  }

  Future<void> _refreshLoop() async {
    while (_refreshAgain) {
      _refreshAgain = false;
      try {
        state = await api.state();
        connectionError = null;
      } catch (e) {
        connectionError = e.toString();
      }
      notifyListeners();
    }
  }

  Future<void> startRecording() async {
    final s = await RecordingSession.start(uploader, chunkSeconds);
    session = s;
    _live(s.id);
    notifyListeners();
  }

  Future<void> stopRecording() async {
    final s = session!;
    session = null;
    notifyListeners();
    await s.stop();
  }

  Future<void> confirm(String id) =>
      api.confirmAction(id).then((_) => refresh());
  Future<void> reject(String id) => api.rejectAction(id).then((_) => refresh());
  Future<void> undo(String id) => api.undoAction(id).then((_) => refresh());

  Future<Stream<SseEvent>> ask(String question) => api.ask(newUuid(), question);

  Future<void> enrollVoiceprint(Uint8List wav) async {
    await api.enrollVoiceprint(wav);
    await refresh();
  }

  @override
  void dispose() {
    _disposed = true;
    _events?.cancel();
    uploader.dispose();
    super.dispose();
  }
}
