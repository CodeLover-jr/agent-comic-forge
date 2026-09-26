# -*- coding: utf-8 -*-
"""编排 Agent（核心）：Function Calling 工具调用循环 + 共享状态（JSON 上下文）。

- 把 编剧/画师/剪辑 注册为可调用工具，让豆包 doubao-seed 模型自主规划并依次调度；
- 前端通过 ctx.log() 实时看到每个 Agent 的思考与动作；
- 前置依赖校验：画师依赖剧本、剪辑依赖画师，违规调用会被拦截并反馈给模型修正；
- LLM 不可用或编排失控时，自动降级为「顺序编排」模式，保证流程不中断。
"""
import json
import logging

import config
from agents import artist, editor, screenwriter
from tools import llm

logger = logging.getLogger("orchestrator")

# 注册给模型的工具清单
TOOLS = [
    {"type": "function", "function": {
        "name": "generate_script",
        "description": "调用编剧Agent：根据剧情/角色/场景扩写为结构化分镜剧本（含冲突钩子与结尾悬念）",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "generate_keyframes_and_clips",
        "description": "调用画师Agent：生成全部关键帧（视觉自检+重绘）并逐镜生成视频片段",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "assemble_final_video",
        "description": "调用剪辑Agent：配音、拼接、字幕、混音并输出最终 MP4 成片",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "finish",
        "description": "全部环节完成，结束编排",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
]

# 工具名 -> 实际 Agent 函数
AGENT_FUNCS = {
    "generate_script": screenwriter.generate_script,
    "generate_keyframes_and_clips": artist.run,
    "assemble_final_video": editor.run,
}

# 工具的前置依赖：key 依赖 value 中的工具先完成
PREREQUISITES = {
    "generate_keyframes_and_clips": {"generate_script"},
    "assemble_final_video": {"generate_script", "generate_keyframes_and_clips"},
}

SYSTEM_PROMPT = """你是「AI漫剧一键生成平台」的流水线编排者，职责是把生产任务拆解并调度给专业 Agent。
用户输入已通过前端校验。你必须：
1. 严格按顺序调度：generate_script → generate_keyframes_and_clips → assemble_final_video → finish；
2. 在【第一条回复】中一次性并行给出前三个工具调用（按上述顺序放在同一条回复里），
   不要分多轮逐个调用；待三个工具全部返回后，下一条回复再调用 finish；
3. 每次只通过函数调用执行动作，不要输出无关文字；每个工具只调用一次；
4. 某个工具返回错误时，先向用户说明并继续规划下一步。"""


def _summarize(user_input: dict) -> str:
    return (
        f"用户输入（JSON）：{json.dumps(user_input, ensure_ascii=False)}\n"
        "请按顺序编排流水线，通过函数调用执行。"
    )


async def run(ctx) -> None:
    ctx.log("编排Agent", "任务启动：解析用户输入，规划流水线（Function Calling 调度）...")
    ctx.set_progress(5)

    if not config.DASHSCOPE_API_KEY or not config.QWEN_MODEL:
        ctx.log("编排Agent", "未检测到 DASHSCOPE_API_KEY / QWEN_MODEL，降级为顺序编排：编剧 → 画师 → 剪辑")
        await screenwriter.generate_script(ctx)
        await artist.run(ctx)
        await editor.run(ctx)
        return

    await _function_calling_loop(ctx)


async def _function_calling_loop(ctx) -> None:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _summarize(ctx.user_input)},
    ]
    done: set[str] = set()

    for step in range(8):
        await ctx.checkpoint("编排Agent", "决策前")
        ctx.log("编排Agent", f"正在决策下一步（决策轮 {step + 1}/8）...")
        try:
            msg = await llm.chat(messages, tools=TOOLS, temperature=0.3)
        except Exception as e:  # noqa: BLE001 - 编排 LLM 不可用则顺序执行
            ctx.log("编排Agent", f"编排 LLM 不可用（{e}），降级为顺序编排：编剧 → 画师 → 剪辑")
            await screenwriter.generate_script(ctx)
            await artist.run(ctx)
            await editor.run(ctx)
            return

        if not getattr(msg, "tool_calls", None):
            # 模型没有返回工具调用：补齐未完成的环节后结束
            remaining = [n for n in AGENT_FUNCS if n not in done]
            if remaining:
                ctx.log("编排Agent", "模型未返回工具调用，自动补齐剩余环节")
                for name in remaining:
                    ctx.log("编排Agent", f"调用工具：{name}")
                    await ctx.checkpoint("编排Agent", f"执行{name}前")
                    await AGENT_FUNCS[name](ctx)
                    done.add(name)
            break

        # FIXED: 必须把含 tool_calls 的 assistant 消息补进对话历史，
        # 否则后面追加的 tool 结果消息缺少前置 assistant 消息，
        # 严格的 OpenAI 兼容接口会直接 400
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name,
                              "arguments": tc.function.arguments or "{}"}}
                for tc in msg.tool_calls
            ],
        })

        for tc in msg.tool_calls:
            name = tc.function.name
            args_text = tc.function.arguments or "{}"
            try:
                json.loads(args_text)
            except json.JSONDecodeError:
                args_text = "{}"

            if name == "finish":
                ctx.log("编排Agent", "流水线编排完成，任务收尾")
                return

            fn = AGENT_FUNCS.get(name)
            if not fn:
                ctx.log("编排Agent", f"模型请求了未知工具 {name}，已忽略")
                messages.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": json.dumps({"ok": False, "error": f"未知工具 {name}"}, ensure_ascii=False),
                })
                continue

            if name in done:
                ctx.log("编排Agent", f"工具 {name} 已执行过，跳过重复调用")
                messages.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": json.dumps({"ok": True, "skipped": "already-done"}, ensure_ascii=False),
                })
                continue

            missing = PREREQUISITES.get(name, set()) - done
            if missing:
                ctx.log("编排Agent", f"工具 {name} 的前置环节 {sorted(missing)} 未完成，已拦截并反馈给模型")
                messages.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": json.dumps(
                        {"ok": False, "error": f"前置环节未完成: {sorted(missing)}，请先调用它们"},
                        ensure_ascii=False),
                })
                continue

            ctx.log("编排Agent", f"调用工具：{name}")
            await ctx.checkpoint("编排Agent", f"执行{name}前")
            try:
                await fn(ctx)
                result = {"ok": True}
            except Exception as e:  # noqa: BLE001
                result = {"ok": False, "error": str(e)}
                ctx.log("编排Agent", f"工具 {name} 执行异常：{e}")
            done.add(name)
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })

    # 循环结束仍未 finish：补齐未完成环节，保证成片产出
    remaining = [n for n in AGENT_FUNCS if n not in done]
    if remaining:
        ctx.log("编排Agent", f"编排循环结束，自动补齐剩余环节：{remaining}")
        for name in remaining:
            await ctx.checkpoint("编排Agent", f"执行{name}前")
            await AGENT_FUNCS[name](ctx)
            done.add(name)
    ctx.log("编排Agent", "编排结束，转交主流程校验成片")
