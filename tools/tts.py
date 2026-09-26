# -*- coding: utf-8 -*-
"""Qwen-Audio 非实时语音合成封装（HTTP，非流式，整段返回）。

POST {TTS_BASE}/services/audio/tts/SpeechSynthesizer
  → output.audio.url（mp3，有时效，立即下载）。
注意：
- qwen-audio-3.1-tts-flash 只支持其「专属 v3.1 音色」，音色不匹配报 Engine error 411；
- 该模型在通用域名 dashscope.aliyuncs.com 上不可用（411），必须走「业务空间专属
  host」：https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1（见 config.TTS_BASE）。
失败自动重试 max_retries 次，仍失败抛异常，由剪辑Agent 用静音占位。
"""
import asyncio
import logging
from pathlib import Path

import httpx

import config
from tools import tracer

logger = logging.getLogger("tts")

_PATH = "/services/audio/tts/SpeechSynthesizer"

VOICE_MAP = {
    "male": config.TTS_VOICE_MALE,
    "female": config.TTS_VOICE_FEMALE,
    "narrator": config.TTS_VOICE_NARRATOR,
}


async def synthesize(text: str, out_path, voice: str = "narrator", max_retries: int = 2) -> str:
    """合成一句台词为 mp3，返回本地路径。"""
    voice_id = VOICE_MAP.get(voice, config.TTS_VOICE_NARRATOR)
    url = f"{config.TTS_BASE}{_PATH}"
    payload = {
        "model": config.TTS_MODEL,
        "input": {
            "text": text[:300],
            "voice": voice_id,
            "format": "mp3",
            "sample_rate": 24000,
        },
    }
    headers = {
        "Authorization": f"Bearer {config.DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }

    last_err = None
    async with tracer.span("剪辑", "语音合成", config.TTS_MODEL):
        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code >= 400:
                        raise RuntimeError(f"TTS 提交失败 status={resp.status_code}, body={resp.text[:300]}")
                    audio_url = resp.json().get("output", {}).get("audio", {}).get("url")
                    if not audio_url:
                        raise RuntimeError(f"TTS 未返回音频 URL: {resp.text[:300]}")
                    r = await client.get(audio_url)
                    r.raise_for_status()
                    Path(out_path).write_bytes(r.content)
                logger.info("配音已生成(%s): %s", voice_id, out_path)
                return str(out_path)
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("语音合成失败(第%s/%s次): %s", attempt + 1, max_retries + 1, e)
                if attempt < max_retries:
                    await asyncio.sleep(3.0 + 5.0 * attempt)  # 3s,8s：引擎411多为间歇性故障
    raise RuntimeError(f"语音合成重试 {max_retries} 次仍失败: {last_err}")
