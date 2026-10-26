# -*- coding: utf-8 -*-
"""群内玩法：斗图反杀。

不挂人设，是被 @ 且带图时直接接一回合，所以单独放。年鉴、关系网、周榜、
反向图灵测试已经去掉，别再往这里加统计类玩法。
"""
import asyncio
import logging
import random
import time
from typing import Dict

import meme_catalog

# 由 bot.py 注入
bot = None


def bind(host):
    global bot
    bot = host


logger = logging.getLogger("qqbot.fun")

_last_battle: Dict[int, float] = {}

# 斗图回怼的本地语料，不调模型：这种场合要的就是快
_BATTLE_LINES = [
    "图不错，我的了",
    "就这？",
    "存了，下次拿来怼你",
    "你这图有点年份了",
    "接住",
    "翻遍库存找出这张",
    "看把你急的",
]


async def meme_battle(ws, group_id: int, user_id: int, text: str) -> bool:
    """被 @ 并且带了图，就直接甩一张自己的图回去。返回是否接管。"""
    # 有人连着刷图会把群填满，同一群 30 秒只反一次
    now = time.time()
    if now - _last_battle.get(group_id, 0.0) < 30:
        return False
    b64 = meme_catalog.pick_b64(None, text or "")
    if not b64:
        return False
    _last_battle[group_id] = now
    line = random.choice(_BATTLE_LINES)
    await bot.send_group_message(ws, group_id, line, at_user_id=user_id)
    await asyncio.sleep(0.4)
    await bot.send_meme_message(ws, group_id=group_id, b64_data=b64)
    return True
