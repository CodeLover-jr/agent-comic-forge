# -*- coding: utf-8 -*-
"""upload.py 单元测试 + 真实上传集成测试。

运行（项目根目录）：
    .venv\\Scripts\\python.exe -m pytest tests/test_upload.py -v
"""
from pathlib import Path

import httpx
import pytest

from tools import upload


# ---------------------------------------------------------------- 辅助桩
class _FakeClient:
    """记录调用、按场景返回的假 AsyncClient。"""
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _patch_client(monkeypatch, get_handler, post_handler=None):
    class Client(_FakeClient):
        async def get(self, *a, **k):
            return await get_handler()

        async def post(self, *a, **k):
            return await post_handler() if post_handler else None

    monkeypatch.setattr(upload.httpx, "AsyncClient", Client)


# ---------------------------------------------------------------- 用例
@pytest.mark.asyncio
async def test_missing_file_raises_immediately(tmp_path, monkeypatch):
    """文件不存在：立即 FileNotFoundError，且不发起任何网络请求。"""
    requests = {"n": 0}

    async def get_handler():
        requests["n"] += 1
        return None

    _patch_client(monkeypatch, get_handler)
    missing = tmp_path / "nope.png"
    with pytest.raises(FileNotFoundError):
        await upload.upload_for_inference(str(missing), "wan3.0-video-prime", max_retries=2)
    assert requests["n"] == 0, "缺失文件不应触发任何请求"


@pytest.mark.asyncio
async def test_network_error_retries_three_times(tmp_path, monkeypatch):
    """网络错误：重试 2 次（共 3 次）后抛 RuntimeError。"""
    calls = {"get": 0}

    class Resp:
        def raise_for_status(self):
            raise httpx.ConnectError("boom")

    async def get_handler():
        calls["get"] += 1
        return Resp()

    _patch_client(monkeypatch, get_handler)

    async def _nosleep(*_):
        return None

    monkeypatch.setattr(upload.asyncio, "sleep", _nosleep)
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    with pytest.raises(RuntimeError):
        await upload.upload_for_inference(str(f), "wan3.0-video-prime", max_retries=2)
    assert calls["get"] == 3


@pytest.mark.asyncio
async def test_code_bug_does_not_retry(tmp_path, monkeypatch):
    """非 httpx 错误（如 KeyError 代码缺陷）：立即抛出，不重试。"""
    calls = {"get": 0}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            calls["get"] += 1
            return {"data": {}}  # 缺字段 → data['upload_dir'] KeyError

    async def get_handler():
        return Resp()

    _patch_client(monkeypatch, get_handler)
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    with pytest.raises(KeyError):
        await upload.upload_for_inference(str(f), "wan3.0-video-prime", max_retries=2)
    assert calls["get"] == 1, "代码缺陷不应重试"


@pytest.mark.asyncio
async def test_real_upload_integration():
    """真实上传一个历史关键帧，应返回 oss:// URL。"""
    src = Path(r"E:\dev-workspace\AnimAI\output\effbcb0c332b\keyframe_01.png")
    if not src.exists():
        pytest.skip("无历史关键帧样本")
    url = await upload.upload_for_inference(str(src), "wan3.0-video-prime")
    assert url.startswith("oss://"), url
