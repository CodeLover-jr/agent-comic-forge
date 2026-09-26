# -*- coding: utf-8 -*-
"""百炼异步任务通用器：提交任务(拿 task_id) → 轮询状态 → 返回 output。

万相文生图 / 图生视频都是这套模型：
- 提交时请求头带 X-DashScope-Async: enable；
- 轮询 GET /tasks/{task_id}，task_status: PENDING/RUNNING/SUCCEEDED/FAILED/CANCELED/UNKNOWN。
"""
import asyncio
import logging

import httpx

import config

logger = logging.getLogger("dash_tasks")


def _headers(async_mode: bool = False) -> dict:
    h = {
        "Authorization": f"Bearer {config.DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }
    if async_mode:
        h["X-DashScope-Async"] = "enable"
    return h


async def submit_and_wait(submit_path: str, payload: dict,
                          timeout: int | None = None,
                          interval: int | None = None,
                          extra_headers: dict | None = None,
                          pause_gate=None) -> dict:
    """提交异步任务并轮询到结束，返回 output 对象（dict）。失败抛异常。

    extra_headers：透传额外请求头（如使用 oss:// 临时URL 时需
    X-DashScope-OssResourceResolve: enable）。
    pause_gate：可选 asyncio.Event（set=可通行），任务暂停时在轮询间隙挂起。
    """
    timeout = timeout or config.TASK_POLL_TIMEOUT
    interval = interval or config.TASK_POLL_INTERVAL
    if not config.DASHSCOPE_API_KEY:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY（请检查 .env）")

    submit_url = f"{config.DASHSCOPE_API_BASE}{submit_path}"
    poll_base = f"{config.DASHSCOPE_API_BASE}/tasks"
    headers = _headers(async_mode=True)
    if extra_headers:
        headers.update(extra_headers)

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(submit_url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"提交失败 status={resp.status_code}, body={resp.text[:500]}")
        task_id = resp.json().get("output", {}).get("task_id")
        if not task_id:
            raise RuntimeError(f"异步任务提交未返回 task_id: {resp.text[:300]}")

        elapsed = 0
        consecutive_errors = 0
        while elapsed < timeout:
            # FIXED: 轮询可能持续数分钟，单次网络抖动/5xx 不应直接判任务失败；
            # 连续 3 次轮询都失败才真正抛出
            try:
                r = await client.get(f"{poll_base}/{task_id}", headers=_headers())
                r.raise_for_status()
                consecutive_errors = 0
            except Exception as poll_err:  # noqa: BLE001
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    raise RuntimeError(f"轮询连续 {consecutive_errors} 次失败: {poll_err}")
                await asyncio.sleep(interval)
                elapsed += interval
                continue
            body = r.json()
            output = body.get("output", {})
            status = output.get("task_status")
            if status == "SUCCEEDED":
                return output
            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                raise RuntimeError(f"异步任务异常: status={status}, detail={body}")
            await asyncio.sleep(interval)
            elapsed += interval
            # 协作式暂停：暂停时在轮询间隙挂起，继续后再查任务状态
            if pause_gate is not None:
                await pause_gate.wait()
    raise TimeoutError(f"异步任务轮询超时（>{timeout}s），task_id={task_id}")


async def download(url: str, out_path) -> str:
    """下载产物 URL 到本地。"""
    from pathlib import Path
    out_path = Path(out_path)
    async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        out_path.write_bytes(r.content)
    return str(out_path)
