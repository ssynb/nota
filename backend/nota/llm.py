"""OpenAI-compatible chat completions over plain HTTP (DeepSeek by default)."""
import json
import re
import threading
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


def _post(body: dict, end: float) -> tuple[int, str]:
    """Reads the whole response, giving up as soon as `end` has passed. Runs in its own thread so the
    caller's wait is bounded even while a read is blocked."""
    with httpx.stream(
        "POST",
        f"{config.LLM_BASE_URL}/chat/completions",
        json=body,
        headers={"authorization": f"Bearer {config.LLM_API_KEY}"},
        timeout=httpx.Timeout(max(0.1, end - time.monotonic())),
    ) as r:
        raw = bytearray()
        for part in r.iter_bytes():  # under load DeepSeek sends keep-alive blank lines before the body
            raw += part
            if time.monotonic() > end:
                raise TimeoutError
        return r.status_code, raw.decode("utf-8", "replace")


def chat(messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False, deadline: float | None = None) -> dict:
    """One call that has fully completed within LLM_CALL_TIMEOUT_SECONDS and before `deadline`
    (time.monotonic()), whichever is earlier; otherwise LLMError. The limit is a hard wall-clock limit:
    the caller stops waiting at it, and a response that only completes after it counts as a timeout."""
    end = time.monotonic() + config.LLM_CALL_TIMEOUT_SECONDS
    if deadline is not None:
        end = min(end, deadline)
    budget = end - time.monotonic()
    timeout_error = LLMError(f"模型调用超时：{max(budget, 0):.0f} 秒内没有返回结果")
    if budget <= 0:
        raise timeout_error
    outcome: dict = {}
    finished = threading.Event()

    def worker():
        try:
            outcome["response"] = _post(_body(messages, tools, json_mode), end)
        except Exception as e:  # noqa: BLE001 — handed to the caller below
            outcome["error"] = e
        finally:
            finished.set()

    threading.Thread(target=worker, daemon=True, name="llm-call").start()
    if not finished.wait(budget) or time.monotonic() > end:
        raise timeout_error
    error = outcome.get("error")
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        raise timeout_error from error
    if isinstance(error, httpx.HTTPError):
        raise LLMError(f"模型调用失败：{error}") from error
    if error is not None:
        raise error
    status, text = outcome["response"]
    if status != 200:
        raise LLMError(f"模型调用失败 {status}: {text.strip()[:300]}")
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
