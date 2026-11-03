import 'package:flutter/material.dart';

import '../controller.dart';
import 'format.dart';

class AskPage extends StatefulWidget {
  const AskPage(this.c, {super.key});
  final NotaController c;

  @override
  State<AskPage> createState() => _AskPageState();
}

class _AskPageState extends State<AskPage> {
  final _input = TextEditingController();
  final _steps = <String>[];
  Map<String, dynamic>? _answer;
  String? _error;
  var _busy = false;

  @override
  void dispose() {
    _input.dispose();
    super.dispose();
  }

  Future<void> _ask() async {
    final q = _input.text.trim();
    if (q.isEmpty || _busy) return;
    setState(() {
      _busy = true;
      _steps.clear();
      _answer = null;
      _error = null;
    });
    try {
      await for (final e in await widget.c.ask(q)) {
        final data = e.json;
        setState(() {
          if (e.event == 'step') _steps.add('${data['tool']}');
          if (e.event == 'answer') _answer = data;
        });
      }
      if (_answer == null) _error = '没有拿到答案，请重试';
    } catch (e) {
      _error = errorText(e);
    } finally {
      if (mounted) setState(() => _busy = false);
      widget.c.refresh();
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final conversations = {
      for (final c in widget.c.list('conversations')) c['id']: c,
    };
    final citations = (_answer?['citations'] as List? ?? const [])
        .cast<Map<String, dynamic>>();
    return Scaffold(
      appBar: AppBar(title: const Text('提问')),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(20, 8, 20, 32),
        children: [
          TextField(
            controller: _input,
            textInputAction: TextInputAction.search,
            onSubmitted: (_) => _ask(),
            decoration: InputDecoration(
              hintText: '比如：上周和张总约了什么时间？',
              border: const OutlineInputBorder(),
              suffixIcon: IconButton(
                tooltip: '提问',
                icon: const Icon(Icons.arrow_upward),
                onPressed: _busy ? null : _ask,
              ),
            ),
          ),
          const SizedBox(height: 16),
          if (_busy) const LinearProgressIndicator(),
          for (final s in _steps)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text('Agent · $s', style: theme.textTheme.labelLarge),
            ),
          if (_error != null)
            Text(_error!, style: TextStyle(color: theme.colorScheme.error)),
          if (_answer != null) ...[
            const SizedBox(height: 16),
            Text('${_answer!['text']}', style: theme.textTheme.bodyLarge),
            if (citations.isNotEmpty) ...[
              const SizedBox(height: 20),
              Text('出处', style: theme.textTheme.titleSmall),
              for (final c in citations)
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  leading: const Icon(Icons.format_quote_outlined),
                  title: Text(
                    '${conversations[c['conversation_id']]?['title'] ?? '对话'}',
                  ),
                  subtitle: Text(
                    '${dateTime(c['started_at'] as String?)} · 第 ${clock(c['offset_seconds'] as num? ?? 0)}',
                  ),
                ),
            ],
          ],
        ],
      ),
    );
  }
}
