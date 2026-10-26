#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
群聊多人群友互动关系网轻量记忆与追踪 (Cross Interaction Tracker)
功能:
1. 记录群内高频互动的群友对（如 A 经常艾特 B 或回复 B）
2. 在上下文 Prompt 中按需注入简要关系提示，防认错人、自然吃瓜
作者: life
"""

import collections
import time
from typing import Dict, List, Optional, Tuple

# 维护群内最近活跃的群友互动对: group_id -> deque of (user_a, nick_a, user_b, nick_b, ts)
MAX_RELATION_RECORDS = 20
group_interactions: Dict[int, collections.deque] = collections.defaultdict(lambda: collections.deque(maxlen=MAX_RELATION_RECORDS))


def record_interaction(group_id: int, sender_id: int, sender_nick: str, target_id: int, target_nick: str):
    """记录群友之间的定向互动事件（@ 或 引用回复）"""
    if not group_id or not sender_id or not target_id or sender_id == target_id:
        return
    now = time.time()
    group_interactions[group_id].append({
        "from_id": sender_id,
        "from_nick": sender_nick or str(sender_id),
        "to_id": target_id,
        "to_nick": target_nick or str(target_id),
        "ts": now
    })


def get_group_relations_summary(group_id: int, current_user_id: Optional[int] = None, max_pairs: int = 3) -> str:
    """
    提取该群最近 15 分钟内最活跃的互动关系，返回 Prompt 辅助文本
    """
    records = group_interactions.get(group_id)
    if not records:
        return ""

    now = time.time()
    recent = [r for r in records if now - r["ts"] < 900.0]
    if not recent:
        return ""

    # 统计 (a, b) 互动的频次
    pair_counts = collections.defaultdict(int)
    nick_map = {}
    for r in recent:
        pair_key = tuple(sorted([r["from_id"], r["to_id"]]))
        pair_counts[pair_key] += 1
        nick_map[r["from_id"]] = r["from_nick"]
        nick_map[r["to_id"]] = r["to_nick"]

    sorted_pairs = sorted(pair_counts.items(), key=lambda x: x[1], reverse=True)[:max_pairs]
    if not sorted_pairs:
        return ""

    snippets = []
    for (u1, u2), cnt in sorted_pairs:
        n1 = nick_map.get(u1, str(u1))
        n2 = nick_map.get(u2, str(u2))
        snippets.append(f"「{n1}」与「{n2}」最近互动较多({cnt}次)")

    return "【群内当前焦点关系】：\n- " + "；".join(snippets) + "\n"
