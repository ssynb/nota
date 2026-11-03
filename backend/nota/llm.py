"""OpenAI-compatible chat completions over plain HTTP (DeepSeek by default)."""
import json
import re

import httpx

from nota import config


class LLMError(Exception):
    pass


def chat(messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False) -> dict:
    body: dict = {"model": config.LLM_MODEL, "messages": messages}
    if tools:
        body["tools"] = tools
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    try:
        r = httpx.post(
            f"{config.LLM_BASE_URL}/chat/completions",
            json=body,
            headers={"authorization": f"Bearer {config.LLM_API_KEY}"},
            timeout=config.LLM_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        raise LLMError(f"模型调用失败：{e}") from e
    if r.status_code != 200:
        raise LLMError(f"模型调用失败 {r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]


def json_reply(messages: list[dict]) -> dict:
    """A JSON object answer; a reply that is not the agreed JSON is a failed call, not an empty result."""
    content = chat(messages, json_mode=True).get("content") or ""
    match = re.search(r"\{.*\}", content, re.DOTALL)
    try:
        return json.loads(match.group(0) if match else content)
    except json.JSONDecodeError as e:
        raise LLMError(f"模型没有返回约定的 JSON：{content[:200]}") from e
