# -*- coding: utf-8 -*-
"""百炼临时存储空间上传：本地文件 → oss:// 临时URL（48 小时有效）。

流程：GET /api/v1/uploads?action=getPolicy&model=xxx 拿上传凭证 →
      以 OSS 表单（multipart/form-data）POST 到 upload_host → 拼 oss:// key。
说明：
- 上传凭证接口有限流，失败由调用方重试；
- 拿到的 oss:// URL 在后续模型调用时，请求头必须带
  X-DashScope-OssResourceResolve: enable。
"""
import asyncio
import logging
from pathlib import Path

import httpx

import config

logger = logging.getLogger("upload")

_POLICY_URL = f"{config.DASHSCOPE_API_BASE}/uploads"


async def upload_for_inference(file_path: str, model_name: str,
                               max_retries: int = 2) -> str:
    """上传本地文件，返回 oss:// 临时URL。"""
    if not config.DASHSCOPE_API_KEY:
        raise RuntimeError("未配置 DASHSCOPE_API_KEY（请检查 .env）")
    # FIXED: 文件不存在是确定性错误，必须立即抛出、绝不重试；
    # 之前交给重试循环会把一次缺失放大成多次无效请求（请求风暴）
    src = Path(file_path)
    if not src.is_file():
        raise FileNotFoundError(f"待上传文件不存在: {file_path}")
    headers = {"Authorization": f"Bearer {config.DASHSCOPE_API_KEY}"}
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                pol = await client.get(_POLICY_URL, headers=headers,
                                       params={"action": "getPolicy", "model": model_name})
                pol.raise_for_status()
                data = pol.json()["data"]

                key = f"{data['upload_dir']}/{src.name}"
                with open(src, "rb") as f:
                    files = {
                        "OSSAccessKeyId": (None, data["oss_access_key_id"]),
                        "Signature": (None, data["signature"]),
                        "policy": (None, data["policy"]),
                        "x-oss-object-acl": (None, data["x_oss_object_acl"]),
                        "x-oss-forbid-overwrite": (None, data["x_oss_forbid_overwrite"]),
                        "key": (None, key),
                        "success_action_status": (None, "200"),
                        "file": (src.name, f),
                    }
                    up = await client.post(data["upload_host"], files=files)
                up.raise_for_status()
                oss_url = f"oss://{key}"
                logger.info("已上传临时文件: %s -> %s", src.name, oss_url)
                return oss_url
        except httpx.HTTPError as e:  # FIXED: 只有网络/HTTP 错误才重试；
            # AttributeError/KeyError 等代码缺陷立即抛出，避免确定性错误被放大成请求风暴
            last_err = e
            logger.warning("上传临时文件失败(第%s/%s次): %s",
                           attempt + 1, max_retries + 1, e)
            if attempt < max_retries:
                await asyncio.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"上传临时文件重试 {max_retries} 次仍失败: {last_err}")
