# -*- coding: utf-8 -*-
"""图片理解封装（qwen3.8-omni-flash，OpenAI 兼容 chat 接口）：供画师Agent 自检。

按官方示例：业务空间专属 host 的 /compatible-mode/v1，chat.completions.create，
消息 content 用 OpenAI 多模态格式：
  [{"type":"image_url","image_url":{"url": data URI}},
   {"type":"text","text": 检查要求}]
本地图片以 Base64 data URI 传入，并设置 modalities=["text"]（仅文本输出）。
"""
import asyncio
import base64
import logging
from pathlib import Path

from openai import AsyncOpenAI

import config
from tools import tracer
from tools.llm import extract_json

logger = logging.getLogger("vision")


def _client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=config.DASHSCOPE_API_KEY,
        base_url=config.OMNI_COMPAT_BASE,
        timeout=120.0,
        max_retries=0,
    )


async def inspect_image(image_path: str, prompt: str,
                        model: str | None = None, max_retries: int = 2) -> str:
    """送入视觉模型，返回文本结论。"""
    suffix = Path(image_path).suffix.lower().lstrip(".")
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp", "bmp": "bmp"}.get(suffix, "png")
    with open(image_path, "rb") as f:
        data_uri = f"data:image/{mime};base64,{base64.b64encode(f.read()).decode('utf-8')}"

    messages = [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": data_uri}},
            {"type": "text", "text": prompt},
        ],
    }]

    last_err = None
    async with tracer.span("画师", "视觉自检", model or config.QWEN_VL_MODEL):
        for attempt in range(max_retries + 1):
            try:
                client = _client()
                resp = await client.chat.completions.create(
                    model=model or config.QWEN_VL_MODEL,
                    messages=messages,
                    modalities=["text"],
                    temperature=0.2,
                )
                text = resp.choices[0].message.content
                if not text:
                    raise RuntimeError("视觉模型返回空文本")
                return text
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("图片理解失败(第%s/%s次): %s", attempt + 1, max_retries + 1, e)
                if attempt < max_retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"图片理解重试 {max_retries} 次仍失败: {last_err}")


def parse_check_result(text: str) -> tuple[bool, str]:
    """解析自检 JSON：{"pass": bool, "issues": str}；解析失败按不合格处理。"""
    try:
        obj = extract_json(text)
        return bool(obj.get("pass")), str(obj.get("issues", ""))
    except Exception:
        return False, "自检结果解析失败（模型未按 JSON 格式返回），按不合格处理"
