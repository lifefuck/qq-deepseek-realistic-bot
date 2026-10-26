# -*- coding: utf-8 -*-
"""管理员私聊：普通聊天、指令、记忆查询。
"""

import aiohttp
import asyncio
import meme_catalog
import re
import time
from services import trace

# 由 bot.py 在启动时注入自己。直接 import bot 在 `python bot.py` 下会拿到
# 另一个模块实例（__main__ 与 bot 各一份），全局状态就分裂了。
bot = None


def bind(host):
    global bot
    bot = host


async def handle_private_message(ws: aiohttp.ClientWebSocketResponse, event: dict):
    """管理员私聊流水线"""
    sender_id = event.get("user_id")
    raw_message = event.get("raw_message", "")
    
    if sender_id != bot.ADMIN_QQ:
        return

    # 私聊同样打 rid，和群聊一个格式，日志里好区分来源
    trace.begin(f"p{sender_id}")
        
    reply_prefix = await bot.extract_reply_context(ws, event, raw_message)
    raw_message = await bot.expand_forward_messages(ws, raw_message)
    
    clean_body = bot.clean_cq_codes(raw_message).strip()
    clean_body = re.sub(r"\[CQ:reply,id=-?\d+\]", "", clean_body).strip()
    
    if not clean_body:
        return
        
    # 游戏指令或云崽专属指令私聊拦截
    if bot.is_game_or_yunzai_command(clean_body):
        bot.logger.debug("[私聊] 识别为游戏/Cookie指令: %r，交由云崽处理，跳过 AI 回复", clean_body)
        return

    # 规范化指令前缀与小写，兼容全角斜杠与输入法大小写
    norm_cmd = clean_body.replace("／", "/").strip()
    norm_lower = norm_cmd.lower()

    # 公开指令检测: /memory 或 /memary（私聊支持查看）
    if norm_lower.startswith(("/memary", "/memory")):
        await bot.handle_memory_summary_command(ws, user_id=bot.ADMIN_QQ)
        return

    # 管理员私聊直接自动唤醒睡眠状态 30 分钟
    if bot.is_bot_sleeping():
        bot.sleep_override_until = time.time() + 1800.0
        bot.logger.info("[作息系统] 主人发起私聊，大肥鱼自动解除睡眠状态 30 分钟！")

    # 管理员指令拦截
    if (
        norm_lower.startswith(("/new", "/stop", "/root", "/user", "/on", "/off"))
        or norm_lower in ("stop", "new", "闭嘴", "停", "暂停")
    ):
        target_cmd = norm_cmd
        if norm_lower in ("stop", "闭嘴", "停", "暂停"):
            target_cmd = "/stop"
        elif norm_lower == "new":
            target_cmd = "/new"
        handled = await bot.handle_admin_command(ws, target_cmd, is_private=True)
        if handled:
            return

    # 管理员私聊直接下达生产命令拦截 (无需切 root 模式，随时可管理任意群友好感度与记忆)
    if True:
        # 1. 清除某人记忆
        m_clear = re.search(r"(?:清除|删掉|清空|重置)\s*(?:用户|群友)?\s*(\d{5,11})\s*(?:的)?\s*(?:memory|memary|记忆|档案)?", clean_body, re.IGNORECASE)
        if not m_clear:
            m_clear = re.search(r"(?:memory|memary|记忆|档案)?\s*(?:清除|删掉|清空|重置)\s*(?:用户|群友)?\s*(\d{5,11})", clean_body, re.IGNORECASE)
        if m_clear:
            target_qq = int(m_clear.group(1))
            cleared = bot.clear_single_user_memory(target_qq, group_id=None)
            if cleared:
                report = f"已清除群友 (QQ {target_qq}) 的全部记忆档案"
            else:
                report = f"记忆库中暂无 (QQ {target_qq}) 的记录"
            await bot.send_private_message(ws, bot.ADMIN_QQ, report, is_command_report=True)
            return

        # 2. 增减某人好感度: 给 123456 加 10 点好感度 / 扣除 123456 20 好感度 / 把 123456 好感度 +5 / 扣 123456 好感 10
        m_delta = re.search(r"(?:把|给|针对|对)?\s*(?:用户|群友)?\s*(\b\d{5,11}\b)\s*(?:的)?\s*(?:好感度|好感|favor)?\s*(加|增加|提升|涨|上升|\+|减|扣除|扣|降低|跌|下降|\-)\s*(\d+)", clean_body, re.IGNORECASE)
        if not m_delta:
            m_delta = re.search(r"(加|增加|提升|涨|上升|\+|减|扣除|扣|降低|跌|下降|\-)\s*(?:用户|群友)?\s*(\b\d{5,11}\b)\s*(?:的)?\s*(?:好感度|好感|favor)?\s*(\d+)", clean_body, re.IGNORECASE)
            if m_delta:
                op_sign = m_delta.group(1)
                target_qq = int(m_delta.group(2))
                delta_val = int(m_delta.group(3))
            else:
                m_delta = re.search(r"(?:好感度|好感|favor)?\s*(加|增加|提升|涨|上升|\+|减|扣除|扣|降低|跌|下降|\-)\s*(\d+)\s*(?:给|针对|对)?\s*(?:用户|群友)?\s*(\b\d{5,11}\b)", clean_body, re.IGNORECASE)
                if m_delta:
                    op_sign = m_delta.group(1)
                    delta_val = int(m_delta.group(2))
                    target_qq = int(m_delta.group(3))
        else:
            target_qq = int(m_delta.group(1))
            op_sign = m_delta.group(2)
            delta_val = int(m_delta.group(3))

        if m_delta:
            is_sub = any(s in op_sign for s in ["减", "扣", "降低", "跌", "下降", "-"])
            actual_delta = -delta_val if is_sub else delta_val
            mem = bot.get_or_create_user_memory(target_qq, group_id=None)
            old_f = mem.get("favor", 50)
            new_f = max(0, min(100, old_f + actual_delta))
            mem["favor"] = new_f
            mem["updated_at"] = time.time()
            bot.save_user_memories()
            action_desc = f"扣除 {delta_val}" if is_sub else f"增加 {delta_val}"
            report = f"已为 (QQ {target_qq}) {action_desc} 点好感度，当前好感度: {old_f} -> {new_f}/100"
            await bot.send_private_message(ws, bot.ADMIN_QQ, report, is_command_report=True)
            return

        # 3. 绝对值修改某人好感度
        m_favor = re.search(r"(?:把|将|改|设置)?\s*(?:用户|群友)?\s*(\b\d{5,11}\b)\s*(?:的)?\s*(?:好感度|favor)?\s*(?:设为|改为|变更为|调至|调成|调为|调节为|设置为|为|\=|\s+)\s*(\d+)", clean_body, re.IGNORECASE)
        if not m_favor:
            m_favor = re.search(r"(?:好感度|favor)\s*(?:设为|改为|变更为|调至|调成|调为|调节为|设置为|为|\=|\s+)\s*(\d+)\s*(?:给|针对|对)?\s*(?:用户|群友)?\s*(\b\d{5,11}\b)", clean_body, re.IGNORECASE)
            if m_favor:
                new_favor = max(0, min(100, int(m_favor.group(1))))
                target_qq = int(m_favor.group(2))
                mem = bot.get_or_create_user_memory(target_qq, group_id=None)
                mem["favor"] = new_favor
                mem["updated_at"] = time.time()
                bot.save_user_memories()
                report = f"已将 (QQ {target_qq}) 的好感度设为 {new_favor}/100"
                await bot.send_private_message(ws, bot.ADMIN_QQ, report, is_command_report=True)
                return
        if m_favor:
            target_qq = int(m_favor.group(1))
            new_favor = max(0, min(100, int(m_favor.group(2))))
            mem = bot.get_or_create_user_memory(target_qq, group_id=None)
            mem["favor"] = new_favor
            mem["updated_at"] = time.time()
            bot.save_user_memories()
            report = f"已将 (QQ {target_qq}) 的好感度设为 {new_favor}/100"
            await bot.send_private_message(ws, bot.ADMIN_QQ, report, is_command_report=True)
            return

    full_content = f"{reply_prefix} {clean_body}" if reply_prefix else clean_body
    bot.logger.info("[私聊] 主人 life 来信: %s", full_content)
    bot.admin_private_timeline.append(f"life: {full_content}")
    bot.admin_private_history.append(f"life: {full_content}")
    
    timeline_str = "\n".join(bot.admin_private_timeline)
    current_task = asyncio.current_task()
    if current_task:
        bot.running_tasks.add(current_task)
    try:
        reply = await bot.call_deepseek(timeline_str, is_at=True, is_admin=True, is_private=True)
    finally:
        if current_task:
            bot.running_tasks.discard(current_task)
            
    if not reply:
        return
        
    # 私聊只发正文，内部标记剥掉，免得主人收到一句 [MEME:卖萌]
    reply, _ = meme_catalog.extract_tag(reply)
    if not reply:
        return
    bot.admin_private_timeline.append(f"小鲸鱼: {reply}")
    bot.admin_private_history.append(f"小鲸鱼: {reply}")
    await asyncio.sleep(0.8)
    await bot.send_private_message(ws, bot.ADMIN_QQ, reply, is_command_report=(bot.get_channel_mode(None) == "root"))

