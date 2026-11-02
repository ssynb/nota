import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { readFileSync } from 'node:fs';
import type { AddressInfo } from 'node:net';
import { runAgent } from './agent.ts';
import { ensureUser, reserve, finish, fail, userState, getEvent, type Mode, type User } from './db.ts';

const INDEX_HTML = readFileSync(new URL('../public/index.html', import.meta.url));
const MAX_TEXT = 4000;
const MAX_ID = 64;
const MODES: Mode[] = ['note', 'ask'];

class HttpError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function sendJson(res: ServerResponse, status: number, body: unknown) {
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(body));
}

const isTimeZone = (tz: string) => Intl.supportedValuesOf('timeZone').includes(tz) || tz === 'UTC';

/** Demo identity: a client-generated id. The timezone only matters when the user is first created. */
function currentUser(req: IncomingMessage, url: URL): User {
  const id = (req.headers['x-user-id'] as string | undefined) ?? url.searchParams.get('uid') ?? '';
  const tz = (req.headers['x-timezone'] as string | undefined) ?? 'UTC';
  if (!id || id.length > MAX_ID) throw new HttpError(400, 'missing or invalid X-User-Id');
  if (!isTimeZone(tz)) throw new HttpError(400, `unknown timezone ${tz}`);
  return ensureUser(id, tz);
}

async function readBody(req: IncomingMessage) {
  let raw = '';
  for await (const chunk of req) raw += chunk;
  const body = JSON.parse(raw || '{}');
  const text = typeof body.text === 'string' ? body.text.trim() : '';
  if (!text || text.length > MAX_TEXT) throw new HttpError(400, `text must be 1-${MAX_TEXT} characters`);
  if (typeof body.idemKey !== 'string' || !body.idemKey || body.idemKey.length > MAX_ID) throw new HttpError(400, 'missing idemKey');
  if (!MODES.includes(body.mode)) throw new HttpError(400, `mode must be one of ${MODES.join(', ')}`);
  return { text, idemKey: body.idemKey as string, mode: body.mode as Mode };
}

async function handleRun(req: IncomingMessage, res: ServerResponse, user: User) {
  const { text, idemKey, mode } = await readBody(req);
  const reservation = reserve(user, idemKey);
  if (reservation.kind === 'quota') return sendJson(res, 429, { error: 'quota_exceeded', used: reservation.used, limit: reservation.limit });
  if (reservation.kind === 'busy') return sendJson(res, 409, { error: 'in_progress' });

  res.writeHead(200, { 'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-cache' });
  const send = (event: string, data: unknown) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  if (reservation.kind === 'replay') {
    send('done', reservation.result);
    return res.end();
  }
  const { runId } = reservation;
  try {
    const out = await runAgent({ userId: user.id, tz: user.tz, runId, mode, now: new Date() }, text, (step) => send('step', step));
    send('done', finish(user, runId, text, out));
  } catch (e) {
    fail(runId);
    console.error(`run ${runId} failed:`, e);
    send('error', { message: (e as Error).message });
  }
  res.end();
}

const icsText = (s: string) => s.replace(/[\\;,]/g, (c) => `\\${c}`).replace(/\n/g, '\\n');
const icsTime = (iso: string) => iso.replace(/[-:]/g, '').replace(/\.\d{3}/, '');

function handleIcs(res: ServerResponse, user: User, id: number) {
  const e = getEvent(user.id, id);
  if (!e) throw new HttpError(404, 'event not found');
  const lines = [
    'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Nota//ZH', 'BEGIN:VEVENT',
    `UID:nota-${e.id}@nota`, `DTSTAMP:${icsTime(e.created_at)}`,
    `DTSTART:${icsTime(e.start_at)}`, `DTEND:${icsTime(e.end_at)}`,
    `SUMMARY:${icsText(e.title)}`, ...(e.location ? [`LOCATION:${icsText(e.location)}`] : []),
    'END:VEVENT', 'END:VCALENDAR',
  ];
  res.writeHead(200, { 'content-type': 'text/calendar; charset=utf-8', 'content-disposition': `attachment; filename="nota-${e.id}.ics"` });
  res.end(lines.join('\r\n') + '\r\n');
}

async function route(req: IncomingMessage, res: ServerResponse) {
  const url = new URL(req.url ?? '/', 'http://localhost');
  if (req.method === 'GET' && url.pathname === '/') {
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    return res.end(INDEX_HTML);
  }
  if (!url.pathname.startsWith('/api/')) throw new HttpError(404, 'not found');
  const user = currentUser(req, url);
  if (req.method === 'GET' && url.pathname === '/api/state') return sendJson(res, 200, userState(user));
  if (req.method === 'POST' && url.pathname === '/api/runs') return handleRun(req, res, user);
  const ics = url.pathname.match(/^\/api\/events\/(\d+)\.ics$/);
  if (req.method === 'GET' && ics) return handleIcs(res, user, Number(ics[1]));
  throw new HttpError(404, 'not found');
}

const server = createServer((req, res) =>
  route(req, res).catch((e) => {
    const status = e instanceof HttpError ? e.status : e instanceof SyntaxError ? 400 : 500;
    if (status === 500) console.error(e);
    if (!res.headersSent) sendJson(res, status, { error: e.message });
    else res.end();
  }),
);
server.listen(Number(process.env.PORT ?? 3000), () => {
  console.log(`Nota listening on http://localhost:${(server.address() as AddressInfo).port}`);
});
