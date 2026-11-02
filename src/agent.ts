// Hand-written OpenAI-compatible function-calling loop: the model decides which tools to call.
import * as store from './db.ts';
import { LeaseLostError, type Lease, type Mode, type Step } from './db.ts';

const LLM_BASE_URL = process.env.LLM_BASE_URL ?? 'https://api.deepseek.com';
const LLM_MODEL = process.env.LLM_MODEL ?? 'deepseek-flash';
const LLM_API_KEY = process.env.LLM_API_KEY;
if (!LLM_API_KEY) throw new Error('LLM_API_KEY is required (see .env.example)');

const MAX_TURNS = 8;
const LLM_TIMEOUT_MS = 120_000;
const DEFAULT_EVENT_MS = 60 * 60 * 1000;

export type AgentContext = Lease & { tz: string; mode: Mode; now: Date };
type Args = Record<string, string | undefined>;
type ToolCall = { id: string; function: { name: string; arguments: string } };
type Message = { role: string; content?: string | null; tool_calls?: ToolCall[]; [k: string]: unknown };

const ISO_TIME = { type: 'string', description: '带时区偏移的 ISO 8601 时间，如 2026-11-04T15:00:00+08:00' };
const fn = (name: string, description: string, properties: object, required: string[]) => ({
  type: 'function',
  function: { name, description, parameters: { type: 'object', properties, required } },
});

const TOOLS = [
  fn('create_todo', '新建一条待办事项', { title: { type: 'string', description: '简洁的待办内容' }, due: { ...ISO_TIME, description: `截止时间（可选）。${ISO_TIME.description}` } }, ['title']),
  fn('set_reminder', '在指定时间提醒用户', { content: { type: 'string', description: '提醒内容' }, remind_at: ISO_TIME }, ['content', 'remind_at']),
  fn(
    'create_calendar_event',
    '在用户日历中创建日程（会议、约会、活动等有确定时间的事）',
    { title: { type: 'string' }, start: ISO_TIME, end: { ...ISO_TIME, description: '结束时间（可选，默认 1 小时）' }, location: { type: 'string' } },
    ['title', 'start'],
  ),
  fn('search_history', '检索该用户过往的笔记、待办、提醒和日程', { query: { type: 'string', description: '空格分隔的关键词，中文请拆成短词，如“预算 评审”' } }, ['query']),
];

/** Local wall-clock text, e.g. "2026-11-04 15:00 周三". */
export function localTime(iso: string | Date, tz: string) {
  const d = new Date(iso);
  const weekday = d.toLocaleDateString('zh-CN', { timeZone: tz, weekday: 'short' });
  return `${d.toLocaleString('sv-SE', { timeZone: tz }).slice(0, 16)} ${weekday}`;
}

const utcOffset = (tz: string, at: Date) =>
  new Intl.DateTimeFormat('en-US', { timeZone: tz, timeZoneName: 'longOffset' }).formatToParts(at).find((p) => p.type === 'timeZoneName')!.value.replace('GMT', 'UTC');

/** Accepts only absolute times; a missing offset would silently be read in the server's timezone. */
function parseTime(value: string | undefined, field: string): string {
  if (!value || !/(Z|[+-]\d{2}:\d{2})$/.test(value) || Number.isNaN(Date.parse(value))) {
    throw new Error(`${field} 必须是带时区偏移的 ISO 8601 时间，收到：${value}`);
  }
  return new Date(value).toISOString();
}

function systemPrompt(ctx: AgentContext) {
  const head = `你是 Nota，用户的 AI 语音便签助手。
当前时间：${localTime(ctx.now, ctx.tz)}，用户时区 ${ctx.tz}（${utcOffset(ctx.tz, ctx.now)}）。
用户输入可能来自语音识别，允许有错别字和口语。最终回复用纯文本，不用 Markdown。
工具参数中的时间一律写成带该时区偏移的 ISO 8601。相对时间按上面的当前时间换算：“下周X”指下一个自然周（周一为一周开始）的周X，“周X/这周X”指本周的周X；只说上午/下午/晚上而没有钟点时分别取 09:00/15:00/20:00。`;
  if (ctx.mode === 'ask') {
    return `${head}
任务：回答用户关于自己过往记录的问题。先调用 search_history 检索（关键词用空格分隔的短词，可换词多次检索），只依据检索结果作答；查不到就直说。不要创建待办、提醒或日程。`;
  }
  return `${head}
任务：把用户这段话整理成便签，并替用户把事办了：
- 每件要做的事调用一次 create_todo；
- 用户要求提醒、或某个时间点需要记得的事，调用 set_reminder；
- 有确定时间的会议、约会、活动，调用 create_calendar_event；
- 话里提到过去记录过的事时，可先 search_history。
只做用户话里有的事，不要编造。全部完成后，用一两句中文写出这段话的摘要作为最终回复。`;
}

const TOOL_IMPLS: Record<string, (ctx: AgentContext, a: Args) => object> = {
  create_todo: (ctx, a) => {
    const due = a.due ? parseTime(a.due, 'due') : null;
    return { ok: true, id: store.addTodo(ctx, a.title!, due).id, due: due && localTime(due, ctx.tz) };
  },
  set_reminder: (ctx, a) => {
    const at = parseTime(a.remind_at, 'remind_at');
    return { ok: true, id: store.addReminder(ctx, a.content!, at).id, remind_at: localTime(at, ctx.tz) };
  },
  create_calendar_event: (ctx, a) => {
    const start = parseTime(a.start, 'start');
    const end = a.end ? parseTime(a.end, 'end') : new Date(Date.parse(start) + DEFAULT_EVENT_MS).toISOString();
    const { id } = store.addEvent(ctx, { title: a.title!, start, end, location: a.location ?? null });
    return { ok: true, id, start: localTime(start, ctx.tz), end: localTime(end, ctx.tz) };
  },
  search_history: (ctx, a) => ({ results: store.searchHistory(ctx.userId, a.query ?? '').map((r) => ({ ...r, at: localTime(r.at, ctx.tz) })) }),
};

/** Tool errors go back to the model so it can correct its arguments; only a lost lease fails the run. */
function execute(ctx: AgentContext, call: ToolCall): Step {
  const tool = call.function.name;
  let args: Args = {};
  try {
    args = JSON.parse(call.function.arguments || '{}');
    const impl = TOOL_IMPLS[tool];
    if (!impl) throw new Error(`未知工具 ${tool}`);
    return { tool, args, result: impl(ctx, args) };
  } catch (e) {
    if (e instanceof LeaseLostError) throw e;
    return { tool, args, result: { error: (e as Error).message } };
  }
}

async function chat(messages: Message[]): Promise<Message> {
  const res = await fetch(`${LLM_BASE_URL}/chat/completions`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', authorization: `Bearer ${LLM_API_KEY}` },
    body: JSON.stringify({ model: LLM_MODEL, messages, tools: TOOLS }),
    signal: AbortSignal.timeout(LLM_TIMEOUT_MS),
  });
  if (!res.ok) throw new Error(`模型调用失败 ${res.status}: ${(await res.text()).slice(0, 300)}`);
  return (await res.json()).choices[0].message;
}

export async function runAgent(ctx: AgentContext, text: string, onStep: (s: Step) => void) {
  const messages: Message[] = [
    { role: 'system', content: systemPrompt(ctx) },
    { role: 'user', content: text },
  ];
  const steps: Step[] = [];
  for (let turn = 0; turn < MAX_TURNS; turn++) {
    // The whole assistant message goes back, including DeepSeek's reasoning_content (required with tools).
    const msg = await chat(messages);
    messages.push(msg);
    if (!msg.tool_calls?.length) return { mode: ctx.mode, reply: msg.content ?? '', steps };
    for (const call of msg.tool_calls) {
      const step = execute(ctx, call);
      steps.push(step);
      onStep(step);
      messages.push({ role: 'tool', tool_call_id: call.id, content: JSON.stringify(step.result) });
    }
  }
  throw new Error(`Agent 超过 ${MAX_TURNS} 轮仍未完成`);
}
