import 'dart:async';

import 'package:flutter/material.dart';

import '../controller.dart';
import 'format.dart';
import 'settings_page.dart';
import 'voiceprint_page.dart';

class RecordPage extends StatelessWidget {
  const RecordPage(this.c, {super.key});
  final NotaController c;

  @override
  Widget build(BuildContext context) {
    final upload = c.uploader.state.value;
    final enrolled = c.state?['voiceprint_enrolled'] == true;
    return Scaffold(
      appBar: AppBar(
        title: const Text('Nota'),
        actions: [
          IconButton(
            tooltip: enrolled ? '声纹已注册' : '注册声纹',
            icon: Badge(
              isLabelVisible: c.state != null && !enrolled,
              smallSize: 8,
              child: const Icon(Icons.record_voice_over_outlined),
            ),
            onPressed: () => _push(context, VoiceprintPage(c)),
          ),
          IconButton(
            tooltip: '设置',
            icon: const Icon(Icons.settings_outlined),
            onPressed: () => _push(context, SettingsPage(c)),
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: c.refresh,
        child: ListView(
          padding: const EdgeInsets.fromLTRB(20, 8, 20, 32),
          children: [
            if (!c.connected) _Banner('未连上服务器，录音会先存在手机上', c.connectionError),
            if (c.runner.error.value != null)
              _Banner('动作执行或回报失败，联网后自动重试', c.runner.error.value),
            const SizedBox(height: 24),
            Center(child: _RecordButton(c)),
            const SizedBox(height: 12),
            Center(
              child: Text(
                upload.unsent > 0
                    ? '已录音，待上传 ${upload.unsent} 个分片'
                    : '分片每 ${c.chunkSeconds} 秒上传一次',
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ),
            const SizedBox(height: 32),
            if (c.quota != null) _Quota(c.quota!),
            const SizedBox(height: 24),
            for (final MapEntry(key: id, value: r)
                in c.live.entries.toList().reversed)
              _LiveCard(id, r),
          ],
        ),
      ),
    );
  }

  void _push(BuildContext context, Widget page) =>
      Navigator.of(context).push(MaterialPageRoute<void>(builder: (_) => page));
}

class _Banner extends StatelessWidget {
  const _Banner(this.text, this.detail);
  final String text;
  final String? detail;

  @override
  Widget build(BuildContext context) => Card(
    color: Theme.of(context).colorScheme.errorContainer,
    child: ListTile(
      leading: const Icon(Icons.cloud_off_outlined),
      title: Text(text),
      subtitle: detail == null
          ? null
          : Text(detail!, maxLines: 2, overflow: TextOverflow.ellipsis),
    ),
  );
}

class _RecordButton extends StatefulWidget {
  const _RecordButton(this.c);
  final NotaController c;

  @override
  State<_RecordButton> createState() => _RecordButtonState();
}

class _RecordButtonState extends State<_RecordButton> {
  Timer? _tick;
  var _busy = false;

  @override
  void dispose() {
    _tick?.cancel();
    super.dispose();
  }

  Future<void> _toggle() async {
    setState(() => _busy = true);
    try {
      if (widget.c.session == null) {
        await widget.c.startRecording();
        _tick = Timer.periodic(
          const Duration(seconds: 1),
          (_) => setState(() {}),
        );
      } else {
        _tick?.cancel();
        await widget.c.stopRecording();
      }
    } catch (e) {
      if (mounted) showError(context, e);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final session = widget.c.session;
    final scheme = Theme.of(context).colorScheme;
    final recording = session != null;
    return Column(
      children: [
        SizedBox.square(
          dimension: 132,
          child: FilledButton(
            onPressed: _busy ? null : _toggle,
            style: FilledButton.styleFrom(
              shape: const CircleBorder(),
              backgroundColor: recording ? scheme.error : scheme.primary,
            ),
            child: Icon(recording ? Icons.stop_rounded : Icons.mic, size: 56),
          ),
        ),
        const SizedBox(height: 16),
        Text(
          recording
              ? clock(DateTime.now().difference(session.startedAt).inSeconds)
              : '点击开始录音',
          style: Theme.of(context).textTheme.headlineSmall
              ?.copyWith(fontFeatures: const [FontFeature.tabularFigures()]),
        ),
      ],
    );
  }
}

class _Quota extends StatelessWidget {
  const _Quota(this.q);
  final Map<String, dynamic> q;

  @override
  Widget build(BuildContext context) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      Text('额度', style: Theme.of(context).textTheme.titleMedium),
      const SizedBox(height: 8),
      _Ledger('本月录音分钟', q['minutes']),
      _Ledger('今日录音次数', q['recordings_today']),
      _Ledger('本月提问', q['asks']),
    ],
  );
}

class _Ledger extends StatelessWidget {
  const _Ledger(this.label, this.l);
  final String label;
  final Map<String, dynamic> l;

  @override
  Widget build(BuildContext context) {
    final used = l['used'] as num;
    final limit = l['limit'] as num?;
    final exhausted = limit != null && used >= limit;
    final scheme = Theme.of(context).colorScheme;
    String n(num v) => v % 1 == 0 ? '${v.toInt()}' : v.toStringAsFixed(1);
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(child: Text(label)),
              Text(
                limit == null ? '${n(used)} / 不限' : '${n(used)} / ${n(limit)}',
                style: TextStyle(color: exhausted ? scheme.error : null),
              ),
            ],
          ),
          const SizedBox(height: 4),
          LinearProgressIndicator(
            value: limit == null ? 0 : (used / limit).clamp(0, 1).toDouble(),
            color: exhausted ? scheme.error : null,
            borderRadius: BorderRadius.circular(2),
          ),
        ],
      ),
    );
  }
}

class _LiveCard extends StatelessWidget {
  const _LiveCard(this.id, this.r);
  final String id;
  final LiveRecording r;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card.outlined(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              recordingStatusLabel[r.status] ?? r.status,
              style: theme.textTheme.titleMedium?.copyWith(
                color: r.status == 'failed' ? theme.colorScheme.error : null,
              ),
            ),
            if (r.error != null)
              Text(r.error!, style: theme.textTheme.bodySmall),
            for (final s in r.steps) ...[
              const SizedBox(height: 8),
              Text('Agent · ${s['tool']}', style: theme.textTheme.labelLarge),
              Text(
                '${s['args']}',
                style: theme.textTheme.bodySmall,
                maxLines: 3,
                overflow: TextOverflow.ellipsis,
              ),
            ],
          ],
        ),
      ),
    );
  }
}
