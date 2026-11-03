import 'package:flutter/material.dart';

import '../controller.dart';
import '../device.dart';
import 'format.dart';

class ActionsPage extends StatelessWidget {
  const ActionsPage(this.c, {super.key});
  final NotaController c;

  @override
  Widget build(BuildContext context) {
    final actions = c.list('actions');
    final needsUser = c.actionsNeedingUser;
    final others = actions.where((a) => !needsUser.contains(a));
    final undone = {
      for (final a in actions)
        if (a['type'] == 'delete_calendar_event')
          (a['args'] as Map)['system_event_id'],
    };
    return Scaffold(
      appBar: AppBar(title: const Text('动作')),
      body: RefreshIndicator(
        onRefresh: c.refresh,
        child: ListView(
          padding: const EdgeInsets.symmetric(vertical: 8),
          children: [
            if (actions.isEmpty)
              const Padding(
                padding: EdgeInsets.all(32),
                child: Text(
                  'Agent 要在手机上做的事会出现在这里。',
                  textAlign: TextAlign.center,
                ),
              ),
            for (final a in needsUser) ActionCard(c, a),
            for (final a in others)
              _ActionTile(
                c,
                a,
                canUndo:
                    a['type'] == 'create_calendar_event' &&
                    a['status'] == 'done' &&
                    !undone.contains(_systemId(a)),
              ),
          ],
        ),
      ),
    );
  }

  static String? _systemId(Map<String, dynamic> a) =>
      (a['result'] as Map?)?['system_id'] as String?;
}

Future<void> _call(BuildContext context, Future<void> Function() fn) async {
  try {
    await fn();
  } catch (e) {
    if (context.mounted) showError(context, e);
  }
}

/// 需要用户动手的动作：待确认（拒绝 / 确认），或已确认待发送的消息（放弃 / 继续发送）。
class ActionCard extends StatelessWidget {
  const ActionCard(
    this.c,
    this.a, {
    this.margin = const EdgeInsets.fromLTRB(16, 8, 16, 8),
    super.key,
  });
  final NotaController c;
  final Map<String, dynamic> a;
  final EdgeInsets margin;

  @override
  Widget build(BuildContext context) {
    final (title, detail) = describeAction(a);
    final id = a['id'] as String;
    final toSend = a['status'] == 'pending';
    return Card.outlined(
      margin: margin,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              toSend ? '待发送 · $title' : title,
              style: Theme.of(context).textTheme.titleMedium,
            ),
            const SizedBox(height: 6),
            Text(detail),
            const SizedBox(height: 12),
            Row(
              mainAxisAlignment: MainAxisAlignment.end,
              children: [
                TextButton(
                  onPressed: () => _call(
                    context,
                    () => toSend ? c.abandonShare(id) : c.reject(id),
                  ),
                  child: Text(toSend ? '放弃' : '拒绝'),
                ),
                const SizedBox(width: 8),
                FilledButton(
                  onPressed: () => _call(
                    context,
                    () => toSend ? c.sendShare(a) : c.confirm(id),
                  ),
                  child: Text(toSend ? '继续发送' : '确认'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _ActionTile extends StatelessWidget {
  const _ActionTile(this.c, this.a, {required this.canUndo});
  final NotaController c;
  final Map<String, dynamic> a;
  final bool canUndo;

  @override
  Widget build(BuildContext context) {
    final (title, detail) = describeAction(a);
    final status = a['status'] as String;
    final error = (a['result'] as Map?)?['error'];
    final scheme = Theme.of(context).colorScheme;
    return ListTile(
      title: Text(title),
      subtitle: Text(
        [
          detail,
          '${actionStatusLabel[status] ?? status}${error == null ? '' : '：${actionErrorLabel[error] ?? error}'}',
        ].join('\n'),
        style: TextStyle(color: status == 'failed' ? scheme.error : null),
      ),
      isThreeLine: true,
      trailing: error == 'calendar_denied'
          ? TextButton(onPressed: openAppSettings, child: const Text('去设置'))
          : canUndo
          ? TextButton(
              onPressed: () => _call(context, () => c.undo(a['id'] as String)),
              child: const Text('撤销'),
            )
          : null,
    );
  }
}
