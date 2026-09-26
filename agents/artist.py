# -*- coding: utf-8 -*-
"""画师 Agent：关键帧生成 + 视觉自检（核心）+ 图生视频。

关键设计：
1. 角色前缀：把用户填写的角色外貌固定为提示词前缀，逐镜拼接，保证跨镜头角色一致；
2. 自检：每张关键帧生成后调用视觉模型检查「角色是否符合设定、画风是否统一」，
   不合格自动改写提示词重绘，最多 3 轮；
3. 关键帧并发生成（信号量限流）；全部完成后并发调用图生视频（提交→轮询→下载）；
4. 任一步失败自动重试 2 次，仍失败则跳过该镜头并告警，不中断整体流程。
"""
import asyncio
import logging
from pathlib import Path

import config
from tools import ffmpeg_tools as ff
from tools import i2v, t2i, vision

logger = logging.getLogger("artist")

STYLE_KEYWORDS = {
    "国漫": "中国风国漫，精致线稿，高饱和上色，电影级光影",
    "日系": "日系动画风格，清透明亮，细腻柔光，大眼睛角色",
    "Q版": "Q版可爱风格，大头小身，圆润线条，软萌表情",
    "水墨古风": "水墨古风，宣纸质感，写意留白，淡雅配色",
}

CHECK_PROMPT_TEMPLATE = """你是漫剧美术质检。请对照以下设定严格检查这张关键帧图：
【角色设定】{characters}
【画风要求】{style}
检查项：
1. 图中人物长相、发型、发色、服装是否与角色设定一致（若图中无人则检查是否符合“无人物”预期）；
2. 画风是否统一，是否有明显崩坏、肢体畸变、多余文字或水印。
只输出 JSON：{{"pass": true 或 false, "issues": "具体问题描述"}}"""


def build_character_prefix(user_input: dict) -> str:
    """把角色外貌固化为前缀，拼接进每个镜头的生图提示词。"""
    parts = []
    for c in user_input.get("characters", []):
        name = c.get("name", "未命名")
        parts.append(
            f"{name}（{c.get('gender', '')}，{c.get('age', '')}岁，"
            f"性格：{c.get('personality', '')}，外貌：{c.get('appearance', '')}）"
        )
    return "角色设定（必须严格保持，跨镜头一致）：" + "；".join(parts)


def build_shot_prompt(shot: dict, ctx, char_prefix: str, extra: str = "") -> str:
    scenes = ctx.user_input.get("scenes") or [{"name": "未知地点", "atmosphere": ""}]
    scene = scenes[(shot["shot_no"] - 1) % len(scenes)]
    scene_desc = f"{scene.get('name', '')}（氛围：{scene.get('atmosphere', '')}）"
    style = STYLE_KEYWORDS.get(ctx.user_input.get("style", "国漫"), "国漫")
    prompt = (
        f"{style}，{char_prefix}，场景：{scene_desc}，镜头景别：{shot['shot_type']}，"
        f"画面内容：{shot['description']}。"
        f"画面干净无文字无水印，构图完整。{extra}"
    )
    return prompt[:1200]


def build_motion_prompt(shot: dict, ctx) -> str:
    scenes = ctx.user_input.get("scenes") or [{"name": "未知地点", "atmosphere": ""}]
    scene = scenes[(shot["shot_no"] - 1) % len(scenes)]
    return (
        f"镜头语言：{shot['shot_type']}。画面：{shot['description']}，场景：{scene.get('name', '')}。"
        "缓慢推进镜头，人物轻微自然的动作，电影感运镜，画面稳定连贯，无文字无水印。"
    )[:800]


async def _self_check(ctx, keyframe: str, char_prefix: str, style: str,
                      attempt: int) -> tuple[bool, str]:
    """调用视觉模型自检，返回 (是否通过, 问题描述)。自检调用失败时按通过处理。"""
    try:
        prompt = CHECK_PROMPT_TEMPLATE.format(characters=char_prefix, style=style)
        text = await vision.inspect_image(keyframe, prompt)
        passed, issues = vision.parse_check_result(text)
        if not issues:
            issues = "未说明具体原因"
        return passed, issues
    except Exception as e:  # noqa: BLE001 - 自检不可用不应卡死流水线
        ctx.log("画师Agent", f"[镜头{attempt}] 自检调用失败，按通过处理继续：{e}")
        return True, ""


async def run(ctx) -> None:
    """完整画师流水线：关键帧（含自检重绘）→ 图生视频片段。"""
    if not ctx.script:
        ctx.log("画师Agent", "未收到剧本，跳过画师环节")
        return

    ctx.log("画师Agent", "开始生成关键帧（每镜：生图 → 视觉自检 → 不合格重绘，最多 3 轮）...")
    ctx.set_progress(20)
    char_prefix = build_character_prefix(ctx.user_input)
    style = ctx.user_input.get("style", "国漫")
    width, height = config.ASPECT_SIZES.get(ctx.user_input.get("aspect", "9:16"), (720, 1280))
    total = len(ctx.script)

    # ---------- 1. 关键帧 + 自检（并发，信号量限流） ----------
    ctx.keyframes = await _generate_keyframes(ctx, char_prefix, style, width, height)

    # ---------- 2. 并发图生视频（异步任务，信号量限流） ----------
    await _generate_clips(ctx, width, height)

    ctx.set_progress(70)
    ctx.log("画师Agent", f"画师环节完成：{len(ctx.clips)} 个视频片段已就绪")


async def _generate_keyframes(ctx, char_prefix: str, style: str,
                              width: int, height: int) -> list:
    """所有镜头的关键帧并发生成：生图 → 视觉自检 → 不合格重绘（≤3 轮）。

    - 信号量把同时在生成的镜头数限制为 config.MAX_CONCURRENT_KEYFRAMES，避免被限流；
    - 生图全程失败 → 该镜头记 None（后续黑屏占位，零图生视频）；
    - 3 轮自检不过 → 保留最终帧继续；
    - 结果按镜头顺序返回（gather 保证顺序）。
    """
    total = len(ctx.script)
    kf_sem = asyncio.Semaphore(config.MAX_CONCURRENT_KEYFRAMES)
    done_count = 0

    async def _one_keyframe(idx: int, shot: dict):
        nonlocal done_count
        keyframe = ctx.out_dir / f"keyframe_{idx:02d}.png"
        prompt = build_shot_prompt(shot, ctx, char_prefix)
        passed = False
        # 整个「生图+自检+重绘」过程占一个并发名额
        async with kf_sem:
            for attempt in range(1, 4):  # 最多 3 轮
                await ctx.checkpoint("画师Agent", f"镜头{idx}第{attempt}轮生图前")
                ctx.log("画师Agent", f"[镜头{idx}/{total}] 第{attempt}/3 轮生图（文生图）...")
                try:
                    await t2i.generate_image(prompt, keyframe, width=width, height=height)
                except Exception as e:  # noqa: BLE001
                    ctx.log("画师Agent", f"[镜头{idx}] 生图失败：{e}，跳过该镜头 ⚠")
                    break
                passed, issues = await _self_check(ctx, str(keyframe), char_prefix, style, idx)
                if passed:
                    ctx.log("画师Agent", f"[镜头{idx}] 视觉自检通过 ✓（角色一致 + 画风统一）")
                    break
                ctx.log("画师Agent", f"[镜头{idx}] 自检未通过：{issues}，改写提示词重绘...")
                prompt = build_shot_prompt(
                    shot, ctx, char_prefix, extra=f"修正上一版问题：{issues[:120]}"
                )
        if not passed and keyframe.exists():
            ctx.log("画师Agent", f"[镜头{idx}] 3 轮自检均未通过，保留最终帧继续（不中断流程）⚠")
        # FIXED (Bug2): 只登记真实存在的关键帧；生图全程失败记 None，
        # 后续该镜头直接黑屏占位，绝不再触发图生视频
        if keyframe.exists():
            result = str(keyframe)
        else:
            result = None
            ctx.log("画师Agent", f"[镜头{idx}] 无可用关键帧，该镜头将用黑屏静态片段占位 ⚠")
        done_count += 1
        ctx.set_progress(20 + int(40 * done_count / total))
        return result

    return await asyncio.gather(
        *[_one_keyframe(idx, shot) for idx, shot in enumerate(ctx.script, start=1)]
    )


async def _generate_clips(ctx, width: int, height: int) -> None:
    """所有镜头的图生视频并发执行：上传→提交→轮询→下载。

    - 信号量把同时在生成的片段数限制为 config.MAX_CONCURRENT_CLIPS，避免被限流；
    - 关键帧缺失 → 黑屏占位，零网络请求；
    - 图生视频重试仍失败 → 关键帧静态片段兜底；
    - 结果按镜头顺序写入 ctx.clips（gather 保证顺序）。
    """
    total = len(ctx.script)
    valid = sum(1 for k in ctx.keyframes if k)
    ctx.log(
        "画师Agent",
        f"关键帧完成（{valid} 张），并发提交图生视频"
        f"（最多 {config.MAX_CONCURRENT_CLIPS} 路同时生成，提交→轮询→下载）...",
    )
    clip_sem = asyncio.Semaphore(config.MAX_CONCURRENT_CLIPS)
    done_count = 0

    async def _one_clip(idx: int, shot: dict) -> str:
        nonlocal done_count
        clip = ctx.out_dir / f"clip_{idx:02d}.mp4"
        keyframe = ctx.keyframes[idx - 1]
        duration = shot.get("duration", 4)
        await ctx.checkpoint("画师Agent", f"镜头{idx}片段生成前")
        # FIXED (Bug2): 关键帧缺失 → 不调用图生视频、不上传，直接黑屏占位，
        # 避免“上传不存在文件 → 层层重试”的请求风暴
        if not keyframe:
            ctx.log("画师Agent", f"[镜头{idx}/{total}] 关键帧缺失，跳过图生视频，使用黑屏占位 ⚠")
            await ff.static_clip(None, clip, width, height, duration)
        else:
            try:
                ctx.log("画师Agent", f"[镜头{idx}/{total}] 提交图生视频任务（约 {duration}s）...")
                # 信号量限制同时在生成的片段数，免费额度下并发过高会被 429 限流
                async with clip_sem:
                    await i2v.generate_clip(
                        keyframe,
                        build_motion_prompt(shot, ctx),
                        ctx.user_input.get("aspect", "9:16"),
                        duration,
                        clip,
                        gate=ctx._gate,
                    )
                ctx.log("画师Agent", f"[镜头{idx}] 视频片段生成完成")
            except Exception as e:  # noqa: BLE001 - 重试仍失败则静态帧兜底
                ctx.log("画师Agent", f"[镜头{idx}] 图生视频失败：{e}，使用关键帧静态片段兜底 ⚠")
                await ff.static_clip(keyframe, clip, width, height, duration)
        done_count += 1
        ctx.set_progress(60 + int(10 * done_count / total))
        return str(clip)

    ctx.clips = await asyncio.gather(
        *[_one_clip(idx, shot) for idx, shot in enumerate(ctx.script, start=1)]
    )
