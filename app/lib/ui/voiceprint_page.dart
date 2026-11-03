import 'dart:async';

import 'package:flutter/material.dart';

import '../controller.dart';
import '../recorder.dart';
import '../wav.dart';
import 'format.dart';

const _minSeconds = 10;
const _script = [
  '你好，我是这台 Nota 的主人。',
  '今天下午三点和团队开周会，讨论下个月的发布计划。',
  '记得提醒我周五之前把报告发给张总。',
  '这段话用来让 Nota 认出我的声音。',
];

class VoiceprintPage extends StatefulWidget {
  const VoiceprintPage(this.c, {super.key});
  final NotaController c;

  @override
  State<VoiceprintPage> createState() => _VoiceprintPageState();
}

class _VoiceprintPageState extends State<VoiceprintPage> {
  OneShotCapture? _capture;
  Timer? _tick;
  var _busy = false;
  String? _message;

  int get _seconds => (_capture?.bytes ?? 0) ~/ bytesPerSecond;

  @override
  void dispose() {
    _tick?.cancel();
    _capture?.stop();
    super.dispose();
  }

  Future<void> _start() async {
    try {
      _capture = await OneShotCapture.start();
      _tick = Timer.periodic(
        const Duration(milliseconds: 500),
        (_) => setState(() {}),
      );
      setState(() => _message = null);
    } catch (e) {
      setState(() => _message = errorText(e));
    }
  }

  Future<void> _finish() async {
    _tick?.cancel();
    final capture = _capture!;
    setState(() {
      _capture = null;
      _busy = true;
    });
    try {
      await widget.c.enrollVoiceprint(await capture.stop());
      _message = '声纹已注册';
    } catch (e) {
      _message = errorText(e);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final recording = _capture != null;
    final enrolled = widget.c.state?['voiceprint_enrolled'] == true;
    return Scaffold(
      appBar: AppBar(title: const Text('注册声纹')),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(24, 8, 24, 32),
        children: [
          Text(
            enrolled
                ? '已注册。重新朗读会覆盖之前的声纹。'
                : '在安静的地方，用平常说话的语气读下面几句（至少 $_minSeconds 秒）。Nota 会用它在录音里认出你。',
            style: theme.textTheme.bodyMedium,
          ),
          const SizedBox(height: 24),
          for (final line in _script)
            Padding(
              padding: const EdgeInsets.only(bottom: 16),
              child: Text(line, style: theme.textTheme.titleLarge),
            ),
          const SizedBox(height: 16),
          if (recording)
            Text(
              '已录 $_seconds 秒',
              textAlign: TextAlign.center,
              style: theme.textTheme.titleMedium,
            ),
          if (_message != null) Text(_message!, textAlign: TextAlign.center),
          const SizedBox(height: 16),
          FilledButton.icon(
            onPressed: _busy
                ? null
                : recording
                ? (_seconds >= _minSeconds ? _finish : null)
                : _start,
            icon: Icon(recording ? Icons.check : Icons.mic),
            label: Text(recording ? '读完了，上传' : '开始朗读'),
          ),
        ],
      ),
    );
  }
}
