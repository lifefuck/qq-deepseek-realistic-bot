# -*- coding: utf-8 -*-
"""群内玩法：年鉴、关系网、周榜、反向图灵测试、斗图反杀。

这些都不走闲聊人设，是独立的群功能，所以单独一个模块，别塞进 bot.py。
"""
import asyncio
import base64
import json
import logging
import random
import re
import time
from typing import Dict, List, Optional

import aiohttp

import card_render
import group_stats
import meme_catalog

# 由 bot.py 注入
bot = None


def bind(host):
    global bot
    bot = host


logger = logging.getLogger("qqbot.fun")

# 群 -> 进行中的图灵测试
_turing: Dict[int, dict] = {}
_turing_tasks: Dict[int, asyncio.Task] = {}
_last_battle: Dict[int, float] = {}

TURING_ROUND_SECONDS = 120
TURING_MIN_LEN, TURING_MAX_LEN = 8, 30

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


# ==================== 通用 ====================

def _group_name(group_id: int) -> str:
    return bot.group_names_cache.get(group_id) or f"群 {group_id}"


async def _send_image(ws, group_id: int, png_bytes: bytes, text: str = "") -> None:
    if text:
        await bot.send_group_message(ws, group_id, text, is_command_report=True)
    b64 = base64.b64encode(png_bytes).decode()
    await bot.send_meme_message(ws, group_id=group_id, b64_data=b64)


async def _ask_llm(system_prompt: str, user_prompt: str, max_tokens: int = 160,
                   temperature: float = 0.9) -> Optional[str]:
    """轻量直连，不经过闲聊那条链路，避免人设提示词污染。"""
    if bot.http_session is None or bot.http_session.closed:
        bot.http_session = aiohttp.ClientSession()
    payload = {
        "model": bot.MODEL_NAME,
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": user_prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    headers = {"Authorization": f"Bearer {bot.SILICONFLOW_API_KEY}", "Content-Type": "application/json"}
    try:
        async with bot.http_session.post(bot.SILICONFLOW_API_URL, headers=headers, json=payload,
                                         timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                logger.warning("图灵出题请求失败: HTTP %d", resp.status)
                return None
            data = await resp.json()
            if "usage" in data:
                bot.token_tracker.record_usage(data["usage"], bot.MODEL_NAME, group_id=None)
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning("图灵出题异常: %s", e)
        return None


# ==================== 年鉴 / 关系网 / 周榜 ====================

async def send_yearbook(ws, group_id: int) -> None:
    totals = group_stats.group_totals(group_id)
    if totals["msgs"] < 50:
        await bot.send_group_message(ws, group_id, "统计才刚开始，等群里聊够 50 句再来要年鉴", is_command_report=True)
        return
    png = card_render.render_yearbook(
        _group_name(group_id), totals,
        group_stats.top_users(group_id, 5, "msgs"),
        group_stats.top_users(group_id, 3, "night"),
        group_stats.top_users(group_id, 3, "chars"),
    )
    await _send_image(ws, group_id, png, f"📊 {_group_name(group_id)} 的群年鉴，数据攒了 {totals['days']} 天")


async def send_relation_map(ws, group_id: int) -> None:
    pairs = group_stats.top_pairs(group_id, 8)
    if not pairs:
        await bot.send_group_message(ws, group_id, "还没记到谁跟谁说话，先 @ 几个人再来", is_command_report=True)
        return
    weight: Dict[int, int] = {}
    for a, b, cnt in pairs:
        weight[a] = weight.get(a, 0) + cnt
        weight[b] = weight.get(b, 0) + cnt
    nodes = [(uid, group_stats.nick_of(group_id, uid), w)
             for uid, w in sorted(weight.items(), key=lambda x: x[1], reverse=True)[:8]]
    keep = {n[0] for n in nodes}
    edges = [(a, b, c) for a, b, c in pairs if a in keep and b in keep]
    png = card_render.render_relation_map(_group_name(group_id), nodes, edges)
    await _send_image(ws, group_id, png, "🕸 谁老跟谁搭话，一眼就看出来了")


async def send_weekly_board(ws, group_id: int) -> None:
    rows = group_stats.top_users(group_id, 5, "msgs", weekly=True)
    png = card_render.render_weekly(_group_name(group_id), rows, "本周实时")
    await _send_image(ws, group_id, png, "🏆 本周活跃榜")


async def weekly_settle_all(ws=None) -> None:
    """每周一凌晨结算：先把上周榜发出去，再清零，一周没露面的人好感自然回落 1 点。"""
    decayed = 0
    for gid_str, g in list(group_stats._stats.items()):
        try:
            gid = int(gid_str)
        except ValueError:
            continue

        # 结算前先发榜，清零之后就只剩空表了
        if ws is not None:
            rows = group_stats.top_users(gid, 5, "msgs", weekly=True)
            if rows:
                try:
                    png = card_render.render_weekly(_group_name(gid), rows, "上周结算")
                    await _send_image(ws, gid, png, "🏆 上周活跃榜出炉，这周重新开始算")
                except Exception as e:
                    logger.warning("[周结算] 群 %d 发榜失败: %s", gid, e)

        # 一周没说话、且好感高于初始值，才回落
        for uid in group_stats.idle_since(gid, 7 * 86400):
            if uid in (bot.ADMIN_QQ, bot.BOT_QQ):
                continue
            mem = bot.get_or_create_user_memory(uid, "", group_id=gid)
            favor = int(mem.get("favor", 50))
            if favor > 50:
                mem["favor"] = favor - 1
                mem["updated_at"] = time.time()
                decayed += 1
        group_stats.rollover_week(gid)
    if decayed:
        bot.save_user_memories()
    group_stats.save(force=True)
    logger.info("[周结算] 完成，好感回落 %d 人次", decayed)


# ==================== 斗图反杀 ====================

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


# ==================== 反向图灵测试 ====================

def _pick_real_line(group_id: int) -> Optional[str]:
    """从内存时间线里挑一句真人说的、长度合适的短句。机器人自己的话不能混进选项。"""
    pool = [r for r in bot.group_history_pool.get(group_id, [])
            if r.get("sender") != "小鲸鱼"
            and TURING_MIN_LEN <= len(r.get("content", "")) <= TURING_MAX_LEN]
    if not pool:
        return None
    return random.choice(pool[-40:])["content"]


async def start_turing(ws, group_id: int) -> None:
    if group_id in _turing:
        await bot.send_group_message(ws, group_id, "上一题还没揭晓呢，先猜", is_command_report=True)
        return
    real = _pick_real_line(group_id)
    if not real:
        await bot.send_group_message(ws, group_id, "群里还没攒够素材，我说两句之后再来开题", is_command_report=True)
        return

    raw = await _ask_llm(
        "你在帮一个QQ群做图灵测试。只输出两句话，每句一行，不要编号，不要解释。",
        f"群里刚才有人说了这句话：「{real}」\n"
        f"请再编 2 句风格相同、长度相近、另一个人可能会说的话。模仿他们的口语和语气，不要书面语。",
        max_tokens=120,
    )
    fakes = [l.strip("-—· 　") for l in (raw or "").splitlines() if len(l.strip()) >= 4][:2]
    if len(fakes) < 2:
        await bot.send_group_message(ws, group_id, "出题失败了，模型没憋出来，等会儿再试", is_command_report=True)
        return

    options = [real] + fakes
    random.shuffle(options)
    answer = options.index(real) + 1

    _turing[group_id] = {"options": options, "answer": answer, "votes": {}, "start": time.time()}
    lines = [f"{i + 1}. {opt}" for i, opt in enumerate(options)]
    await bot.send_group_message(ws, group_id,
                                 "🎭 反向图灵测试：三句里有一句是真人刚说的，哪一句？\n"
                                 + "\n".join(lines) +
                                 f"\n回数字投票，{TURING_ROUND_SECONDS} 秒后揭晓",
                                 is_command_report=True)
    _turing_tasks[group_id] = asyncio.create_task(_turing_countdown(ws, group_id))


async def _turing_countdown(ws, group_id: int) -> None:
    try:
        await asyncio.sleep(TURING_ROUND_SECONDS)
        await reveal_turing(ws, group_id)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning("图灵揭晓异常: %s", e)


def record_turing_vote(group_id: int, user_id: int, text: str) -> bool:
    state = _turing.get(group_id)
    if not state:
        return False
    m = re.fullmatch(r"\s*([1-3])\s*", text)
    if not m:
        return False
    state["votes"][user_id] = int(m.group(1))
    return True


async def reveal_turing(ws, group_id: int) -> None:
    state = _turing.pop(group_id, None)
    _turing_tasks.pop(group_id, None)
    if not state:
        return
    answer = state["answer"]
    options = state["options"]
    votes = state["votes"]
    right = [uid for uid, v in votes.items() if v == answer]
    wrong = len(votes) - len(right)

    head = f"🎭 揭晓：真话是【{answer}】{options[answer - 1]}"
    if not votes:
        tail = "一个人都没投，这题白问了"
    elif right:
        tail = f"猜对 {len(right)} 人，猜错 {wrong} 人。猜对的有点东西"
    else:
        tail = f"{wrong} 个人全军覆没，AI 装群友装得还挺像"
    await bot.send_group_message(ws, group_id, f"{head}\n{tail}", is_command_report=True)

    # 猜对的加点好感，算是给参与感一点反馈
    for uid in right[:5]:
        mem = bot.get_or_create_user_memory(uid, "", group_id=group_id)
        if uid != bot.ADMIN_QQ:
            mem["favor"] = min(100, int(mem.get("favor", 50)) + 1)
            mem["updated_at"] = time.time()
    if right:
        bot.save_user_memories()


# ==================== AI 自主发起玩法 ====================

# 群 -> 上次自主发起时间。这玩意不能太勤，不然群里天天被年鉴糊脸
_last_self_play: Dict[int, float] = {}
SELF_PLAY_INTERVAL = 5400.0      # 两小时最多一次
SELF_PLAY_CHANCE = 0.06          # 冷场接话时 6% 概率改成开个玩法


async def maybe_self_play(ws, group_id: int) -> bool:
    """冷场时替 AI 决定要不要自己开个玩法。返回 True 表示这次接话被玩法顶掉了。"""
    cfg = bot.get_group_config(group_id)
    if not cfg.get("enable_fun", True):
        return False
    if bot.is_bot_sleeping(group_id):
        return False
    now = time.time()
    if now - _last_self_play.get(group_id, 0.0) < SELF_PLAY_INTERVAL:
        return False
    if random.random() > SELF_PLAY_CHANCE:
        return False

    totals = group_stats.group_totals(group_id)
    options = []
    if totals["msgs"] >= 300:
        options.append("yearbook")
    if len(group_stats.top_pairs(group_id, 6)) >= 3:
        options.append("relation")
    if totals["msgs"] >= 120:
        options.append("weekly")
    if _pick_real_line(group_id) and group_id not in _turing:
        options.append("turing")
    if not options:
        return False

    action = random.choice(options)
    _last_self_play[group_id] = now
    logger.info("[群 %d] 冷场，自己开一局玩法: %s", group_id, action)
    try:
        if action == "yearbook":
            await send_yearbook(ws, group_id)
        elif action == "relation":
            await send_relation_map(ws, group_id)
        elif action == "weekly":
            await send_weekly_board(ws, group_id)
        elif action == "turing":
            await start_turing(ws, group_id)
        return True
    except Exception as e:
        logger.warning("[群 %d] 自主玩法失败: %s", group_id, e)
        return False


# ==================== 命令入口 ====================

_FUN_CMDS = {
    "/年鉴": "yearbook", "/yearbook": "yearbook", "群年鉴": "yearbook",
    "/关系网": "relation", "/relations": "relation", "关系网": "relation",
    "/周榜": "weekly", "/weekly": "weekly", "本周榜": "weekly",
    "/图灵": "turing", "/turing": "turing",
}


async def handle_fun_command(ws, group_id: int, user_id: int, text: str) -> bool:
    key = text.strip().lower().replace("／", "/")
    action = _FUN_CMDS.get(key)
    if not action:
        return False
    if action == "yearbook":
        await send_yearbook(ws, group_id)
    elif action == "relation":
        await send_relation_map(ws, group_id)
    elif action == "weekly":
        await send_weekly_board(ws, group_id)
    elif action == "turing":
        await start_turing(ws, group_id)
    logger.info("[群 %d] 用户 %d 触发玩法: %s", group_id, user_id, action)
    return True
