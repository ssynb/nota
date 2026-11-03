import 'dart:async';

const _min = Duration(seconds: 2);
const _max = Duration(seconds: 60);

/// 失败后按指数退避（2 秒起，封顶 60 秒）再次调用 [action]；成功一次即复位。
class RetryTimer {
  RetryTimer(this.action);
  final void Function() action;
  Timer? _timer;
  Duration _delay = _min;
  bool _disposed = false;

  void report({required bool failed}) {
    _timer?.cancel();
    if (!failed || _disposed) {
      _delay = _min;
      return;
    }
    _timer = Timer(_delay, action);
    final next = _delay * 2;
    _delay = next > _max ? _max : next;
  }

  void dispose() {
    _disposed = true;
    _timer?.cancel();
  }
}
