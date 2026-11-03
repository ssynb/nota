"""OpenAI-compatible chat completions over plain HTTP (DeepSeek by default)."""
import json
import re
import time

import httpx

from nota import config


class LLMError(Exception):
    pass


def _body(messages, tools, json_mode) -> dict:
    body: dict = {"model": config.LLM_MODEL, "messages": messages}
    if tools:
        body["tools"] = tools
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if config.LLM_THINKING:
        body["thinking"] = {"type": config.LLM_THINKING}
    if config.LLM_REASONING_EFFORT:
        body["reasoning_effort"] = config.LLM_REASONING_EFFORT
    return body


def chat(messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False, deadline: float | None = None) -> dict:
    """One call, finished within LLM_CALL_TIMEOUT_SECONDS and before `deadline` (time.monotonic()),
    whichever comes first. The body is read as a stream so the clock is checked between keep-alive lines."""
    limit = time.monotonic() + config.LLM_CALL_TIMEOUT_SECONDS
    end = limit if deadline is None else min(limit, deadline)
    timeout_error = LLMError(f"模型调用超时：{end - time.monotonic():.0f} 秒内没有返回结果")
    remaining = end - time.monotonic()
    if remaining <= 0:
        raise timeout_error
    try:
        with httpx.stream(
            "POST",
            f"{config.LLM_BASE_URL}/chat/completions",
            json=_body(messages, tools, json_mode),
            headers={"authorization": f"Bearer {config.LLM_API_KEY}"},
            timeout=httpx.Timeout(remaining),
        ) as r:
            raw = bytearray()
            for part in r.iter_bytes():
                raw += part
                if time.monotonic() > end:
                    raise timeout_error
    except httpx.TimeoutException as e:
        raise timeout_error from e
    except httpx.HTTPError as e:
        raise LLMError(f"模型调用失败：{e}") from e
    text = raw.decode("utf-8", "replace")
    if r.status_code != 200:
        raise LLMError(f"模型调用失败 {r.status_code}: {text.strip()[:300]}")
    try:
        message = json.loads(text)["choices"][0]["message"]
    except (json.JSONDecodeError, TypeError, KeyError, IndexError) as e:
        raise LLMError(f"模型返回了无效响应：{text.strip()[:200]}") from e
    if not isinstance(message, dict):
        raise LLMError(f"模型返回了无效响应：{text.strip()[:200]}")
    return message


def json_reply(messages: list[dict]) -> dict:
    """A JSON object answer; a reply that is not the agreed JSON is a failed call, not an empty result."""
    content = chat(messages, json_mode=True).get("content") or ""
    match = re.search(r"\{.*\}", content, re.DOTALL)
    try:
        return json.loads(match.group(0) if match else content)
    except json.JSONDecodeError as e:
        raise LLMError(f"模型没有返回约定的 JSON：{content[:200]}") from e
