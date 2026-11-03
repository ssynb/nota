import 'package:flutter/material.dart';

import '../controller.dart';
import 'format.dart';

class ActionsPage extends StatelessWidget {
  const ActionsPage(this.c, {super.key});
  final NotaController c;

  @override
  Widget build(BuildContext context) {
    final actions = c.list('actions');
    final toConfirm = actions.where(
      (a) => a['status'] == 'pending_confirmation',
    );
    final others = actions.where((a) => a['status'] != 'pending_confirmation');
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
            for (final a in toConfirm) _ConfirmCard(c, a),
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

class _ConfirmCard extends StatelessWidget {
  const _ConfirmCard(this.c, this.a);
  final NotaController c;
  final Map<String, dynamic> a;

  @override
  Widget build(BuildContext context) {
    final (title, detail) = describeAction(a);
    final id = a['id'] as String;
    return Card.outlined(
      margin: const EdgeInsets.fromLTRB(16, 8, 16, 8),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(title, style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 6),
            Text(detail),
            const SizedBox(height: 12),
            Row(
              mainAxisAlignment: MainAxisAlignment.end,
              children: [
                TextButton(
                  onPressed: () => _call(context, () => c.reject(id)),
                  child: const Text('拒绝'),
                ),
                const SizedBox(width: 8),
                FilledButton(
                  onPressed: () => _call(context, () => c.confirm(id)),
                  child: const Text('确认'),
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
          '${actionStatusLabel[status] ?? status}${error == null ? '' : '：$error'}',
        ].join('\n'),
        style: TextStyle(color: status == 'failed' ? scheme.error : null),
      ),
      isThreeLine: true,
      trailing: canUndo
          ? TextButton(
              onPressed: () => _call(context, () => c.undo(a['id'] as String)),
              child: const Text('撤销'),
            )
          : null,
    );
  }
}
