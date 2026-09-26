# -*- coding: utf-8 -*-
"""千问图像文生图封装（百炼 DashScope multimodal-generation 端点，同步）。

严格按官方 DashScope 示例：
  POST /services/aigc/multimodal-generation/generation
  body: model / input.messages[{role,content:[{text}]}] / parameters
  返回: output.choices[0].message.content[0].image（PNG URL，24h，立即下载）
失败自动重试 max_retries 次，仍失败抛异常，由画师Agent 决定跳过/降级。
"""
import asyncio
import logging
from pathlib import Path

import httpx

import config
from tools import tracer

logger = logging.getLogger("t2i")

_PATH = "/services/aigc/multimodal-generation/generation"


async def generate_image(prompt: str, out_path, width: int = 720, height: int = 1280,
                         model: str | None = None, max_retries: int = 2) -> str:
    """生成一张图并保存到 out_path，返回绝对路径。"""
    if not config.DASHSCOPE_API_KEY:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY（请检查 .env）")
    url = f"{config.DASHSCOPE_API_BASE}{_PATH}"
    payload = {
        "model": model or config.IMAGE_MODEL,
        "input": {
            "messages": [
                {"role": "user", "content": [{"text": prompt}]}
            ]
        },
        "parameters": {
            "prompt_extend": True,
            "size": f"{width}*{height}",
            "n": 1,
        },
    }
    headers = {
        "Authorization": f"Bearer {config.DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }

    last_err = None
    async with tracer.span("画师", "文生图", model or config.IMAGE_MODEL):
        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code >= 400:
                        raise RuntimeError(f"文生图提交失败 status={resp.status_code}, body={resp.text[:400]}")
                    choices = resp.json().get("output", {}).get("choices") or []
                    content = (choices[0].get("message", {}).get("content") if choices else None) or []
                    img_url = next((c.get("image") for c in content if c.get("image")), None)
                    if not img_url:
                        raise RuntimeError(f"文生图未返回 image: {resp.text[:300]}")
                    img = await client.get(img_url)
                    img.raise_for_status()
                    Path(out_path).write_bytes(img.content)
                logger.info("关键帧已生成: %s", out_path)
                return str(out_path)
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("文生图失败(第%s/%s次): %s", attempt + 1, max_retries + 1, e)
                if attempt < max_retries:
                    await asyncio.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"文生图重试 {max_retries} 次仍失败: {last_err}")
