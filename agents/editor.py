# -*- coding: utf-8 -*-
"""剪辑 Agent：归一化探测 → 配音 → 拼接 → 字幕 → 混音 → 成片自检。

时间线以「片段实际探测时长」为准（不同视频平台单镜时长可能与剧本不完全一致），
保证字幕、配音、画面三者对齐。
音色规则：台词以「旁白」开头 → 旁白音色；以角色名开头 → 按角色性别；否则默认男声。
"""
import asyncio
import logging
from pathlib import Path

import config
from tools import ffmpeg_tools as ff
from tools import tts

logger = logging.getLogger("editor")


def _pick_voice(dialogue: str, gender_map: dict[str, str]) -> str:
    d = dialogue.strip()
    if d.startswith("旁白") or d.startswith("【旁白】"):
        return "narrator"
    if d.startswith("【男】"):
        return "male"
    if d.startswith("【女】"):
        return "female"
    for name, gender in gender_map.items():
        if name and d.startswith(name):
            return "female" if "女" in gender else "male"
    return "male"


async def run(ctx) -> None:
    if not ctx.clips:
        ctx.log("剪辑Agent", "未收到视频片段，跳过剪辑环节")
        return

    ctx.log("剪辑Agent", "开始归一化片段并探测实际时长（构建真实时间线）...")
    ctx.set_progress(75)
    width, height = config.ASPECT_SIZES.get(ctx.user_input.get("aspect", "9:16"), (720, 1280))

    # ---------- 1. 归一化 + 探测（并发，信号量限流）→ 真实时间线 ----------
    norm_sem = asyncio.Semaphore(config.MAX_CONCURRENT_EDITOR)

    async def _norm_one(i: int, clip: str) -> tuple[str, float]:
        norm = ctx.out_dir / f"norm_{i + 1:02d}.mp4"
        async with norm_sem:
            await ctx.checkpoint("剪辑Agent", f"镜头{i + 1}归一化前")
            await ff.normalize_clip(clip, norm, width, height)
            dur = await ff.probe_duration(norm)
        if dur <= 0.1:  # 探测失败则回退剧本时长
            dur = float(ctx.script[i].get("duration", 4) or 4)
        return str(norm), dur

    # gather 按镜头顺序返回；时间线必须在拿到全部实际时长后顺序累计
    pairs = await asyncio.gather(
        *[_norm_one(i, clip) for i, clip in enumerate(ctx.clips)]
    )
    norm_clips: list[str] = []
    timeline: list[tuple[float, float]] = []  # [(起始秒, 实际时长)]
    cursor = 0.0
    for norm, dur in pairs:
        timeline.append((cursor, dur))
        cursor += dur
        norm_clips.append(norm)

    # ---------- 2. 配音（并发，按真实起始秒） ----------
    ctx.log("剪辑Agent", "并发生成每句台词配音（信号量限流，失败静音占位）...")
    gender_map = {c.get("name", ""): c.get("gender", "") for c in ctx.user_input.get("characters", [])}
    tts_sem = asyncio.Semaphore(config.MAX_CONCURRENT_EDITOR)

    async def _voice_one(i: int, shot: dict) -> tuple[str, float]:
        start, dur = timeline[i]
        dialogue = (shot.get("dialogue") or "").strip()
        async with tts_sem:
            await ctx.checkpoint("剪辑Agent", f"镜头{i + 1}配音前")
            if not dialogue:
                silent = ctx.out_dir / f"voice_silent_{i + 1:02d}.mp3"
                aud = await ff.make_silence(dur, silent)
            else:
                voice = _pick_voice(dialogue, gender_map)
                aud_path = ctx.out_dir / f"voice_{i + 1:02d}.mp3"
                try:
                    ctx.log("剪辑Agent", f"[镜头{i + 1}] 语音合成（{voice}）：{dialogue[:24]}...")
                    aud = await tts.synthesize(dialogue, aud_path, voice=voice)
                except Exception as e:  # noqa: BLE001 - 重试仍失败则静音占位
                    ctx.log("剪辑Agent", f"[镜头{i + 1}] 配音失败：{e}，用静音占位 ⚠")
                    silent = ctx.out_dir / f"voice_silent_{i + 1:02d}.mp3"
                    aud = await ff.make_silence(dur, silent)
        return aud, start

    # gather 保证顺序：narration 与镜头一一对应
    ctx.narrations = list(await asyncio.gather(
        *[_voice_one(i, shot) for i, shot in enumerate(ctx.script)]
    ))

    # ---------- 3. ffmpeg 合成 ----------
    await ctx.checkpoint("剪辑Agent", "最终合成前")
    ctx.log("剪辑Agent", "开始 ffmpeg 合成：拼接 → 烧录字幕 → 混入配音与 BGM...")
    silent = ctx.out_dir / "video_silent.mp4"
    await ff.concat_clips(norm_clips, silent)

    ass_path = ff.write_ass(ctx.script, ctx.out_dir / "subs.ass", width, height, timeline=timeline)
    mixed = ctx.out_dir / "audio_mix.wav"
    await ff.mix_audio(ctx.narrations, mixed, cursor, bgm=config.BGM_PATH)

    final = ctx.out_dir / "final_video.mp4"
    await ff.burn_and_mux(silent, ass_path, mixed, final)
    ctx.final_video = str(final)

    # ---------- 4. 成片自检 ----------
    ctx.log("剪辑Agent", "成片自检：校验总时长与音画同步...")
    actual = await ff.probe_duration(final)
    expected = cursor
    tolerance = max(2.0, expected * 0.15)
    if abs(actual - expected) <= tolerance:
        ctx.log("剪辑Agent", f"自检通过：实际 {actual:.1f}s ≈ 预期 {expected:.1f}s ✓")
    else:
        ctx.log("剪辑Agent",
                f"自检发现时长偏差（实际 {actual:.1f}s / 预期 {expected:.1f}s），重新封装截齐...")
        retry = ctx.out_dir / "final_video_v2.mp4"
        await ff.burn_and_mux(silent, ass_path, mixed, retry)
        actual = await ff.probe_duration(retry)
        ctx.log("剪辑Agent", f"重新封装完成：实际时长 {actual:.1f}s")
        ctx.final_video = str(retry)

    audio_dur = await ff.probe_duration(mixed)
    if audio_dur < 0.5:
        ctx.log("剪辑Agent", "警告：最终音轨近乎静音，请检查 DASHSCOPE_API_KEY 与 TTS 模型/音色 ⚠")

    ctx.set_progress(100)
    ctx.log("剪辑Agent", f"成片完成：{Path(ctx.final_video).name}")
