// Load test against real server processes: two processes share one SQLite file,
// the model is the local fake reached through LLM_BASE_URL.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { startFakeLLM } from './fake-llm.ts';
import { startServer, tempDb, run, state, newUser, setPlan } from './support.ts';

const db = tempDb();
let llm: Awaited<ReturnType<typeof startFakeLLM>>;
let servers: Awaited<ReturnType<typeof startServer>>[];
const base = (i: number) => servers[i % servers.length].url;

before(async () => {
  llm = await startFakeLLM();
  servers = await Promise.all([startServer(db, llm.url), startServer(db, llm.url)]);
});
after(() => {
  llm?.close();
  servers?.forEach((s) => s.stop());
});

test('free user: 50 concurrent distinct requests across two processes, exactly 10 succeed', async () => {
  const user = newUser();
  const results = await Promise.all(Array.from({ length: 50 }, (_, i) => run(base(i), user, `笔记 ${i}`)));

  assert.equal(results.filter((r) => r.done).length, 10);
  assert.equal(results.filter((r) => r.status === 429).length, 40);
  const s = await state(base(0), user);
  assert.equal(s.user.used, 10);
  assert.equal(s.notes.length, 10);
});

test('same idempotency key submitted 20 times concurrently is charged once and replays one result', async () => {
  const user = newUser();
  const idemKey = randomUUID();
  const results = await Promise.all(Array.from({ length: 20 }, (_, i) => run(base(i), user, '买牛奶', { idemKey })));
  const retry = await run(base(0), user, '买牛奶', { idemKey });

  const noteIds = new Set([...results, retry].filter((r) => r.done).map((r) => r.done.noteId));
  assert.equal(noteIds.size, 1);
  assert.ok(retry.done, 'retry after completion replays the stored result');
  const s = await state(base(0), user);
  assert.equal(s.user.used, 1);
  assert.equal(s.notes.length, 1);
  assert.equal(s.todos.length, 1);
});

test('model failure refunds the charge and leaves no partial actions', async () => {
  const user = newUser();
  const r = await run(base(0), user, '#fail 整理一下');

  assert.ok(r.events.some((e) => e.event === 'error'));
  const s = await state(base(0), user);
  assert.equal(s.user.used, 0);
  assert.equal(s.todos.length, 0);
});

test('pro user is not limited', async () => {
  const user = newUser();
  await state(base(0), user);
  setPlan(db, user, 'pro');
  const results = await Promise.all(Array.from({ length: 15 }, (_, i) => run(base(i), user, `pro ${i}`)));
  assert.equal(results.filter((r) => r.done).length, 15);
});

test("quota resets at midnight in the user's timezone, not UTC", async () => {
  const user = newUser();
  // 23:59 and 00:01 in Asia/Shanghai; both instants are still 2026-11-02 in UTC.
  const lateNight = await startServer(db, llm.url, '2026-11-02T15:59:00Z');
  await Promise.all(Array.from({ length: 10 }, (_, i) => run(lateNight.url, user, `夜 ${i}`)));
  const blocked = await run(lateNight.url, user, '第 11 次');
  lateNight.stop();

  const nextDay = await startServer(db, llm.url, '2026-11-02T16:01:00Z');
  const allowed = await run(nextDay.url, user, '新的一天');
  const s = await state(nextDay.url, user);
  nextDay.stop();

  assert.equal(blocked.status, 429);
  assert.ok(allowed.done);
  assert.equal(s.user.used, 1);
});
