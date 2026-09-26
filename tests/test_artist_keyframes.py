# -*- coding: utf-8 -*-
"""画师并发关键帧（artist._generate_keyframes）单元测试。

覆盖：
1. 所有关键帧真正并发、结果按镜头顺序返回；
2. 生图失败 → 该镜头记 None；
3. 视觉自检不通过 → 重绘最多 3 次，最终保留最后一帧；
4. 信号量把同时在生成的镜头数限制为 MAX_CONCURRENT_KEYFRAMES。

运行（项目根目录）：
    .venv\\Scripts\\python.exe -m pytest tests/test_artist_keyframes.py -v
"""
import asyncio
from pathlib import Path

import pytest

from agents import artist
import config


# ---------------------------------------------------------------- 辅助桩
class FakeCtx:
    def __init__(self, tmp_path, n_shots):
        self.script = [
            {"shot_no": i + 1, "shot_type": "medium",
             "description": f"shot {i + 1}", "duration": 4}
            for i in range(n_shots)
        ]
        self.user_input = {
            "characters": [],
            "scenes": [{"name": "room", "atmosphere": "calm"}],
            "style": "国漫",
            "aspect": "9:16",
        }
        self.out_dir = tmp_path
        self._gate = asyncio.Event()
        self._gate.set()

    async def checkpoint(self, agent="", where=""):
        await self._gate.wait()

    def log(self, agent, message):
        pass

    def set_progress(self, value):
        pass


def _pass_check():
    async def inspect(image, prompt):
        return "{}"

    def parse(text):
        return True, "ok"

    return inspect, parse


# ---------------------------------------------------------------- 用例
@pytest.mark.asyncio
async def test_keyframes_concurrent_in_order(tmp_path, monkeypatch):
    """4 张关键帧并发：总耗时远小于串行之和，结果按镜头顺序排列。"""
    ctx = FakeCtx(tmp_path, 4)

    async def fake_generate(prompt, out_path, width=None, height=None):
        await asyncio.sleep(0.2)
        Path(out_path).write_bytes(b"img")
        return str(out_path)

    inspect, parse = _pass_check()
    monkeypatch.setattr(artist.t2i, "generate_image", fake_generate)
    monkeypatch.setattr(artist.vision, "inspect_image", inspect)
    monkeypatch.setattr(artist.vision, "parse_check_result", parse)

    t0 = asyncio.get_event_loop().time()
    results = await artist._generate_keyframes(ctx, "prefix", "国漫", 720, 1280)
    elapsed = asyncio.get_event_loop().time() - t0

    assert results == [str(tmp_path / f"keyframe_{i:02d}.png") for i in range(1, 5)]
    assert elapsed < 0.5, f"串行才会超过 0.5s，实际 {elapsed:.2f}s，未并发"


@pytest.mark.asyncio
async def test_t2i_failure_returns_none(tmp_path, monkeypatch):
    """第 2 镜生图抛错：该位置记 None，其余镜头正常。"""
    ctx = FakeCtx(tmp_path, 3)

    async def fake_generate(prompt, out_path, width=None, height=None):
        if str(out_path).endswith("keyframe_02.png"):
            raise RuntimeError("simulated image failure")
        Path(out_path).write_bytes(b"img")
        return str(out_path)

    inspect_calls = {"n": 0}

    async def inspect(image, prompt):
        inspect_calls["n"] += 1
        return "{}"

    def parse(text):
        return True, "ok"

    monkeypatch.setattr(artist.t2i, "generate_image", fake_generate)
    monkeypatch.setattr(artist.vision, "inspect_image", inspect)
    monkeypatch.setattr(artist.vision, "parse_check_result", parse)

    results = await artist._generate_keyframes(ctx, "prefix", "国漫", 720, 1280)

    assert results[0].endswith("keyframe_01.png")
    assert results[1] is None
    assert results[2].endswith("keyframe_03.png")
    assert inspect_calls["n"] == 2, "生图失败的镜头不应再做视觉自检"


@pytest.mark.asyncio
async def test_selfcheck_fail_redraws_three_times(tmp_path, monkeypatch):
    """自检始终不通过：生图恰好 3 次，最终保留最后一帧（结果非 None）。"""
    ctx = FakeCtx(tmp_path, 1)
    gen_calls = {"n": 0}

    async def fake_generate(prompt, out_path, width=None, height=None):
        gen_calls["n"] += 1
        Path(out_path).write_bytes(b"img")
        return str(out_path)

    async def inspect(image, prompt):
        return "{}"

    def parse(text):
        return False, "face mismatch"

    monkeypatch.setattr(artist.t2i, "generate_image", fake_generate)
    monkeypatch.setattr(artist.vision, "inspect_image", inspect)
    monkeypatch.setattr(artist.vision, "parse_check_result", parse)

    results = await artist._generate_keyframes(ctx, "prefix", "国漫", 720, 1280)

    assert gen_calls["n"] == 3, f"应重绘到 3 次，实际 {gen_calls['n']}"
    assert results == [str(tmp_path / "keyframe_01.png")]


@pytest.mark.asyncio
async def test_semaphore_caps_inflight(tmp_path, monkeypatch):
    """信号量：MAX_CONCURRENT_KEYFRAMES=2 时，同时在生成的镜头峰值必须为 2。"""
    monkeypatch.setattr(config, "MAX_CONCURRENT_KEYFRAMES", 2)
    ctx = FakeCtx(tmp_path, 6)
    inflight = {"now": 0, "max": 0}

    async def fake_generate(prompt, out_path, width=None, height=None):
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        await asyncio.sleep(0.15)
        inflight["now"] -= 1
        Path(out_path).write_bytes(b"img")
        return str(out_path)

    inspect, parse = _pass_check()
    monkeypatch.setattr(artist.t2i, "generate_image", fake_generate)
    monkeypatch.setattr(artist.vision, "inspect_image", inspect)
    monkeypatch.setattr(artist.vision, "parse_check_result", parse)

    results = await artist._generate_keyframes(ctx, "prefix", "国漫", 720, 1280)

    assert inflight["max"] == 2, f"并发峰值应为 2，实际 {inflight['max']}"
    assert len(results) == 6
