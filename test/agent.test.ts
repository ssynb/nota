import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { startFakeLLM } from './fake-llm.ts';
import { startServer, tempDb, run, state, newUser } from './support.ts';

let llm: Awaited<ReturnType<typeof startFakeLLM>>;
let server: Awaited<ReturnType<typeof startServer>>;

before(async () => {
  llm = await startFakeLLM(0);
  server = await startServer(tempDb(), llm.url);
});
after(() => {
  llm?.close();
  server?.stop();
});

test('tool calls chosen by the model are streamed as steps and their actions persisted', async () => {
  const user = newUser();
  const r = await run(server.url, user, '#full 周三下午三点开预算评审会，记得提醒我交预算');

  const stepTools = r.events.filter((e) => e.event === 'step').map((e) => e.data.tool);
  assert.deepEqual(new Set(stepTools), new Set(['create_todo', 'set_reminder', 'create_calendar_event']));
  assert.match(r.done.reply, /预算评审会/);

  const s = await state(server.url, user);
  assert.equal(s.todos[0].title, '准备预算表');
  assert.equal(s.reminders[0].remind_at, '2026-11-04T07:00:00.000Z');
  const ics = await (await fetch(`${server.url}/api/events/${s.events[0].id}.ics?uid=${user}`)).text();
  assert.match(ics, /DTSTART:20261104T070000Z/);
  assert.match(ics, /SUMMARY:预算评审会/);
});

test('a question about history is answered from past notes via search_history (two-character Chinese term)', async () => {
  const user = newUser();
  await run(server.url, user, '#full 周三下午三点开预算评审会');
  await run(server.url, user, '周末去爬山');

  const r = await run(server.url, user, '#ask 预算', { mode: 'ask' });

  assert.deepEqual(r.events.filter((e) => e.event === 'step').map((e) => e.data.tool), ['search_history']);
  assert.match(r.done.reply, /预算评审会/);
  assert.doesNotMatch(r.done.reply, /爬山/);
  assert.equal(r.done.noteId, null);
});
