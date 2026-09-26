# -*- coding: utf-8 -*-
"""图生视频封装（百炼异步任务）：上传首帧(→oss临时URL) → 提交 → 轮询 → 下载。

注意：经实测，首帧直接传 Base64 data URI 会被计费网关拦截（报 Arrearage），
必须先把关键帧上传到百炼临时存储空间拿 oss:// URL，并在提交头加
X-DashScope-OssResourceResolve: enable。
同一路径 video-synthesis 支持多家模型，参数差异通过 _MODEL_PROFILES 区分：
- 万相 wan3.0：显式 ratio、audio=false、prompt_extend=true，时长 2~30s
- MiniMax-H3：图生视频比例由图片决定(恒 adaptive)，时长 4~15s，分辨率 768P/2K
失败自动重试 max_retries 次，仍失败抛异常，由画师Agent 决定跳过/降级。
"""
import asyncio
import logging

import config
from tools import dash_tasks, tracer, upload as upload_tool

logger = logging.getLogger("i2v")

SUBMIT_PATH = "/services/aigc/video-generation/video-synthesis"


def _profile(model: str) -> dict:
    if model.lower().startswith("minimax"):
        return {
            "resolutions": {"9:16": "768P", "16:9": "768P"},
            "min_dur": 4, "max_dur": 15,
            "ratio": None,           # 图生视频比例由首帧图片决定
            "extra": {"watermark": False},
        }
    # 万相 wan3.0 / wan2.x
    return {
        "resolutions": {"9:16": "720P", "16:9": "1080P"},
        "min_dur": 2, "max_dur": 30,
        "ratio": "explicit",        # 显式传 ratio
        "extra": {"audio": False, "prompt_extend": True},
    }


async def generate_clip(image_path: str, prompt: str, ratio: str, duration: float,
                        out_path, max_retries: int = 2, gate=None) -> str:
    """完整走一遍 上传首帧 → 提交 → 轮询 → 下载，返回本地视频路径。

    gate：可选 asyncio.Event（暂停门控），轮询期间任务暂停时在此等待。
    """
    model = config.VIDEO_MODEL
    prof = _profile(model)
    duration_i = max(prof["min_dur"], min(prof["max_dur"], int(round(duration))))
    parameters = {"resolution": prof["resolutions"].get(ratio, "768P"), "duration": duration_i}
    if prof["ratio"] == "explicit":
        parameters["ratio"] = ratio if ratio in ("9:16", "16:9") else "adaptive"
    parameters.update(prof["extra"])

    last_err = None
    async with tracer.span("画师", "图生视频(上传→提交→轮询→下载)", model):
        for attempt in range(max_retries + 1):
            try:
                # 1) 上传首帧拿 oss:// 临时URL（不能用 Base64 data URI）
                first_url = await upload_tool.upload_for_inference(image_path, model)
                # 2) 提交异步任务（oss:// 需带解析头）
                payload = {
                    "model": model,
                    "input": {
                        "prompt": prompt,
                        "media": [{"type": "first_frame", "url": first_url}],
                    },
                    "parameters": parameters,
                }
                output = await dash_tasks.submit_and_wait(
                    SUBMIT_PATH, payload,
                    extra_headers={"X-DashScope-OssResourceResolve": "enable"},
                    pause_gate=gate)
                video_url = output.get("video_url")
                if not video_url:
                    raise RuntimeError(f"图生视频未返回 video_url: {output}")
                # 3) 下载成片
                await dash_tasks.download(video_url, out_path)
                logger.info("视频片段已生成: %s", out_path)
                return str(out_path)
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("图生视频失败(第%s/%s次): %s", attempt + 1, max_retries + 1, e)
                if attempt < max_retries:
                    await asyncio.sleep(3.0 * (attempt + 1))
    raise RuntimeError(f"图生视频重试 {max_retries} 次仍失败: {last_err}")
