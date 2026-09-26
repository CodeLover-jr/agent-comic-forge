# -*- coding: utf-8 -*-
"""共享状态：TaskContext —— 全流水线唯一的 JSON 上下文。

编排Agent / 编剧Agent / 画师Agent / 剪辑Agent 读写同一个实例：
- script / keyframes / clips / narrations / final_video 为流水线产物；
- log() / set_progress() 同时写入 events（供 SSE 重放）并推送给实时订阅者。
"""
import asyncio
import json
import time
from pathlib import Path


def _now_str() -> str:
    return time.strftime("%H:%M:%S")


class TaskContext:
    def __init__(self, task_id: str, user_input: dict, out_dir: Path):
        self.task_id = task_id
        self.user_input = user_input          # {plot, characters, scenes, style, aspect}
        self.out_dir = Path(out_dir)

        self.script = []                      # 分镜列表：[{shot_no, shot_type, description, dialogue, duration}]
        self.keyframes = []                   # 关键帧图片绝对路径
        self.clips = []                       # 视频片段绝对路径
        self.narrations = []                  # [(音频绝对路径, 起始秒)]
        self.final_video = ""                 # 成片 MP4 绝对路径

        self.status = "running"               # running / done / failed
        self.progress = 0                     # 0~100
        self.is_paused = False                # 暂停标记（协作式暂停）
        self._gate = asyncio.Event()          # set=可通行；clear 后任务在安全点挂起
        self._gate.set()
        self.created_at = _now_str()
        self.events = []                      # 全部事件（供 SSE 订阅时重放）
        self._subscribers: list[asyncio.Queue] = []

        self.out_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 事件
    def _emit(self, event: dict) -> None:
        self.events.append(event)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # 理论不会触发（无界队列），防御性兜底
                pass

    def subscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.append(q)

    def log(self, agent: str, message: str) -> None:
        """Agent 思考/动作日志：前端实时打印。"""
        self._emit({"type": "log", "agent": agent, "message": message, "ts": _now_str()})

    def set_progress(self, value: int) -> None:
        self.progress = max(0, min(100, int(value)))
        self._emit({"type": "progress", "value": self.progress})

    # ------------------------------------------------------------- 暂停/继续
    async def checkpoint(self, agent: str = "系统", where: str = "") -> None:
        """协作式暂停安全点：任务被暂停时在此挂起，直到用户点继续。

        只在步骤之间检查，不强行中断进行中的网络请求（避免脏数据/半成品文件）。
        """
        if self.status == "running" and self.is_paused:
            self.log(agent, f"已在安全点挂起（{where}），等待继续...")
            await self._gate.wait()

    def pause(self) -> None:
        if self.status != "running" or self.is_paused:
            return
        self.is_paused = True
        self._gate.clear()
        self.log("系统", "任务暂停中：当前步骤完成后将在安全点挂起")
        self._emit({"type": "paused"})

    def resume(self) -> None:
        if not self.is_paused:
            return
        self.is_paused = False
        self._gate.set()
        self.log("系统", "任务继续")
        self._emit({"type": "resumed"})

    def finish(self, video_url: str) -> None:
        self.status = "done"
        self.set_progress(100)
        self._emit({"type": "done", "video_url": video_url})
        self._dump()

    def fail(self, message: str) -> None:
        self.status = "failed"
        self._emit({"type": "error", "message": message})
        self._dump()

    # ------------------------------------------------------------------ 持久化
    def _dump(self) -> None:
        """把整条任务的 JSON 上下文落盘（output/<task_id>/context.json），便于审计复盘。"""
        try:
            (self.out_dir / "context.json").write_text(
                json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "user_input": self.user_input,
            "script": self.script,
            "keyframes": self.keyframes,
            "clips": self.clips,
            "narrations": [{"audio": p, "start": s} for p, s in self.narrations],
            "final_video": self.final_video,
            "status": self.status,
            "paused": self.is_paused,
            "progress": self.progress,
            "created_at": self.created_at,
        }

    def status_payload(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "paused": self.is_paused,
            "progress": self.progress,
            "shot_count": len(self.script),
            "final_video": self.final_video,
            "created_at": self.created_at,
        }
