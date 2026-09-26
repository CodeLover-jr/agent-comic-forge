# -*- coding: utf-8 -*-
"""编剧 Agent：把用户输入（剧情/角色/场景/画风）扩写为结构化分镜剧本。

规则：
- 开头 5 秒内必须有冲突钩子，结尾必须留悬念；
- 输出结构化 JSON 数组：[{shot_no, shot_type, description, dialogue, duration}]；
- LLM 不可用时降级为模板剧本，保证流水线不中断。
"""
import logging

import config
from tools import llm

logger = logging.getLogger("screenwriter")

SYSTEM_PROMPT = """你是资深漫剧编剧，擅长把一段剧情梗概扩写成分镜剧本。
输出要求：只输出一个 JSON 数组，不要输出任何解释文字。数组元素格式：
[
  {"shot_no": 1, "shot_type": "远景", "description": "画面内容描述（第三人称客观，供文生图使用，需包含主要角色与场景）", "dialogue": "台词（无台词填空字符串；推荐用“角色名：内容”或“旁白：内容”开头）", "duration": 4}
]
硬性规则：
1. 第 1 个镜头（开头 5 秒内）必须出现冲突钩子：矛盾、异常、悬念、突发事件之一；
2. 最后一个镜头必须留悬念，暗示下一集剧情；
3. 每个镜头 duration 在 3~5 秒之间，总片长控制在 20~50 秒；
4. shot_type 只能从：远景 / 全景 / 中景 / 近景 / 特写 中选择；
5. description 要具体、可画，包含角色外貌动作、场景氛围，但不要出现镜头术语。"""


def build_user_prompt(user_input: dict) -> str:
    chars = "\n".join(
        f"- {c.get('name', '未命名')}：{c.get('gender', '')}/{c.get('age', '')}岁，性格：{c.get('personality', '')}，外貌：{c.get('appearance', '')}"
        for c in user_input.get("characters", [])
    )
    scenes = "\n".join(
        f"- {s.get('name', '未命名场景')}：氛围 {s.get('atmosphere', '')}"
        for s in user_input.get("scenes", [])
    )
    return (
        f"【剧情梗概】\n{user_input.get('plot', '')}\n\n"
        f"【角色设定】\n{chars or '（未提供）'}\n\n"
        f"【场景设定】\n{scenes or '（未提供）'}\n\n"
        f"【画风】{user_input.get('style', '国漫')}\n"
        f"【画幅】{user_input.get('aspect', '9:16')}\n\n"
        "请扩写为完整分镜剧本，严格遵守输出格式与硬性规则。"
    )


def _validate(data) -> list[dict]:
    """校验并规整模型输出的分镜数组。"""
    if not isinstance(data, list):
        raise ValueError("剧本 JSON 不是数组")
    if len(data) < 4:
        raise ValueError("分镜数量过少")
    shots = []
    for i, raw in enumerate(data[: config.MAX_SHOTS], start=1):
        if not isinstance(raw, dict):
            continue
        dur = float(raw.get("duration", 4) or 4)
        dur = max(3.0, min(5.0, dur))
        shots.append({
            "shot_no": i,
            "shot_type": str(raw.get("shot_type", "中景"))[:8],
            "description": str(raw.get("description", ""))[:300],
            "dialogue": str(raw.get("dialogue", ""))[:120],
            "duration": dur,
        })
    if len(shots) < 4:
        raise ValueError("有效分镜不足 4 个")
    return shots


def _fallback_script(user_input: dict) -> list[dict]:
    """LLM 不可用时的模板剧本：包含冲突钩子与结尾悬念，保证流水线可跑通。"""
    chars = user_input.get("characters") or [{"name": "主角"}]
    scenes = user_input.get("scenes") or [{"name": "未知地点"}]
    name = chars[0].get("name", "主角")
    scene = scenes[0].get("name", "未知地点")
    return [
        {"shot_no": 1, "shot_type": "远景",
         "description": f"{scene}全景，气氛压抑，{name}独自站在画面中央，远处传来异响（冲突钩子）",
         "dialogue": "旁白：这是命运开始改变的一天。", "duration": 4},
        {"shot_no": 2, "shot_type": "近景",
         "description": f"{name}猛然回头，表情紧张，瞳孔放大",
         "dialogue": "谁在那里？", "duration": 3},
        {"shot_no": 3, "shot_type": "中景",
         "description": f"{name}在{scene}中奔跑，环境光影快速变换",
         "dialogue": "我得离开这里！", "duration": 4},
        {"shot_no": 4, "shot_type": "特写",
         "description": f"{name}的手伸向一件神秘物品，指尖即将触碰",
         "dialogue": "", "duration": 3},
        {"shot_no": 5, "shot_type": "中景",
         "description": f"神秘物品发出光芒，{name}被光芒笼罩",
         "dialogue": "旁白：秘密，就此揭开。", "duration": 4},
        {"shot_no": 6, "shot_type": "特写",
         "description": f"{name}瞳孔中倒映出异世界景象（结尾悬念）",
         "dialogue": "旁白：这一切，才刚刚开始……", "duration": 4},
    ]


async def generate_script(ctx) -> None:
    """生成剧本并写入 ctx.script。"""
    ctx.log("编剧Agent", "开始扩写剧本：规划分镜数量与节奏结构...")
    ctx.set_progress(10)
    try:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(ctx.user_input)},
        ]
        msg = await llm.chat(messages, temperature=0.8)
        data = llm.extract_json(msg.content or "")
        if isinstance(data, dict) and "shots" in data:
            data = data["shots"]
        ctx.script = _validate(data)
        total = sum(s["duration"] for s in ctx.script)
        ctx.log("编剧Agent", f"剧本完成：{len(ctx.script)} 个镜头，预计总时长 {total:.1f}s，节奏与钩子已规划")
    except Exception as e:  # noqa: BLE001 - LLM 不可用则降级
        logger.warning("编剧Agent 降级为模板剧本: %s", e)
        ctx.script = _fallback_script(ctx.user_input)
        ctx.log("编剧Agent", f"LLM 不可用（{e}），已使用模板剧本：{len(ctx.script)} 个镜头")
