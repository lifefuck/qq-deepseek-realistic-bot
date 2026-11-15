# -*- coding: utf-8 -*-
"""第一步：先把这段群聊读懂，再决定接不接、往哪个方向接。

借鉴 MaiBot 的 Planner→Replyer 和 Pallas-Bot 的 turn_policy：一次生成同时干
「读懂谁在跟谁说什么」「判断该不该开口」「把话说漂亮」三件事，flash 这种小模型
必然顾此失彼——表现就是接不上梗、答非所问、对着别人的对话点评。

所以拆成两次调用：
    第一次（这里）：只输出一行 JSON，说清谁在说、说的什么事、跟谁说、要不要接、往哪接；
    第二次（services/llm.py）：拿着这个判断去写正文。

代价是多一次请求（实测 0.5~0.8 秒）。收益是正文那一步不用再猜语境。
任何异常都降级成空提示，绝不让它挡住回复主链路。
"""

import aiohttp
import asyncio
import json
import logging
import re
from typing import Optional

logger = logging.getLogger("qqbot.social_read")

# 只读不写的判断题，不需要长思考；温度压到 0.2 保证同一段聊天判断稳定
_TIMEOUT = 8.0
# 别给太小：flash 是带思考的，bot.get_base_chat_payload 会把上限抬到 240，
# 而思考 token 就吃掉 240，正文被截成空字符串（finish_reason=length）。
# 实测同一个判断要烧 200~300 个思考 token，所以关思考那次给 300、兜底给 900。

_SYS = """你在读一段 QQ 群聊，任务是判断最后这句该怎么应对。只输出 JSON，不要解释。

字段：
who   —— 最后这句是谁在说（用昵称）
about —— 他们在聊的具体那件事，一句话，不超过 20 字
to    —— 这句在跟谁说：某个人 / 全群 / 没人（自言自语）
act   —— 该不该由你接话：接 / 不接
how   —— 如果要接，接哪个点（一句话，不超过 20 字）；不接就留空

判断「不接」的情形：他们在聊跟你无关的正事、两个人之间的私聊、已经在收尾的寒暄、
纯表情包、你没有任何具体的东西能补。
判断「接」的情形：这句话留了话口（问句、抛梗、吐槽、感慨、@你、说你）、
或者你有具体的一句能补上去。"""


def _extract_json(text: str) -> Optional[dict]:
    """模型经常裹一层 ```json 或加一句前言，这里只挖第一个 JSON 对象"""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _clean(text, limit: int) -> str:
    s = str(text or "").strip().replace("\n", " ")
    s = re.sub(r"[「」\"'`]", "", s)
    return s[:limit]


async def read(timeline_text: str, latest_line: str, group_id=None) -> Optional[dict]:
    """读一段聊天，返回 {who, about, to, act, how}；读不出来返回 None。"""
    import bot as host  # 延迟导入：模块级 import 会拿到 __main__ 的另一份实例

    if not timeline_text.strip():
        return None

    messages = [
        {"role": "system", "content": _SYS},
        {"role": "user", "content": f"群聊记录：\n{timeline_text}\n\n最后这句：{latest_line or timeline_text.strip().splitlines()[-1]}"},
    ]
    headers = {"Authorization": f"Bearer {host.SILICONFLOW_API_KEY}", "Content-Type": "application/json"}
    raw = ""
    # 两次尝试：先关思考（0.98s 出结果），空了再带思考重来（2.7s）。
    # 这一步只输出一行 JSON，思考纯属浪费；关掉之后判断质量实测没有差别。
    for no_think, budget in ((True, 300), (False, 900)):
        if no_think:
            payload = {
                "model": host.MODEL_NAME, "messages": messages, "temperature": 0.2,
                "max_tokens": budget, "thinking": {"type": "disabled"},
            }
        else:
            payload = host.get_base_chat_payload(
                model=host.MODEL_NAME, messages=messages, max_tokens=budget,
                temperature=0.2, effort="low",
            )
        try:
            async with host.get_http_session().post(
                host.SILICONFLOW_API_URL, headers=headers, json=payload,
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
            ) as resp:
                data = await resp.json()
            raw = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        except Exception as e:
            logger.debug("读懂阶段失败（不影响回复）: %s: %s", type(e).__name__, e)
            return None
        # 正文被思考吃光就加预算再来一次，别让这一步悄悄失效
        if raw.strip():
            break

    parsed = _extract_json(raw)
    if not parsed:
        logger.debug("读懂阶段返回不是 JSON: %s", raw[:80])
        return None
    return {
        "who": _clean(parsed.get("who"), 20),
        "about": _clean(parsed.get("about"), 40),
        "to": _clean(parsed.get("to"), 20),
        "act": _clean(parsed.get("act"), 6),
        "how": _clean(parsed.get("how"), 40),
    }


def wants_pass(result: Optional[dict]) -> bool:
    """读懂阶段是否判定「不该接」。只有明确写「不接」才算，缺字段一律当没说。"""
    if not result:
        return False
    return "不接" in (result.get("act") or "")


def hint_text(result: Optional[dict]) -> str:
    """把判断结果写成给正文那一步看的提示。字段缺失就不写，宁可少说。"""
    if not result:
        return ""
    bits = []
    if result.get("who"):
        bits.append(f"说话的是{result['who']}")
    if result.get("about"):
        bits.append(f"在说「{result['about']}」")
    if result.get("to"):
        bits.append(f"是跟{result['to']}说的")
    head = "，".join(bits)
    how = result.get("how") or ""
    if not head and not how:
        return ""
    body = f"\n【你读到的】：{head}。\n" if head else "\n"
    if how:
        body += f"【往这儿接】：{how}（别把这句话原样说出来）\n"
    return body
