import 'package:flutter/material.dart';

import '../controller.dart';
import 'format.dart';

class NotesPage extends StatelessWidget {
  const NotesPage(this.c, {super.key});
  final NotaController c;

  @override
  Widget build(BuildContext context) {
    final todos = c.list('todos');
    final conversations = c.list('conversations');
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('纪要')),
      body: RefreshIndicator(
        onRefresh: c.refresh,
        child: ListView(
          children: [
            if (conversations.isEmpty && todos.isEmpty)
              const Padding(
                padding: EdgeInsets.all(32),
                child: Text(
                  '录一段话，整理好的纪要和待办会出现在这里。',
                  textAlign: TextAlign.center,
                ),
              ),
            if (todos.isNotEmpty) ...[
              _Section('待办'),
              for (final t in todos) _TodoTile(t),
            ],
            if (conversations.isNotEmpty) _Section('对话'),
            for (final conv in conversations)
              ListTile(
                title: Text('${conv['title']}'),
                subtitle: Text(
                  '${dateTime(conv['started_at'] as String?)}\n${conv['overview']}',
                  maxLines: 3,
                  overflow: TextOverflow.ellipsis,
                  style: theme.textTheme.bodySmall,
                ),
                isThreeLine: true,
                onTap: () => Navigator.of(context).push(
                  MaterialPageRoute<void>(
                    builder: (_) => ConversationPage(conv),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _Section extends StatelessWidget {
  const _Section(this.title);
  final String title;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.fromLTRB(16, 20, 16, 4),
    child: Text(title, style: Theme.of(context).textTheme.titleMedium),
  );
}

class _TodoTile extends StatelessWidget {
  const _TodoTile(this.t);
  final Map<String, dynamic> t;

  @override
  Widget build(BuildContext context) => ListTile(
    leading: const Icon(Icons.check_box_outline_blank),
    title: Text('${t['title']}'),
    subtitle: t['due_at'] == null
        ? null
        : Text('截止 ${dateTime(t['due_at'] as String)}'),
  );
}

class ConversationPage extends StatelessWidget {
  const ConversationPage(this.conv, {super.key});
  final Map<String, dynamic> conv;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final speakers = (conv['speakers'] as List? ?? const [])
        .cast<Map<String, dynamic>>();
    final facts = (conv['facts'] as List? ?? const [])
        .cast<Map<String, dynamic>>();
    final todos = (conv['todos'] as List? ?? const [])
        .cast<Map<String, dynamic>>();
    String speaker(String? label) {
      final s = speakers.where((s) => s['label'] == label).firstOrNull;
      return s?['is_owner'] == true ? '我' : (label ?? '');
    }

    return Scaffold(
      appBar: AppBar(title: Text('${conv['title']}')),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(20, 8, 20, 32),
        children: [
          Text(
            '${dateTime(conv['started_at'] as String?)} – ${dateTime(conv['ended_at'] as String?)}',
            style: theme.textTheme.bodySmall,
          ),
          const SizedBox(height: 12),
          Text('${conv['overview']}', style: theme.textTheme.bodyLarge),
          if (todos.isNotEmpty) ...[
            const SizedBox(height: 24),
            Text('待办', style: theme.textTheme.titleMedium),
            for (final t in todos) _TodoTile(t),
          ],
          if (facts.isNotEmpty) ...[
            const SizedBox(height: 24),
            Text('要点', style: theme.textTheme.titleMedium),
            for (final f in facts)
              Padding(
                padding: const EdgeInsets.only(top: 12),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    SizedBox(
                      width: 56,
                      child: Text(
                        clock(f['offset_seconds'] as num? ?? 0),
                        style: theme.textTheme.bodySmall?.copyWith(
                          fontFeatures: const [FontFeature.tabularFigures()],
                        ),
                      ),
                    ),
                    Expanded(
                      child: Text(
                        '${speaker(f['speaker_label'] as String?)}：${f['text']}',
                      ),
                    ),
                  ],
                ),
              ),
          ],
        ],
      ),
    );
  }
}
