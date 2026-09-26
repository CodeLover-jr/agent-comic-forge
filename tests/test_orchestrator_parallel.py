# -*- coding: utf-8 -*-
"""编排 Agent 并行工具调用（orchestrator._function_calling_loop）单元测试。

覆盖：
1. 模型一条回复给出全部三个工具调用 → 仅 2 次 LLM 往返（批量 + finish），按顺序执行；
2. 批量调用顺序错乱时，前置依赖拦截 + 同批后续调用补齐，最终全部执行；
3. 含 tool_calls 的 assistant 消息被补写进对话历史；
4. 模型不返回工具调用时，自动补齐全部环节。

运行：
    .venv\\Scripts\\python.exe -m pytest tests/test_orchestrator_parallel.py -v
"""
import asyncio
from types import SimpleNamespace

import pytest

from agents import orchestrator
import config


class FakeCtx:
    def __init__(self):
        self.user_input = {"plot": "x", "characters": [], "scenes": []}
        self._gate = asyncio.Event()
        self._gate.set()

    async def checkpoint(self, agent="", where=""):
        await self._gate.wait()

    def log(self, agent, message):
        pass

    def set_progress(self, value):
        pass


def _tool_call(cid, name):
    return SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments="{}"))


def _msg(*names, start=0):
    return SimpleNamespace(
        tool_calls=[_tool_call(f"c{start + i}", n) for i, n in enumerate(names)],
        content=None,
    )


def _patch_agents(monkeypatch, order):
    def make(label):
        async def stub(ctx):
            order.append(label)
        return stub

    monkeypatch.setitem(orchestrator.AGENT_FUNCS, "generate_script",
                        make("generate_script"))
    monkeypatch.setitem(orchestrator.AGENT_FUNCS, "generate_keyframes_and_clips",
                        make("generate_keyframes_and_clips"))
    monkeypatch.setitem(orchestrator.AGENT_FUNCS, "assemble_final_video",
                        make("assemble_final_video"))


@pytest.mark.asyncio
async def test_parallel_calls_two_round_trips(monkeypatch):
    """一条回复三个调用 + 一条 finish：恰好 2 次 LLM 往返，顺序正确。"""
    monkeypatch.setattr(config, "DASHSCOPE_API_KEY", "x")
    monkeypatch.setattr(config, "QWEN_MODEL", "m")
    order, history, calls = [], [], {"n": 0}
    _patch_agents(monkeypatch, order)

    responses = [
        _msg("generate_script", "generate_keyframes_and_clips",
             "assemble_final_video"),
        _msg("finish", start=3),
    ]

    async def fake_chat(messages, tools=None, temperature=0.3):
        history.append(list(messages))
        calls["n"] += 1
        return responses[calls["n"] - 1]

    monkeypatch.setattr(orchestrator.llm, "chat", fake_chat)

    await orchestrator._function_calling_loop(FakeCtx())

    assert calls["n"] == 2
    assert order == ["generate_script", "generate_keyframes_and_clips",
                     "assemble_final_video"]


@pytest.mark.asyncio
async def test_wrong_order_batch_recovers(monkeypatch):
    """批量顺序错乱（assemble 排第一）：拦截后同批后续仍执行，最终全部完成。"""
    monkeypatch.setattr(config, "DASHSCOPE_API_KEY", "x")
    monkeypatch.setattr(config, "QWEN_MODEL", "m")
    order = []
    _patch_agents(monkeypatch, order)

    responses = [
        _msg("assemble_final_video", "generate_script",
             "generate_keyframes_and_clips", "assemble_final_video"),
        _msg("finish", start=4),
    ]
    calls = {"n": 0}

    async def fake_chat(messages, tools=None, temperature=0.3):
        calls["n"] += 1
        return responses[calls["n"] - 1]

    monkeypatch.setattr(orchestrator.llm, "chat", fake_chat)

    await orchestrator._function_calling_loop(FakeCtx())

    assert sorted(order) == ["assemble_final_video",
                            "generate_keyframes_and_clips", "generate_script"]


@pytest.mark.asyncio
async def test_assistant_message_appended(monkeypatch):
    """批量执行后，第二轮对话历史中必须有含 tool_calls 的 assistant 消息和 3 条 tool 结果。"""
    monkeypatch.setattr(config, "DASHSCOPE_API_KEY", "x")
    monkeypatch.setattr(config, "QWEN_MODEL", "m")
    order, history, calls = [], [], {"n": 0}
    _patch_agents(monkeypatch, order)

    responses = [
        _msg("generate_script", "generate_keyframes_and_clips",
             "assemble_final_video"),
        _msg("finish", start=3),
    ]

    async def fake_chat(messages, tools=None, temperature=0.3):
        history.append(list(messages))
        calls["n"] += 1
        return responses[calls["n"] - 1]

    monkeypatch.setattr(orchestrator.llm, "chat", fake_chat)

    await orchestrator._function_calling_loop(FakeCtx())

    second_round = history[1]
    assistants = [m for m in second_round if m.get("role") == "assistant"]
    tool_results = [m for m in second_round if m.get("role") == "tool"]
    assert any(len(m.get("tool_calls", [])) == 3 for m in assistants), \
        "缺少含 3 个 tool_calls 的 assistant 消息"
    assert len(tool_results) == 3


@pytest.mark.asyncio
async def test_no_tool_calls_autofills(monkeypatch):
    """模型不返回工具调用：自动补齐三个环节，不卡死。"""
    monkeypatch.setattr(config, "DASHSCOPE_API_KEY", "x")
    monkeypatch.setattr(config, "QWEN_MODEL", "m")
    order = []
    _patch_agents(monkeypatch, order)

    async def fake_chat(messages, tools=None, temperature=0.3):
        return SimpleNamespace(tool_calls=None, content="无法调用工具")

    monkeypatch.setattr(orchestrator.llm, "chat", fake_chat)

    await orchestrator._function_calling_loop(FakeCtx())

    assert sorted(order) == ["assemble_final_video",
                            "generate_keyframes_and_clips", "generate_script"]
