# -*- coding: utf-8 -*-
"""ffmpeg 封装：片段归一化、拼接、静音/静态帧占位、混音、字幕烧录、时长探测。"""
import asyncio
import json
import logging
from pathlib import Path

logger = logging.getLogger("ffmpeg")


def _fpath(p) -> str:
    """供 filter_complex 使用的路径：正斜杠 + 转义冒号（Windows 盘符）。"""
    s = str(p).replace("\\", "/")
    return s.replace(":", "\\:")


def _esc_concat_path(p) -> str:
    """concat list 文件中的路径转义（单引号转义为 '\''）。"""
    return str(p).replace("\\", "/").replace("'", "'\\''")


async def _run(cmd: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 执行失败: {' '.join(cmd)}\n{stderr.decode('utf-8', errors='ignore')[-800:]}"
        )


async def probe_duration(path) -> float:
    """用 ffprobe 探测媒体时长（秒）。"""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    try:
        data = json.loads(out.decode("utf-8", errors="ignore") or "{}")
        return float(data.get("format", {}).get("duration", 0.0) or 0.0)
    except Exception:
        return 0.0


async def normalize_clip(src, dst, width: int, height: int, fps: int = 25) -> str:
    """统一分辨率/帧率/编码，保证后续 concat 不花屏。"""
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={fps},setsar=1"
    )
    cmd = ["ffmpeg", "-y", "-i", str(src), "-vf", vf, "-an",
           "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
           "-pix_fmt", "yuv420p", str(dst)]
    await _run(cmd)
    return str(dst)


async def concat_clips(clips: list[str], out) -> str:
    """用 concat demuxer 拼接（各片段已归一化），视频轨 copy。"""
    list_file = Path(out).with_suffix(".concat.txt")
    lines = [f"file '{_esc_concat_path(p)}'" for p in clips]
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
           "-c", "copy", str(out)]
    await _run(cmd)
    return str(out)


async def make_silence(duration: float, out, sample_rate: int = 24000) -> str:
    """生成指定时长的静音 mp3（配音失败时的占位）。"""
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=mono",
           "-t", f"{duration:.2f}", "-q:a", "9", str(out)]
    await _run(cmd)
    return str(out)


async def static_clip(image, out, width: int, height: int, duration: float, fps: int = 25) -> str:
    """用单张关键帧生成静态视频片段（图生视频失败时的兜底，保证成片不断片）。"""
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={fps},setsar=1"
    )
    if image and Path(image).exists():
        cmd = ["ffmpeg", "-y", "-loop", "1", "-i", str(image), "-t", f"{duration:.2f}",
               "-vf", vf, "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
               "-pix_fmt", "yuv420p", str(out)]
    else:
        cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={fps}",
               "-t", f"{duration:.2f}", "-c:v", "libx264", "-crf", "20",
               "-preset", "veryfast", "-pix_fmt", "yuv420p", str(out)]
    await _run(cmd)
    return str(out)


async def mix_audio(parts: list[tuple[str, float]], out, total: float, bgm: str = "") -> str:
    """混音：把每段配音按起始秒 adelay 对齐，可选 BGM 压低铺底，输出 wav。

    parts: [(音频路径, 起始秒)]
    """
    if not parts:
        await make_silence(total, out)
        return str(out)

    inputs: list[str] = []
    filters: list[str] = []
    for i, (path, delay) in enumerate(parts):
        inputs += ["-i", str(path)]
        ms = int(round(delay * 1000))
        filters.append(f"[{i}:a]adelay={ms}:all=1,apad=pad_dur=2[a{i}]")

    mix_in = "".join(f"[a{i}]" for i in range(len(parts)))
    n_inputs = len(parts)
    if bgm and Path(bgm).exists():
        inputs += ["-i", str(bgm)]
        filters.append(
            f"[{n_inputs}:a]volume=0.15,aloop=loop=-1:size=2e+09,atrim=0:{total:.2f}[bg]"
        )
        filters.append(
            f"{mix_in}[bg]amix=inputs={n_inputs + 1}:normalize=0:duration=longest,"
            f"atrim=0:{total:.2f}[out]"
        )
    else:
        filters.append(
            f"{mix_in}amix=inputs={n_inputs}:normalize=0:duration=longest,atrim=0:{total:.2f}[out]"
        )

    cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filters),
           "-map", "[out]", str(out)]
    await _run(cmd)
    return str(out)


def _fmt_ass_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, cs = divmod(rem, 1000)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def write_ass(shots: list[dict], out_path, width: int, height: int,
              timeline: list[tuple[float, float]] | None = None) -> str:
    """按镜头时间轴生成 ASS 字幕（台词非空才写条目）。

    timeline: 可选，[(起始秒, 时长秒)]，使用片段实际探测时长；
    不传则按剧本中每个镜头的 duration 累计。
    """
    fs = int(max(24, min(width, height) * 0.05))
    style = (
        f"Style: Default,Microsoft YaHei,{fs},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
        "-1,0,0,0,100,100,0,0,1,2,1,2,40,40,60,1"
    )
    lines = [
        "[Script Info]", "ScriptType: v4.00+",
        f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 2", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        style, "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for i, s in enumerate(shots):
        if timeline is not None and i < len(timeline):
            start, dur = timeline[i]
        else:
            # 无 timeline 时：从 0 开始按剧本 duration 累计
            dur = float(s.get("duration", 4) or 4)
            start = sum(float(shots[j].get("duration", 4) or 4) for j in range(i))
        dialogue = (s.get("dialogue") or "").strip()
        if dialogue:
            text = dialogue.replace("{", "｛").replace("}", "｝").replace("\n", "\\N")
            lines.append(
                f"Dialogue: 0,{_fmt_ass_time(start)},{_fmt_ass_time(start + dur)},Default,,0,0,0,,{text}"
            )
    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(out_path)


async def burn_and_mux(video, ass_path, audio_path, out) -> str:
    """烧录字幕 + 混入音轨，产出最终 MP4（-shortest 防止音轨溢出画面）。"""
    vf = f"subtitles={_fpath(ass_path)}"
    cmd = ["ffmpeg", "-y", "-i", str(video), "-i", str(audio_path),
           "-vf", vf, "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
           "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)]
    await _run(cmd)
    return str(out)
