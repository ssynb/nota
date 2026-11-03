import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:nota_app/wav.dart';

void main() {
  test('WAV 头符合契约：16 kHz、单声道、16-bit PCM', () {
    final wav = wavBytes(Uint8List.fromList([1, 2, 3, 4]));
    expect(wav.sublist(0, 44), [
      ...'RIFF'.codeUnits, 40, 0, 0, 0, ...'WAVE'.codeUnits, //
      ...'fmt '.codeUnits, 16, 0, 0, 0, 1, 0, 1, 0, //
      0x80, 0x3e, 0, 0, 0, 0x7d, 0, 0, 2, 0, 16, 0, //
      ...'data'.codeUnits, 4, 0, 0, 0,
    ]);
    expect(wav.sublist(44), [1, 2, 3, 4]);
  });

  test('PCM 按固定长度切片，结束时交出不足一片的尾巴，字节不丢不乱', () {
    final out = <Uint8List>[];
    final chunker = PcmChunker(4, out.add);
    chunker.add(Uint8List.fromList([0, 1, 2]));
    chunker.add(Uint8List.fromList([3, 4, 5, 6, 7, 8]));
    chunker.close();
    expect(out, [
      [0, 1, 2, 3],
      [4, 5, 6, 7],
      [8],
    ]);
  });
}
