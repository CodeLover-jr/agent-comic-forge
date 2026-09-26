# -*- coding: utf-8 -*-
"""剪辑 Agent 并发（归一化 / TTS）单元测试。

覆盖：
1. 归一化并发，时间线按探测实际时长顺序累计，配音起始秒正确；
2. 无台词镜头生成静音；
3. TTS 失败 → 静音占位，不中断；
4. 归一化 / TTS 信号量峰值 = MAX_CONCURRENT_EDITOR。

运行：
    .venv\\Scripts\\python.exe -m pytest tests/test_editor_concurrent.py -v
"""
import asyncio
from types import SimpleNamespace

import pytest

from agents import editor
import config


class FakeCtx:
    def __init__(self, tmp_path, dialogues, norm_durs):
        n = len(dialogues)
        self.clips = [str(tmp_path / f"clip_{i:02d}.mp4") for i in range(1, n + 1)]
        self.script = [
            {"shot_no": i + 1, "duration": norm_durs[i], "dialogue": d}
            for i, d in enumerate(dialogues)
        ]
        self.user_input = {
            "characters": [{"name": "阿明", "gender": "男"}],
            "scenes": [], "style": "国漫", "aspect": "9:16",
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


def _patch_ff(monkeypatch, norm_durs, holder, norm_sleep=0.0, track_norm=None):
    async def normalize(src, dst, width, height):
        if track_norm is not None:
            track_norm["now"] += 1
            track_norm["max"] = max(track_norm["max"], track_norm["now"])
            await asyncio.sleep(norm_sleep)
            track_norm["now"] -= 1
        return str(dst)

    async def probe(path):
        s = str(path)
        if s.endswith("audio_mix.wav"):
            return 99.0
        if "final_video" in s:
            return holder.get("total", 16.0)
        if "norm_" in s:
            idx = int(s.split("norm_")[1][:2]) - 1
            return norm_durs[idx]
        return 0.0

    async def concat(clips, out):
        return str(out)

    def write_ass(shots, out, width, height, timeline=None):
        return "subs"

    async def mix(parts, out, total, bgm=""):
        holder["parts"] = parts
        holder["total"] = total
        return str(out)

    async def burn(video, ass, audio, out):
        return str(out)

    async def silence(dur, out):
        holder.setdefault("silence", []).append(str(out))
        return str(out)

    # 默认 TTS 桩：需要自定义的用例在 _patch_ff 之后重新 setattr 覆盖
    async def default_synth(text, out, voice="male"):
        return str(out)

    monkeypatch.setattr(editor.ff, "normalize_clip", normalize)
    monkeypatch.setattr(editor.ff, "probe_duration", probe)
    monkeypatch.setattr(editor.ff, "concat_clips", concat)
    monkeypatch.setattr(editor.ff, "write_ass", write_ass)
    monkeypatch.setattr(editor.ff, "mix_audio", mix)
    monkeypatch.setattr(editor.ff, "burn_and_mux", burn)
    monkeypatch.setattr(editor.ff, "make_silence", silence)
    monkeypatch.setattr(editor.tts, "synthesize", default_synth)


@pytest.mark.asyncio
async def test_timeline_from_probed_durations(tmp_path, monkeypatch):
    """归一化并发后，时间线按实际探测时长累计；无台词镜头静音；起始秒正确。"""
    dialogues = ["阿明：你好", "", "旁白：走吧", "阿明：来了"]
    norm_durs = [4.0, 3.0, 5.0, 4.0]
    holder = {}
    ctx = FakeCtx(tmp_path, dialogues, norm_durs)
    _patch_ff(monkeypatch, norm_durs, holder)

    await editor.run(ctx)

    assert holder["total"] == pytest.approx(16.0)
    starts = [round(s, 1) for _, s in holder["parts"]]
    assert starts == [0.0, 4.0, 7.0, 12.0]
    # 第 2 镜无台词 → 静音文件，且该镜 narration 用的正是静音路径
    assert len(holder["silence"]) == 1
    assert holder["parts"][1][0] == holder["silence"][0]


@pytest.mark.asyncio
async def test_tts_concurrent_peak(tmp_path, monkeypatch):
    """TTS 并发：6 镜配音，同时在合成的峰值 = MAX_CONCURRENT_EDITOR(3)。"""
    monkeypatch.setattr(config, "MAX_CONCURRENT_EDITOR", 3)
    dialogues = [f"阿明：台词{i}" for i in range(6)]
    norm_durs = [4.0] * 6
    holder, inflight = {}, {"now": 0, "max": 0}
    ctx = FakeCtx(tmp_path, dialogues, norm_durs)
    _patch_ff(monkeypatch, norm_durs, holder)

    async def synth(text, out, voice="male"):
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        await asyncio.sleep(0.15)
        inflight["now"] -= 1
        return str(out)

    monkeypatch.setattr(editor.tts, "synthesize", synth)

    await editor.run(ctx)

    assert inflight["max"] == 3
    assert len(holder["parts"]) == 6


@pytest.mark.asyncio
async def test_tts_failure_silence_fallback(tmp_path, monkeypatch):
    """第 2 镜 TTS 失败：静音占位，其余正常，混音仍执行。"""
    dialogues = ["阿明：一", "阿明：二", "阿明：三"]
    norm_durs = [4.0, 4.0, 4.0]
    holder = {}
    ctx = FakeCtx(tmp_path, dialogues, norm_durs)
    _patch_ff(monkeypatch, norm_durs, holder)

    async def synth(text, out, voice="male"):
        if str(out).endswith("voice_02.mp3"):
            raise RuntimeError("tts engine 411")
        return str(out)

    monkeypatch.setattr(editor.tts, "synthesize", synth)

    await editor.run(ctx)

    assert len(holder["parts"]) == 3
    # 第 2 镜走静音兜底
    assert "voice_silent_02.mp3" in holder["parts"][1][0]


@pytest.mark.asyncio
async def test_normalize_concurrent_peak(tmp_path, monkeypatch):
    """归一化并发：6 个片段，同时在归一化的峰值 = MAX_CONCURRENT_EDITOR(3)。"""
    monkeypatch.setattr(config, "MAX_CONCURRENT_EDITOR", 3)
    dialogues = [""] * 6
    norm_durs = [4.0] * 6
    holder, track = {}, {"now": 0, "max": 0}
    ctx = FakeCtx(tmp_path, dialogues, norm_durs)
    _patch_ff(monkeypatch, norm_durs, holder, norm_sleep=0.15, track_norm=track)

    await editor.run(ctx)

    assert track["max"] == 3
