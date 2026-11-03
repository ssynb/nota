import 'dart:typed_data';

const sampleRate = 16000;
const bytesPerSecond = sampleRate * 2;
const _headerBytes = 44;

/// 16 kHz 单声道 16-bit PCM 包成 WAV。
Uint8List wavBytes(Uint8List pcm) {
  final out = Uint8List(_headerBytes + pcm.length);
  final h = ByteData.sublistView(out);
  void tag(int at, String s) => out.setAll(at, s.codeUnits);
  tag(0, 'RIFF');
  h.setUint32(4, 36 + pcm.length, Endian.little);
  tag(8, 'WAVE');
  tag(12, 'fmt ');
  h.setUint32(16, 16, Endian.little);
  h.setUint16(20, 1, Endian.little); // PCM
  h.setUint16(22, 1, Endian.little); // 单声道
  h.setUint32(24, sampleRate, Endian.little);
  h.setUint32(28, bytesPerSecond, Endian.little);
  h.setUint16(32, 2, Endian.little); // block align
  h.setUint16(34, 16, Endian.little); // bits per sample
  tag(36, 'data');
  h.setUint32(40, pcm.length, Endian.little);
  out.setAll(_headerBytes, pcm);
  return out;
}

/// 把连续 PCM 切成固定字节数的块；[close] 时把不足一块的尾巴也交出去。
class PcmChunker {
  PcmChunker(this.chunkBytes, this.onChunk);
  final int chunkBytes;
  final void Function(Uint8List pcm) onChunk;
  final _buffer = BytesBuilder(copy: false);

  void add(Uint8List pcm) {
    var rest = pcm;
    while (_buffer.length + rest.length >= chunkBytes) {
      final take = chunkBytes - _buffer.length;
      _buffer.add(rest.sublist(0, take));
      rest = rest.sublist(take);
      onChunk(_buffer.takeBytes());
    }
    if (rest.isNotEmpty) _buffer.add(rest);
  }

  void close() {
    if (_buffer.isNotEmpty) onChunk(_buffer.takeBytes());
  }
}
