# -*- coding: utf-8 -*-
"""链路可观测：轻量 trace 埋点（基于 contextvars，无需给每个函数传参）。

用法（异步工具内部）：
    async with tracer.span("画师", "文生图", config.IMAGE_MODEL):
        ...  # 正常结束记 ok；抛异常记 error 并继续向上抛
工作原理：任务开始时 main 把一个“事件接收槽(sink)”绑定到当前上下文，
span 结束时把事件交给 sink；sink 负责 SSE 推送 + 落库 trace_events。
没有绑定 sink（如单元测试）时 span 仅计时，不产生任何副作用。
"""
import contextvars
import logging
import time

logger = logging.getLogger("tracer")

_sink: contextvars.ContextVar = contextvars.ContextVar("trace_sink", default=None)


def bind_sink(sink) -> contextvars.Token:
    return _sink.set(sink)


def reset_sink(token) -> None:
    _sink.reset(token)


class span:
    """一个执行步骤的计时与状态记录。"""

    def __init__(self, agent: str, step: str, model: str = "") -> None:
        self.agent = agent
        self.step = step
        self.model = model
        self._t0 = 0.0

    async def __aenter__(self) -> "span":
        self._t0 = time.perf_counter()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        duration_ms = int((time.perf_counter() - self._t0) * 1000)
        status = "error" if exc else "ok"
        detail = f"{type(exc).__name__}: {exc}"[:480] if exc else ""
        event = {
            "agent": self.agent, "step": self.step, "model": self.model,
            "status": status, "duration_ms": duration_ms, "detail": detail,
        }
        sink = _sink.get()
        if sink is not None:
            try:
                sink.record(event)
            except Exception:  # noqa: BLE001 - 观测失败绝不能影响主流程
                logger.warning("trace sink 记录失败", exc_info=True)
        return False  # 不吞异常
