import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../controller.dart';

class SettingsPage extends StatefulWidget {
  const SettingsPage(this.c, {super.key});
  final NotaController c;

  @override
  State<SettingsPage> createState() => _SettingsPageState();
}

class _SettingsPageState extends State<SettingsPage> {
  late final _url = TextEditingController(text: widget.c.baseUrl);
  late final _chunk = TextEditingController(text: '${widget.c.chunkSeconds}');

  @override
  void dispose() {
    _url.dispose();
    _chunk.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    final seconds = int.tryParse(_chunk.text);
    final url = Uri.tryParse(_url.text.trim());
    if (seconds == null ||
        seconds < 1 ||
        seconds > 60 ||
        url == null ||
        !url.hasScheme) {
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('请填写有效地址，分片时长 1–60 秒')));
      return;
    }
    await widget.c.updateSettings(url.toString(), seconds);
    if (mounted) Navigator.of(context).pop();
  }

  @override
  Widget build(BuildContext context) {
    final user = widget.c.api;
    return Scaffold(
      appBar: AppBar(
        title: const Text('设置'),
        actions: [TextButton(onPressed: _save, child: const Text('保存'))],
      ),
      body: ListView(
        padding: const EdgeInsets.all(20),
        children: [
          TextField(
            controller: _url,
            keyboardType: TextInputType.url,
            autocorrect: false,
            decoration: const InputDecoration(
              labelText: '服务器地址',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 16),
          TextField(
            controller: _chunk,
            keyboardType: TextInputType.number,
            decoration: const InputDecoration(
              labelText: '分片时长（秒，不超过 60）',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 24),
          ListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('用户 ID'),
            subtitle: Text(user.userId),
            trailing: IconButton(
              tooltip: '复制',
              icon: const Icon(Icons.copy),
              onPressed: () =>
                  Clipboard.setData(ClipboardData(text: user.userId)),
            ),
          ),
          ListTile(
            contentPadding: EdgeInsets.zero,
            title: const Text('时区'),
            subtitle: Text(user.timezone),
          ),
        ],
      ),
    );
  }
}
