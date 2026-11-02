// Effectiveness eval against the real model: npm run eval [-- <case id substring>]
import { readFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

process.env.DB_PATH = join(mkdtempSync(join(tmpdir(), 'nota-eval-')), 'eval.db');
const { db, ensureUser, reserve } = await import('../src/db.ts');
const { runAgent, localTime } = await import('../src/agent.ts');
type Step = import('../src/db.ts').Step;

type Expectation = { tool: string; args?: Record<string, string> };
type Case = { id: string; input: string; mode?: 'note' | 'ask'; history?: string[]; expect: Expectation[]; forbid?: string[]; reply?: string };
const suite = JSON.parse(readFileSync(new URL('cases.json', import.meta.url), 'utf8')) as { now: string; tz: string; cases: Case[] };

const MIN_PASS_RATE = 0.8;
const CONCURRENCY = 4;
const TIME_FIELDS = new Set(['remind_at', 'start', 'end', 'due']);
const NOW = new Date(suite.now);

/** Time fields compare as local wall-clock prefixes ("2026-11-03 08:00"); text fields as regex. */
function argMatches(field: string, expected: string, actual: unknown) {
  if (typeof actual !== 'string') return false;
  if (TIME_FIELDS.has(field)) return !Number.isNaN(Date.parse(actual)) && localTime(actual, suite.tz).startsWith(expected);
  return new RegExp(expected).test(actual);
}

function judge(c: Case, steps: Step[], reply: string): string[] {
  const ok = steps.filter((s) => !(s.result as { error?: string }).error);
  const problems = c.expect
    .filter((e) => !ok.some((s) => s.tool === e.tool && Object.entries(e.args ?? {}).every(([k, v]) => argMatches(k, v, (s.args as Record<string, unknown>)[k]))))
    .map((e) => `缺少 ${e.tool} ${JSON.stringify(e.args ?? {})}`);
  for (const tool of c.forbid ?? []) if (steps.some((s) => s.tool === tool)) problems.push(`不应调用 ${tool}`);
  if (c.reply && !new RegExp(c.reply).test(reply)) problems.push(`回复不含 /${c.reply}/`);
  return problems;
}

async function evaluate(c: Case, i: number) {
  const userId = `eval-${i}`;
  const reservation = reserve(ensureUser(userId, suite.tz), 'eval');
  if (reservation.kind !== 'run') throw new Error(`eval user ${userId} could not reserve a run: ${reservation.kind}`);
  for (const text of c.history ?? []) {
    db.prepare('INSERT INTO notes (user_id, run_id, text, summary, created_at) VALUES (?, 0, ?, ?, ?)').run(userId, text, text, new Date(NOW.getTime() - 86_400_000).toISOString());
  }
  const started = Date.now();
  try {
    const out = await runAgent({ userId, runId: reservation.runId, owner: reservation.owner, tz: suite.tz, mode: c.mode ?? 'note', now: NOW }, c.input, () => {});
    return { c, steps: out.steps, reply: out.reply, problems: judge(c, out.steps, out.reply), ms: Date.now() - started };
  } catch (e) {
    return { c, steps: [], reply: '', problems: [`运行失败：${(e as Error).message}`], ms: Date.now() - started };
  }
}

const filter = process.argv[2];
const cases = suite.cases.filter((c) => !filter || c.id.includes(filter));
const results: Awaited<ReturnType<typeof evaluate>>[] = [];
let next = 0;
await Promise.all(
  Array.from({ length: CONCURRENCY }, async () => {
    while (next < cases.length) {
      const i = next++;
      results[i] = await evaluate(cases[i], i);
    }
  }),
);

for (const r of results) {
  const calls = r.steps.map((s) => `${s.tool}(${JSON.stringify(s.args)})`).join(' ');
  console.log(`${r.problems.length ? '✗' : '✓'} ${r.c.id}  [${(r.ms / 1000).toFixed(1)}s]`);
  if (r.problems.length) console.log(`    问题：${r.problems.join('；')}\n    调用：${calls || '无'}\n    回复：${r.reply.replace(/\n/g, ' ').slice(0, 160)}`);
}
const passed = results.filter((r) => !r.problems.length).length;
const rate = passed / results.length;
console.log(`\n通过 ${passed}/${results.length}（${(rate * 100).toFixed(0)}%），模型 ${process.env.LLM_MODEL ?? 'deepseek-flash'}`);
process.exit(rate >= MIN_PASS_RATE ? 0 : 1);
