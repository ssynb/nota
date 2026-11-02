import { DatabaseSync } from 'node:sqlite';
import { randomUUID } from 'node:crypto';

export const FREE_DAILY_LIMIT = 10;
/** A running run's owner renews this lease; once it lapses (process died) the run is released. */
export const RUN_LEASE_MS = 30_000;
const BUSY_TIMEOUT_MS = 5000;

export const db = new DatabaseSync(process.env.DB_PATH ?? 'nota.db', { timeout: BUSY_TIMEOUT_MS });
const SCHEMA = `
  CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    plan TEXT NOT NULL DEFAULT 'free' CHECK (plan IN ('free', 'pro')),
    tz TEXT NOT NULL
  );
  CREATE TABLE IF NOT EXISTS usage (
    user_id TEXT NOT NULL, day TEXT NOT NULL, used INTEGER NOT NULL,
    PRIMARY KEY (user_id, day)
  );
  CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL, idem_key TEXT NOT NULL,
    day TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed')),
    owner TEXT, lease_until TEXT,
    result TEXT,
    UNIQUE (user_id, idem_key)
  );
  CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, run_id INTEGER NOT NULL,
    text TEXT NOT NULL, summary TEXT NOT NULL, created_at TEXT NOT NULL
  );
  CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, run_id INTEGER NOT NULL,
    title TEXT NOT NULL, due_at TEXT, created_at TEXT NOT NULL
  );
  CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, run_id INTEGER NOT NULL,
    content TEXT NOT NULL, remind_at TEXT NOT NULL, created_at TEXT NOT NULL
  );
  CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, run_id INTEGER NOT NULL,
    title TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL, location TEXT, created_at TEXT NOT NULL
  );
`;

export type Plan = 'free' | 'pro';
export type User = { id: string; plan: Plan; tz: string };
export type Mode = 'note' | 'ask';
export type Step = { tool: string; args: unknown; result: unknown };
export type RunResult = { mode: Mode; reply: string; noteId: number | null; steps: Step[] };
export type Reservation =
  | { kind: 'run'; runId: number; owner: string }
  | { kind: 'replay'; result: RunResult }
  | { kind: 'busy' }
  | { kind: 'quota'; used: number; limit: number };

/** Every write path that touches more than one row goes through here; IMMEDIATE takes the write lock up front. */
function tx<T>(fn: () => T): T {
  db.exec('BEGIN IMMEDIATE');
  try {
    const result = fn();
    db.exec('COMMIT');
    return result;
  } catch (e) {
    db.exec('ROLLBACK');
    throw e;
  }
}

const now = () => new Date().toISOString();
const leaseUntil = () => new Date(Date.now() + RUN_LEASE_MS).toISOString();

/**
 * Databases created before run leases lack owner/lease_until. Their running rows have no owner that could still
 * finish them, so they get an already-expired lease and are released (refund + undo) once, on next access.
 */
function migrate() {
  const columns = (db.prepare('PRAGMA table_info(runs)').all() as { name: string }[]).map((c) => c.name);
  if (columns.includes('lease_until')) return;
  db.exec('ALTER TABLE runs ADD COLUMN owner TEXT; ALTER TABLE runs ADD COLUMN lease_until TEXT;');
  db.prepare("UPDATE runs SET lease_until = ? WHERE status = 'running'").run(new Date(0).toISOString());
}

// Several processes may open a fresh file at once. Schema creation and migration run under the write lock, where
// the busy timeout applies; the WAL switch comes after and is persistent, so later opens find it already set.
tx(() => {
  db.exec(SCHEMA);
  migrate();
});
db.exec('PRAGMA journal_mode = WAL');

/** Calendar date (YYYY-MM-DD) of `at` in the user's timezone; the quota day key. */
export const localDay = (tz: string, at = new Date()) => at.toLocaleDateString('sv-SE', { timeZone: tz });

/** Creates the user on first sight. Timezone is fixed at creation so switching it cannot reopen a spent day. */
export function ensureUser(id: string, tz: string): User {
  db.prepare('INSERT OR IGNORE INTO users (id, tz) VALUES (?, ?)').run(id, tz);
  return db.prepare('SELECT id, plan, tz FROM users WHERE id = ?').get(id) as User;
}

export function setPlan(userId: string, plan: Plan): boolean {
  return db.prepare('UPDATE users SET plan = ? WHERE id = ?').run(plan, userId).changes === 1;
}

const limitOf = (user: User) => (user.plan === 'free' ? FREE_DAILY_LIMIT : null);

/** Undo every action the run took, refund its unit and mark it failed. Call inside tx. */
function release(runId: number) {
  for (const table of ['todos', 'reminders', 'events']) db.prepare(`DELETE FROM ${table} WHERE run_id = ?`).run(runId);
  db.prepare('UPDATE usage SET used = used - 1 WHERE (user_id, day) = (SELECT user_id, day FROM runs WHERE id = ?)').run(runId);
  db.prepare("UPDATE runs SET status = 'failed' WHERE id = ?").run(runId);
}

/** Runs whose owner stopped renewing the lease died with their process; treat them as failed. Call inside tx. */
function releaseExpired(userId: string) {
  const expired = db.prepare("SELECT id FROM runs WHERE user_id = ? AND status = 'running' AND lease_until < ?").all(userId, now()) as { id: number }[];
  for (const { id } of expired) release(id);
}

/** Proof that the holder still owns a running run; every write made on the run's behalf must present it. */
export type Lease = { userId: string; runId: number; owner: string };

/** The run was released or taken over; the holder must stop and leave everything to the current owner. */
export class LeaseLostError extends Error {}

const OWNED = "EXISTS (SELECT 1 FROM runs WHERE id = :runId AND owner = :owner AND status = 'running')";
const owns = (runId: number, owner: string) => (db.prepare(`SELECT ${OWNED} AS owned`).get({ runId, owner }) as { owned: number }).owned === 1;

/**
 * Claims the idempotency key and charges one unit atomically.
 * A key that previously failed may be claimed again; done keys replay; running keys are busy.
 */
export function reserve(user: User, idemKey: string): Reservation {
  return tx(() => {
    releaseExpired(user.id);
    const day = localDay(user.tz);
    const owner = randomUUID();
    const claimed = db
      .prepare(
        `INSERT INTO runs (user_id, idem_key, day, status, owner, lease_until) VALUES (?, ?, ?, 'running', ?, ?)
         ON CONFLICT (user_id, idem_key) DO UPDATE SET status = 'running', day = excluded.day, owner = excluded.owner, lease_until = excluded.lease_until
         WHERE status = 'failed'
         RETURNING id`,
      )
      .get(user.id, idemKey, day, owner, leaseUntil()) as { id: number } | undefined;
    if (!claimed) {
      const run = db.prepare('SELECT status, result FROM runs WHERE user_id = ? AND idem_key = ?').get(user.id, idemKey) as { status: string; result: string | null };
      return run.status === 'done' ? { kind: 'replay', result: JSON.parse(run.result!) } : { kind: 'busy' };
    }
    const limit = limitOf(user);
    const charged = db
      .prepare(
        `INSERT INTO usage (user_id, day, used) VALUES (?, ?, 1)
         ON CONFLICT (user_id, day) DO UPDATE SET used = used + 1 WHERE :limit IS NULL OR used < :limit`,
      )
      .run({ limit }, user.id, day);
    if (charged.changes === 1) return { kind: 'run', runId: claimed.id, owner };
    db.prepare('DELETE FROM runs WHERE id = ?').run(claimed.id);
    return { kind: 'quota', used: usedOn(user.id, day), limit: limit! };
  });
}

export function renewLease(runId: number, owner: string) {
  db.prepare("UPDATE runs SET lease_until = ? WHERE id = ? AND owner = ? AND status = 'running'").run(leaseUntil(), runId, owner);
}

export function finish(user: User, runId: number, owner: string, text: string, result: Omit<RunResult, 'noteId'>): RunResult {
  return tx(() => {
    if (!owns(runId, owner)) throw new LeaseLostError(`run ${runId} lease lost before it finished`);
    const note =
      result.mode === 'note'
        ? (db.prepare('INSERT INTO notes (user_id, run_id, text, summary, created_at) VALUES (?, ?, ?, ?, ?) RETURNING id').get(user.id, runId, text, result.reply, now()) as { id: number })
        : null;
    const full: RunResult = { ...result, noteId: note?.id ?? null };
    db.prepare("UPDATE runs SET status = 'done', result = ? WHERE id = ?").run(JSON.stringify(full), runId);
    return full;
  });
}

/** Failed run: refund the unit and undo its actions, unless the lease already passed to someone else. */
export function fail(runId: number, owner: string) {
  tx(() => {
    if (owns(runId, owner)) release(runId);
  });
}

const usedOn = (userId: string, day: string) =>
  (db.prepare('SELECT used FROM usage WHERE user_id = ? AND day = ?').get(userId, day) as { used: number } | undefined)?.used ?? 0;

/** Inserts an action row only while the lease is held; the ownership check and the write are one statement. */
function insertOwned(table: string, lease: Lease, values: Record<string, string | null>) {
  const columns = Object.keys(values);
  const row = db
    .prepare(
      `INSERT INTO ${table} (user_id, run_id, created_at, ${columns.join(', ')})
       SELECT :userId, :runId, :createdAt, ${columns.map((c) => `:${c}`).join(', ')} WHERE ${OWNED} RETURNING id`,
    )
    .get({ userId: lease.userId, runId: lease.runId, owner: lease.owner, createdAt: now(), ...values }) as { id: number } | undefined;
  if (!row) throw new LeaseLostError(`run ${lease.runId} lease lost; ${table} not written`);
  return row;
}

export const addTodo = (lease: Lease, title: string, dueAt: string | null) => insertOwned('todos', lease, { title, due_at: dueAt });

export const addReminder = (lease: Lease, content: string, remindAt: string) => insertOwned('reminders', lease, { content, remind_at: remindAt });

export const addEvent = (lease: Lease, e: { title: string; start: string; end: string; location: string | null }) =>
  insertOwned('events', lease, { title: e.title, start_at: e.start, end_at: e.end, location: e.location });

export function getEvent(userId: string, id: number) {
  return db.prepare('SELECT * FROM events WHERE user_id = ? AND id = ?').get(userId, id) as
    | { id: number; title: string; start_at: string; end_at: string; location: string | null; created_at: string }
    | undefined;
}

const HISTORY_SQL = `
  SELECT 'note' AS type, text || ' / ' || summary AS text, created_at AS at FROM notes WHERE user_id = :u
  UNION ALL SELECT 'todo', title, coalesce(due_at, created_at) FROM todos WHERE user_id = :u
  UNION ALL SELECT 'reminder', content, remind_at FROM reminders WHERE user_id = :u
  UNION ALL SELECT 'event', title || coalesce(' @' || location, ''), start_at FROM events WHERE user_id = :u
  ORDER BY at DESC LIMIT 1000`;
const SEARCH_LIMIT = 10;

/** Substring match on model-supplied keywords, ranked by how many keywords hit. */
export function searchHistory(userId: string, query: string) {
  const terms = query.split(/[\s,，、。;；]+/).filter(Boolean);
  const rows = db.prepare(HISTORY_SQL).all({ u: userId }) as { type: string; text: string; at: string }[];
  return rows
    .map((row) => ({ row, hits: terms.filter((t) => row.text.includes(t)).length }))
    .filter((r) => r.hits > 0)
    .sort((a, b) => b.hits - a.hits)
    .slice(0, SEARCH_LIMIT)
    .map((r) => r.row);
}

export function userState(user: User) {
  tx(() => releaseExpired(user.id));
  const q = (sql: string) => db.prepare(sql).all(user.id);
  return {
    user: { ...user, used: usedOn(user.id, localDay(user.tz)), limit: limitOf(user) },
    notes: q('SELECT id, text, summary, created_at FROM notes WHERE user_id = ? ORDER BY id DESC LIMIT 50'),
    todos: q('SELECT id, title, due_at FROM todos WHERE user_id = ? ORDER BY id DESC LIMIT 100'),
    reminders: q('SELECT id, content, remind_at FROM reminders WHERE user_id = ? ORDER BY remind_at DESC LIMIT 100'),
    events: q('SELECT id, title, start_at, end_at, location FROM events WHERE user_id = ? ORDER BY start_at DESC LIMIT 100'),
  };
}
