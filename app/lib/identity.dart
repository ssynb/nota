import 'dart:math';

import 'package:shared_preferences/shared_preferences.dart';

const _userIdKey = 'user_id';
final _random = Random.secure();

/// UUID v4。
String newUuid() {
  final b = List.generate(16, (_) => _random.nextInt(256));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  final hex = b.map((x) => x.toRadixString(16).padLeft(2, '0')).join();
  return '${hex.substring(0, 8)}-${hex.substring(8, 12)}-'
      '${hex.substring(12, 16)}-${hex.substring(16, 20)}-${hex.substring(20)}';
}

/// 首次调用生成 UUID 并保存，之后返回同一个。
String loadUserId(SharedPreferences prefs) {
  final saved = prefs.getString(_userIdKey);
  if (saved != null) return saved;
  final id = newUuid();
  prefs.setString(_userIdKey, id);
  return id;
}
