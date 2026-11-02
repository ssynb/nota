// Local OpenAI-compatible model double, reached through LLM_BASE_URL.
// Behaviour is driven by markers in the user's text:
//   #full     -> one turn calling create_todo + set_reminder + create_calendar_event
//   #ask <q>  -> search_history(q), then answers with the raw tool result
//   #fail     -> create_todo, then HTTP 500 on the follow-up turn
//   otherwise -> create_todo(text), then a summary
// Like DeepSeek thinking mode, it rejects follow-ups that drop reasoning_content.
import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';

type Msg = { role: string; content?: string; tool_calls?: unknown[]; reasoning_content?: string };

const FULL_CALLS = [
  ['create_todo', { title: '准备预算表' }],
  ['set_reminder', { content: '交预算', remind_at: '2026-11-04T15:00:00+08:00' }],
  ['create_calendar_event', { title: '预算评审会', start: '2026-11-04T15:00:00+08:00', end: '2026-11-04T16:00:00+08:00' }],
] as const;

function toolTurn(calls: ReadonlyArray<readonly [string, object]>) {
  return {
    role: 'assistant',
    content: null,
    reasoning_content: 'thinking',
    tool_calls: calls.map(([name, args], i) => ({
      id: `call_${i}`,
      type: 'function',
      function: { name, arguments: JSON.stringify(args) },
    })),
  };
}

function reply(messages: Msg[]): { status: number; message?: object } {
  const text = messages.find((m) => m.role === 'user')?.content ?? '';
  const toolResults = messages.filter((m) => m.role === 'tool');
  if (toolResults.length === 0) {
    if (text.includes('#full')) return { status: 200, message: toolTurn(FULL_CALLS) };
    if (text.startsWith('#ask ')) return { status: 200, message: toolTurn([['search_history', { query: text.slice(5) }]]) };
    return { status: 200, message: toolTurn([['create_todo', { title: text }]]) };
  }
  if (text.includes('#fail')) return { status: 500 };
  const content = text.startsWith('#ask ') ? toolResults[0].content : `摘要：${text}`;
  return { status: 200, message: { role: 'assistant', content } };
}

export async function startFakeLLM(delayMs = 30) {
  const server = createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    const { messages } = JSON.parse(body) as { messages: Msg[] };
    await new Promise((r) => setTimeout(r, delayMs));
    const droppedReasoning = messages.some((m) => m.role === 'assistant' && m.tool_calls && !m.reasoning_content);
    const { status, message } = droppedReasoning ? { status: 400 } : reply(messages);
    res.writeHead(status, { 'content-type': 'application/json' });
    res.end(JSON.stringify(message ? { choices: [{ index: 0, message, finish_reason: 'stop' }] } : { error: { message: 'fake failure' } }));
  });
  await new Promise<void>((r) => server.listen(0, '127.0.0.1', r));
  return { url: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, close: () => server.close() };
}
