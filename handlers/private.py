# -*- coding: utf-8 -*-
"""管理员私聊：普通聊天、指令、记忆查询。
"""

import aiohttp
import asyncio
import meme_catalog
import re
import time
from services import trace
from services import vision
from services import affect
import media_resolver

# 由 bot.py 在启动时注入自己。直接 import bot 在 `python bot.py` 下会拿到
# 另一个模块实例（__main__ 与 bot 各一份），全局状态就分裂了。
bot = None


def bind(host):
    global bot
    bot = host


def _apply_favor_everywhere(target_qq: int, *, new_value: int = None, delta: int = None) -> tuple[int, int]:
    """好感度写进这个人在所有群的分区，并返回 (改前值, 影响分区数)。

    以前只写 user_memories["private"]，群回复读的是群号桶，私聊改完等于没改。
    """
    uid = str(target_qq)
    touched = 0
    old_val = 50
    for scope_key, scope in bot.user_memories.items():
        mem = scope.get(uid)
        if not isinstance(mem, dict):
            continue
        try:
            old_val = int(mem.get("favor", 50))
        except (TypeError, ValueError):
            old_val = 50
        if new_value is not None:
            mem["favor"] = max(0, min(100, new_value))
        else:
            mem["favor"] = max(0, min(100, old_val + (delta or 0)))
        mem["updated_at"] = time.time()
        touched += 1
    if not touched:
        mem = bot.get_or_create_user_memory(target_qq, group_id=None)
        old_val = int(mem.get("favor", 50))
        mem["favor"] = max(0, min(100, new_value if new_value is not None else old_val + (delta or 0)))
        mem["updated_at"] = time.time()
        touched = 1
    bot.save_user_memories()
    return old_val, touched


# 私聊同一时间只跑一轮生成：连发两句时不会互相看不到对方、各回各的
_PRIVATE_GEN_LOCK = asyncio.Lock()


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
    
    # 捕获私聊中的图片，若有图片则通过 Gemini 转录画面内容
    m_imgs = re.findall(r'\[CQ:image,[^\]]*?url=([^,\]]+)', raw_message)
    vision_desc = None
    session = bot.get_http_session()
    if m_imgs:
        target_img_url = m_imgs[0].replace("&amp;", "&")
        bot.logger.info("[私聊] 收到图片，正在调用 Gemini 识图: %s", target_img_url[:60])
        vision_desc = await vision.describe_image(session, target_img_url)
        if vision_desc:
            bot.logger.info("[私聊] Gemini 识图成功: %s", vision_desc[:60])

    # 媒体与通用网页链接解析
    media_info = None
    if session:
        try:
            media_info = await media_resolver.parse_media_content(session, raw_message)
            if media_info and media_info.get("summary"):
                await bot.send_private_message(ws, sender_id, media_info["summary"].strip())
        except Exception as e:
            bot.logger.warning("[私聊] 链接解析/发送异常: %s", e)

    clean_body = bot.clean_cq_codes(raw_message).strip()
    clean_body = re.sub(r"\[CQ:reply,id=-?\d+\]", "", clean_body).strip()
    # 私聊也走同一套心境，key 用 private，不占群的账
    try:
        affect.on_group_message("private", clean_body)
    except Exception:
        pass

    if media_info:
        clean_body = f"{clean_body}\n{media_info['summary']}".strip()
    
    if vision_desc:
        clean_body = f"{clean_body}\n[用户发送/附带的图片/表情包内容]: {vision_desc}".strip()
    
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

    # 管理员私聊：睡眠期不设硬叫醒，保持自然互动
    if bot.is_bot_sleeping():
        bot.logger.info("[作息系统] 主人发起私聊，当前处于夜间作息睡眠期（自然响应，无需强制叫醒）")

    # 管理员指令拦截
    if (
        norm_lower.startswith(("/new", "/stop", "/on", "/off", "/root", "/user"))
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

    # 管理员私聊直接下达生产命令拦截（随时可管理任意群友好感度与记忆）
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
        m_delta = re.search(r"(?:把|给|针对|对)?\s*(?:用户|群友)?\s*((?<!\d)\d{5,11}(?!\d))\s*(?:的)?\s*(?:好感度|好感|favor)?\s*(加|增加|提升|涨|上升|\+|减|扣除|扣|降低|跌|下降|\-)\s*(\d+)", clean_body, re.IGNORECASE)
        if not m_delta:
            m_delta = re.search(r"(加|增加|提升|涨|上升|\+|减|扣除|扣|降低|跌|下降|\-)\s*(?:用户|群友)?\s*((?<!\d)\d{5,11}(?!\d))\s*(?:的)?\s*(?:好感度|好感|favor)?\s*(\d+)", clean_body, re.IGNORECASE)
            if m_delta:
                op_sign = m_delta.group(1)
                target_qq = int(m_delta.group(2))
                delta_val = int(m_delta.group(3))
            else:
                m_delta = re.search(r"(?:好感度|好感|favor)?\s*(加|增加|提升|涨|上升|\+|减|扣除|扣|降低|跌|下降|\-)\s*(\d+)\s*(?:给|针对|对)?\s*(?:用户|群友)?\s*((?<!\d)\d{5,11}(?!\d))", clean_body, re.IGNORECASE)
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
            old_f, scope_n = _apply_favor_everywhere(target_qq, delta=actual_delta)
            new_f = max(0, min(100, old_f + actual_delta))
            action_desc = f"扣除 {delta_val}" if is_sub else f"增加 {delta_val}"
            report = f"已为 (QQ {target_qq}) {action_desc} 点好感度: {old_f} -> {new_f}/100（影响 {scope_n} 个群）"
            await bot.send_private_message(ws, bot.ADMIN_QQ, report, is_command_report=True)
            return

        # 3. 绝对值修改某人好感度
        m_favor = re.search(r"(?:把|将|改|设置)?\s*(?:用户|群友)?\s*((?<!\d)\d{5,11}(?!\d))\s*(?:的)?\s*(?:好感度|favor)?\s*(?:设为|改为|变更为|调至|调成|调为|调节为|设置为|为|\=|\s+)\s*(\d+)", clean_body, re.IGNORECASE)
        if not m_favor:
            m_favor = re.search(r"(?:好感度|favor)\s*(?:设为|改为|变更为|调至|调成|调为|调节为|设置为|为|\=|\s+)\s*(\d+)\s*(?:给|针对|对)?\s*(?:用户|群友)?\s*((?<!\d)\d{5,11}(?!\d))", clean_body, re.IGNORECASE)
            if m_favor:
                new_favor = max(0, min(100, int(m_favor.group(1))))
                target_qq = int(m_favor.group(2))
                _, scope_n = _apply_favor_everywhere(target_qq, new_value=new_favor)
                report = f"已将 (QQ {target_qq}) 的好感度设为 {new_favor}/100（影响 {scope_n} 个群）"
                await bot.send_private_message(ws, bot.ADMIN_QQ, report, is_command_report=True)
                return
        if m_favor:
            target_qq = int(m_favor.group(1))
            new_favor = max(0, min(100, int(m_favor.group(2))))
            _, scope_n = _apply_favor_everywhere(target_qq, new_value=new_favor)
            report = f"已将 (QQ {target_qq}) 的好感度设为 {new_favor}/100（影响 {scope_n} 个群）"
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

    sleep_instruction = ""
    if bot.is_bot_sleeping():
        sleep_instruction = (
            "【当前为夜间作息睡眠时段】：\n"
            "你正处于半夜被主人私聊叫醒的状态，整个人很困、睡意朦胧。\n"
            "要求：状态是困、反应慢、句子短，像被吵醒的人随口应一句；别写括号动作，也别套固定拟声词。"
        )

    try:
        async with _PRIVATE_GEN_LOCK:
            reply = await bot.call_deepseek(timeline_str, is_at=True, is_admin=True, is_private=True, extra_system_instruction=sleep_instruction)
    finally:
        if current_task:
            bot.running_tasks.discard(current_task)
            
    if not reply:
        return
        
    # 私聊只发正文，内部标记剥掉，免得主人收到一句 [MEME:卖萌]
    reply, _ = meme_catalog.extract_tag(reply)
    if not reply:
        return
    # 撤回的私聊消息不再回复（任务多半已被 cancel，这里是第二道闸）
    if bot.was_recalled(event.get("message_id")):
        bot.logger.info("[私聊] 触发消息已撤回，丢弃回复")
        return
    sent = await bot.send_private_message(ws, bot.ADMIN_QQ, reply, is_command_report=False)
    if sent:
        bot.admin_private_timeline.append(f"小鲸鱼: {reply}")
        bot.admin_private_history.append(f"小鲸鱼: {reply}")

