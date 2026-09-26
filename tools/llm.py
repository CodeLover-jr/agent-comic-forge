# -*- coding: utf-8 -*-
"""通义千问 Qwen 封装（百炼 OpenAI 兼容协议）。

提供：
- chat()：带重试的对话补全，支持 Function Calling（tools 参数）；
- extract_json()：从模型输出中稳健提取 JSON。
"""
import asyncio
import json
import logging
import re

from openai import AsyncOpenAI

import config
from tools import tracer

logger = logging.getLogger("llm")

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=config.DASHSCOPE_API_KEY,
            base_url=config.DASHSCOPE_COMPAT_BASE,
            timeout=120.0,
            max_retries=0,
        )
    return _client


async def chat(messages, tools=None, model=None, temperature=0.7, max_retries=2):
    """带重试的对话补全，重试 max_retries 次仍失败则抛异常。"""
    client = get_client()
    model = model or config.QWEN_MODEL
    kwargs = {"model": model, "messages": messages, "temperature": temperature}
    if tools:
        kwargs["tools"] = tools

    last_err = None
    async with tracer.span("LLM", "对话补全(含Function Calling)", model):
        for attempt in range(max_retries + 1):
            try:
                resp = await client.chat.completions.create(**kwargs)
                return resp.choices[0].message
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("LLM 调用失败(第%s/%s次): %s", attempt + 1, max_retries + 1, e)
                if attempt < max_retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"LLM 调用重试 {max_retries} 次仍失败: {last_err}")


def extract_json(text: str):
    """从模型输出中稳健提取 JSON 对象或数组。"""
    if not text or not text.strip():
        raise ValueError("模型输出为空，无法解析 JSON")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == open_ch:
                depth += 1
            elif text[i] == close_ch:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
    raise ValueError(f"无法从模型输出中解析 JSON: {text[:200]}")
