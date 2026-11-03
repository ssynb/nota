import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:nota_app/uploader.dart';

import 'fake_server.dart';

Uint8List chunk(int seq) => Uint8List.fromList(List.filled(10, seq));

void main() {
  late Directory dir;
  late FakeServer server;

  setUp(() {
    dir = Directory.systemTemp.createTempSync('nota_upload');
    server = FakeServer();
  });
  tearDown(() => dir.deleteSync(recursive: true));

  test('断网时录完的录音留在本地，App 重启且恢复网络后完整送达并清理本地', () async {
    server.online = false;
    final offline = ChunkUploader(server.api(), dir);
    await offline.start('r1', DateTime.utc(2026, 11, 3, 9));
    for (var seq = 0; seq < 3; seq++) {
      await offline.addChunk('r1', seq, chunk(seq));
    }
    await offline.finish('r1', 3);
    await offline.sync();
    offline.dispose();
    expect(server.created, isEmpty);

    server.online = true;
    final restarted = ChunkUploader(server.api(), dir);
    await restarted.sync();
    restarted.dispose();

    expect(server.created, {'r1'});
    expect(server.chunks['r1'], {0: chunk(0), 1: chunk(1), 2: chunk(2)});
    expect(server.finishCalls, isNotEmpty);
    expect(dir.listSync(recursive: true).whereType<File>(), isEmpty);
  });

  test('finish 返回 missing 时补传缺的分片并再次 finish', () async {
    final uploader = ChunkUploader(server.api(), dir);
    await uploader.start('r2', DateTime.utc(2026, 11, 3, 9));
    await uploader.addChunk('r2', 0, chunk(0));
    await uploader.addChunk('r2', 1, chunk(1));
    await uploader.sync();
    server.dropOnFinish.add(1);
    await uploader.finish('r2', 2);
    await uploader.sync();
    uploader.dispose();

    expect(server.chunks['r2'], {0: chunk(0), 1: chunk(1)});
    expect(server.finishCalls, ['r2', 'r2']);
    expect(dir.listSync(recursive: true).whereType<File>(), isEmpty);
  });
}
