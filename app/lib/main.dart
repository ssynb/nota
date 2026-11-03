import 'package:flutter/material.dart';
import 'package:path_provider/path_provider.dart';

import 'controller.dart';
import 'ui/actions_page.dart';
import 'ui/ask_page.dart';
import 'ui/notes_page.dart';
import 'ui/record_page.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final controller = await NotaController.create(
    await getApplicationSupportDirectory(),
  );
  runApp(NotaApp(controller));
}

class NotaApp extends StatelessWidget {
  const NotaApp(this.c, {super.key});
  final NotaController c;

  @override
  Widget build(BuildContext context) => MaterialApp(
    title: 'Nota',
    debugShowCheckedModeBanner: false,
    locale: const Locale('zh', 'CN'),
    theme: ThemeData(
      colorSchemeSeed: const Color(0xFF3B5BDB),
      useMaterial3: true,
    ),
    home: _Shell(c),
  );
}

class _Shell extends StatefulWidget {
  const _Shell(this.c);
  final NotaController c;

  @override
  State<_Shell> createState() => _ShellState();
}

class _ShellState extends State<_Shell> {
  var _tab = 0;

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: widget.c,
    builder: (context, _) {
      final needsUser = widget.c.actionsNeedingUser.length;
      return Scaffold(
        body: IndexedStack(
          index: _tab,
          children: [
            RecordPage(widget.c),
            NotesPage(widget.c),
            ActionsPage(widget.c),
            AskPage(widget.c),
          ],
        ),
        bottomNavigationBar: NavigationBar(
          selectedIndex: _tab,
          onDestinationSelected: (i) => setState(() => _tab = i),
          destinations: [
            const NavigationDestination(
              icon: Icon(Icons.mic_none),
              label: '录音',
            ),
            const NavigationDestination(icon: Icon(Icons.notes), label: '纪要'),
            NavigationDestination(
              icon: Badge(
                isLabelVisible: needsUser > 0,
                label: Text('$needsUser'),
                child: const Icon(Icons.event_available_outlined),
              ),
              label: '动作',
            ),
            const NavigationDestination(icon: Icon(Icons.search), label: '提问'),
          ],
        ),
      );
    },
  );
}
