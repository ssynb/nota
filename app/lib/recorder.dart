import 'dart:async';
import 'dart:typed_data';

import 'package:record/record.dart';

import 'identity.dart';
import 'uploader.dart';
import 'wav.dart';

const _pcmConfig = RecordConfig(
  encoder: AudioEncoder.pcm16bits,
  sampleRate: sampleRate,
  numChannels: 1,
);

/// 打开麦克风，输出 16 kHz 单声道 16-bit PCM；未授权时抛错。
Future<(AudioRecorder, Stream<Uint8List>)> _openMic() async {
  final recorder = AudioRecorder();
  if (!await recorder.hasPermission()) {
    await recorder.dispose();
    throw Exception('未授权麦克风，请在“设置”里允许 Nota 使用麦克风');
  }
  return (recorder, await recorder.startStream(_pcmConfig));
}

/// 一段录音：边录边按固定时长切片，交给 [ChunkUploader] 落盘上传。
class RecordingSession {
  RecordingSession._(this.id, this.startedAt, this._recorder, this._uploader);

  final String id;
  final DateTime startedAt;
  final AudioRecorder _recorder;
  final ChunkUploader _uploader;
  late final StreamSubscription<Uint8List> _sub;
  late final PcmChunker _chunker;
  final _done = Completer<void>();
  Future<void> _writes = Future.value();
  var _seq = 0;

  static Future<RecordingSession> start(
    ChunkUploader uploader,
    int chunkSeconds,
  ) async {
    final (recorder, pcm) = await _openMic();
    final session = RecordingSession._(
      newUuid(),
      DateTime.now(),
      recorder,
      uploader,
    );
    await uploader.start(session.id, session.startedAt);
    session._chunker = PcmChunker(chunkSeconds * bytesPerSecond, session._emit);
    session._sub = pcm.listen(
      session._chunker.add,
      onDone: session._done.complete,
    );
    return session;
  }

  void _emit(Uint8List pcm) {
    final seq = _seq++;
    _writes = _writes.then((_) => _uploader.addChunk(id, seq, wavBytes(pcm)));
  }

  Future<void> stop() async {
    await _recorder.stop();
    await _done.future;
    await _sub.cancel();
    await _recorder.dispose();
    _chunker.close();
    await _writes;
    await _uploader.finish(id, _seq);
  }
}

/// 一次性录一段 PCM（声纹注册用），[stop] 返回整段 WAV。
class OneShotCapture {
  OneShotCapture._(this._recorder);
  final AudioRecorder _recorder;
  final _pcm = BytesBuilder(copy: false);
  final _done = Completer<void>();

  int get bytes => _pcm.length;

  static Future<OneShotCapture> start() async {
    final (recorder, pcm) = await _openMic();
    final capture = OneShotCapture._(recorder);
    pcm.listen(capture._pcm.add, onDone: capture._done.complete);
    return capture;
  }

  Future<Uint8List> stop() async {
    await _recorder.stop();
    await _done.future;
    await _recorder.dispose();
    return wavBytes(_pcm.takeBytes());
  }
}
