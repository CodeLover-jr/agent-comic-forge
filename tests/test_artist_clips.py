# -*- coding: utf-8 -*-
"""画师并发图生视频（artist._generate_clips）单元测试。

覆盖：
1. 所有片段真正并发执行、结果按镜头顺序返回；
2. 关键帧缺失 → 黑屏占位，不调用图生视频；
3. 图生视频失败 → 关键帧静态片段兜底；
4. 信号量把同时在生成的片段数限制为 MAX_CONCURRENT_CLIPS。

运行（项目根目录）：
    .venv\\Scripts\\python.exe -m pytest tests/test_artist_clips.py -v
"""
import asyncio

import pytest

from agents import artist
import config


# ---------------------------------------------------------------- 辅助桩
class FakeCtx:
    def __init__(self, tmp_path, n_shots, keyframes):
        self.script = [
            {"shot_no": i + 1, "shot_type": "中景",
             "description": f"镜头{i + 1}", "duration": 4}
            for i in range(n_shots)
        ]
        self.keyframes = keyframes
        self.out_dir = tmp_path
        self.user_input = {"aspect": "9:16"}
        self._gate = asyncio.Event()
        self._gate.set()
        self.clips = []
        self.progress_events = []

    async def checkpoint(self, agent="", where=""):
        await self._gate.wait()

    def log(self, agent, message):
        pass

    def set_progress(self, value):
        self.progress_events.append(value)


def _touch(path):
    from pathlib import Path
    path = Path(path)
    path.write_bytes(b"fake-media")


# ---------------------------------------------------------------- 用例
@pytest.mark.asyncio
async def test_clips_run_concurrently_in_order(tmp_path, monkeypatch):
    """4 个片段并发执行：总耗时远小于串行之和，结果按镜头顺序排列。"""
    keyframes = [str(tmp_path / f"k{i}.png") for i in range(1, 5)]
    ctx = FakeCtx(tmp_path, 4, keyframes)

    async def fake_generate_clip(image_path, prompt, ratio, duration, out_path, gate=None):
        await asyncio.sleep(0.2)
        _touch(out_path)
        return str(out_path)

    async def fail_static(*a, **k):
        raise AssertionError("本用例不应走占位")

    monkeypatch.setattr(artist.i2v, "generate_clip", fake_generate_clip)
    monkeypatch.setattr(artist.ff, "static_clip", fail_static)

    t0 = asyncio.get_event_loop().time()
    await artist._generate_clips(ctx, 720, 1280)
    elapsed = asyncio.get_event_loop().time() - t0

    assert len(ctx.clips) == 4
    assert ctx.clips == [str(tmp_path / f"clip_{i:02d}.mp4") for i in range(1, 5)]
    assert elapsed < 0.5, f"串行才会超过 0.5s，实际 {elapsed:.2f}s，未并发"


@pytest.mark.asyncio
async def test_missing_keyframe_black_placeholder(tmp_path, monkeypatch):
    """关键帧缺失：黑屏占位（static_clip 首参 None），且不调用图生视频。"""
    keyframes = [str(tmp_path / "k1.png"), None, str(tmp_path / "k3.png")]
    ctx = FakeCtx(tmp_path, 3, keyframes)

    i2v_calls = {"n": 0}

    async def fake_generate_clip(*a, **k):
        i2v_calls["n"] += 1

    static_calls = []

    async def fake_static_clip(image, out, w, h, dur):
        static_calls.append(image)
        _touch(out)

    monkeypatch.setattr(artist.i2v, "generate_clip", fake_generate_clip)
    monkeypatch.setattr(artist.ff, "static_clip", fake_static_clip)

    await artist._generate_clips(ctx, 720, 1280)

    assert i2v_calls["n"] == 2, "缺失镜头不应调用图生视频"
    assert static_calls == [None], "缺失镜头必须黑屏占位"
    assert len(ctx.clips) == 3


@pytest.mark.asyncio
async def test_i2v_failure_uses_keyframe_fallback(tmp_path, monkeypatch):
    """图生视频抛错：用该镜头关键帧生成静态片段兜底，片段仍在结果中。"""
    keyframes = [str(tmp_path / "k1.png"), str(tmp_path / "k2.png")]
    ctx = FakeCtx(tmp_path, 2, keyframes)

    async def fake_generate_clip(image_path, *a, **k):
        if image_path.endswith("k2.png"):
            raise RuntimeError("模拟云端失败")
        _touch(a[3])  # out_path 是第 5 个位置参数 → a = (prompt, ratio, duration, out_path)

    static_calls = []

    async def fake_static_clip(image, out, w, h, dur):
        static_calls.append(image)
        _touch(out)

    monkeypatch.setattr(artist.i2v, "generate_clip", fake_generate_clip)
    monkeypatch.setattr(artist.ff, "static_clip", fake_static_clip)

    await artist._generate_clips(ctx, 720, 1280)

    assert static_calls == [str(tmp_path / "k2.png")], "失败镜头应用其关键帧兜底"
    assert len(ctx.clips) == 2


@pytest.mark.asyncio
async def test_semaphore_caps_inflight(tmp_path, monkeypatch):
    """信号量：MAX_CONCURRENT_CLIPS=2 时，同时在生成的片段数峰值必须为 2。"""
    monkeypatch.setattr(config, "MAX_CONCURRENT_CLIPS", 2)
    n = 6
    keyframes = [str(tmp_path / f"k{i}.png") for i in range(1, n + 1)]
    ctx = FakeCtx(tmp_path, n, keyframes)

    inflight = {"now": 0, "max": 0}

    async def fake_generate_clip(image_path, prompt, ratio, duration, out_path, gate=None):
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        await asyncio.sleep(0.15)
        inflight["now"] -= 1
        _touch(out_path)
        return str(out_path)

    monkeypatch.setattr(artist.i2v, "generate_clip", fake_generate_clip)

    await artist._generate_clips(ctx, 720, 1280)

    assert inflight["max"] == 2, f"并发峰值应为 2，实际 {inflight['max']}"
    assert len(ctx.clips) == n
