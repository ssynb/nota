import { spawn, execFileSync, type ChildProcess } from 'node:child_process';
import { after } from 'node:test';
import { createInterface } from 'node:readline';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';

const ROOT = new URL('..', import.meta.url).pathname;
export const TZ = 'Asia/Shanghai';

export const tempDb = () => join(mkdtempSync(join(tmpdir(), 'nota-')), 'test.db');

export function env(db: string, llmUrl: string) {
  return { ...process.env, DB_PATH: db, LLM_BASE_URL: llmUrl, LLM_API_KEY: 'test', LLM_MODEL: 'fake', PORT: '0' };
}

// A failed startup can reject before the caller holds a handle to its sibling servers;
// killing every spawned child here keeps a failing run from hanging on their open pipes.
const children = new Set<ChildProcess>();
after(() => children.forEach((c) => c.kill()));

/** Spawns a real server process; resolves once it prints its URL. */
export async function startServer(db: string, llmUrl: string, now?: string) {
  const child = spawn(process.execPath, ['--import', './test/clock.ts', 'src/server.ts'], {
    cwd: ROOT,
    env: { ...env(db, llmUrl), TEST_NOW: now ?? '' },
    stdio: ['ignore', 'pipe', 'inherit'],
  });
  children.add(child);
  child.once('exit', () => children.delete(child));
  const url = await new Promise<string>((resolve, reject) => {
    child.once('exit', (code) => reject(new Error(`server exited early: ${code}`)));
    createInterface({ input: child.stdout }).on('line', (line) => {
      const m = line.match(/http:\/\/\S+/);
      if (m) resolve(m[0]);
    });
  });
  return { url, stop: () => child.kill() };
}

export const setPlan = (db: string, userId: string, plan: string) =>
  execFileSync(process.execPath, ['src/admin.ts', userId, plan], { cwd: ROOT, env: { ...process.env, DB_PATH: db } });

export const newUser = () => randomUUID();
const headers = (user: string) => ({ 'x-user-id': user, 'x-timezone': TZ });

export type SSE = { event: string; data: any };
export type RunResponse = { status: number; events: SSE[]; done?: any; json?: any };

export async function run(base: string, user: string, text: string, opts: { mode?: string; idemKey?: string } = {}): Promise<RunResponse> {
  const res = await fetch(`${base}/api/runs`, {
    method: 'POST',
    headers: { ...headers(user), 'content-type': 'application/json' },
    body: JSON.stringify({ text, mode: opts.mode ?? 'note', idemKey: opts.idemKey ?? randomUUID() }),
  });
  if (!res.headers.get('content-type')?.startsWith('text/event-stream')) return { status: res.status, events: [], json: await res.json() };
  const events = (await res.text())
    .split('\n\n')
    .filter(Boolean)
    .map((block) => ({
      event: block.match(/^event: (.*)$/m)?.[1] ?? 'message',
      data: JSON.parse(block.match(/^data: (.*)$/m)?.[1] ?? 'null'),
    }));
  return { status: res.status, events, done: events.find((e) => e.event === 'done')?.data };
}

export async function state(base: string, user: string) {
  return (await fetch(`${base}/api/state`, { headers: headers(user) })).json();
}
