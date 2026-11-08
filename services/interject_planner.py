# -*- coding: utf-8 -*-
"""接话时机模型：把「值不值得接」修正成「现在该不该开口」。

质量分只回答前半句。同一句话，在刚说完话的 30 秒里插嘴是抢话，
在冷了一小时的群里突然冒头是吓人。这里叠加四个时机因子：

    刚说过话      ×0.55（别霸屏）
    还在热聊窗口  ×1.15（顺着话头接）
    群里刷得飞快  ×1.10（话题正热）
    上一条没人接  ×0.70（别自说自话追着讲）

全部本地计算，零 token。
"""

COOLDOWN_SEC = 60.0          # 硬冷却：刚开口一分钟内不再插话
HOT_WINDOW_SEC = 30 * 60     # 半小时内说过话算「还在聊」
UNANSWERED_PENALTY = 0.70    # 上一条没人理，降权


def adjust(score: float, *, now: float, last_bot_reply: float,
           msgs_since_bot: int = 0, last_reply_answered: bool = True) -> tuple[float, str]:
    """返回（修正后的分数，原因串）。"""
    score = max(0.0, min(1.0, float(score)))
    reasons = []

    gap = now - last_bot_reply if last_bot_reply else 1e9
    if gap < COOLDOWN_SEC:
        score *= 0.55
        reasons.append("刚说过")
    elif gap < HOT_WINDOW_SEC:
        score *= 1.15
        reasons.append("热聊中")

    if msgs_since_bot >= 6:
        score *= 1.10
        reasons.append("刷屏")
    elif msgs_since_bot <= 1 and gap > 10 * 60:
        score *= 0.85
        reasons.append("冷场")

    if not last_reply_answered:
        score *= UNANSWERED_PENALTY
        reasons.append("上条没人接")

    return max(0.0, min(1.0, score)), "+".join(reasons)


def too_soon(now: float, last_bot_reply: float, cooldown: float = COOLDOWN_SEC) -> bool:
    """硬冷却判定，放在打分之前省一次计算。"""
    return bool(last_bot_reply) and (now - last_bot_reply) < cooldown
