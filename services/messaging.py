# -*- coding: utf-8 -*-
"""群消息发送：切段、限频、剥标签、配表情包。

从 bot.py 搬出来的。全局状态还留在 bot 里，这里通过 bot.xxx 读，
只有一份真相，不搞两套缓存。
"""

import asyncio
import logging
import random
import re
import time
from typing import Optional

import aiohttp

# 由 bot.py 在启动时注入自己。直接 import bot 在 `python bot.py` 下会拿到
# 另一个模块实例（__main__ 与 bot 各一份），全局状态就分裂了。
bot = None


def bind(host):
    global bot
    bot = host
import meme_catalog
import safety_guard

logger = logging.getLogger("qqbot.messaging")


def split_reply_segments(text: str) -> list[str]:
    """智能正则切分回复，生成像真人在 QQ 连续打字发出的 1~3 条短消息"""
    clean = bot.strip_markdown(text).strip()
    clean = bot.remove_trailing_period(clean)
    if not clean:
        return []
    lines = [l.strip() for l in clean.split('\n') if l.strip()]
    if len(lines) > 1:
        return lines[:3]
    if len(clean) <= 16:
        return [clean]
    # 针对中文标点（句号、感叹号、问号、分号）切分
    m = re.split(r'([!！?？~。；\s]{1,3})', clean)
    parts = []
    buf = ''
    for token in m:
        if not token:
            continue
        buf += token
        if re.search(r'[!！?？~。；\s]', token) and len(buf.strip()) >= 5:
            parts.append(buf.strip())
            buf = ''
    if buf.strip():
        parts.append(buf.strip())
    if len(parts) >= 2:
        return parts[:3]
    return [clean]


async def send_group_message(ws: aiohttp.ClientWebSocketResponse, group_id: int, text: str, is_command_report: bool = False, at_user_id: Optional[int] = None):
    """向 NapCat 发送群消息并捕获发送状态（支持可选 @ 对方，并内置群级 60 秒频控保护防 1200 拦截）"""
    if "[PASS]" in text:
        return
    clean_text = bot.strip_markdown(text).strip()
    # 链接混淆防风控
    clean_text = bot.obfuscate_urls_for_qq(clean_text)
    # 过滤末尾句号
    clean_text = bot.remove_trailing_period(clean_text)
    # 普通聊天强控 200 字防水群；管理汇报与 memory 概览解除字数硬截断限制（放宽至 2000 字）
    max_len = 2000 if is_command_report else 200
    if len(clean_text) > max_len:
        clean_text = clean_text[:max_len - 3] + "..."
    if not clean_text:
        return
        
    # 如果指定了目标群友且不是全员指令汇报，前置 [CQ:at,qq=xxx] 艾特对方
    if at_user_id and at_user_id != bot.BOT_QQ and not is_command_report:
        msg_payload = f"[CQ:at,qq={at_user_id}] {clean_text}"
    else:
        msg_payload = clean_text

    async with bot.group_send_locks[group_id]:
        now = time.time()
        # 清理 60 秒之前的发送记录
        bot.group_sent_times[group_id] = [t for t in bot.group_sent_times[group_id] if now - t < 60.0]
        
        # 如果最近 60 秒内已经发了 8 条，进入安全限流等待，避免触发 QQ 风控 (本群每分钟只能发10条)
        if len(bot.group_sent_times[group_id]) >= 8:
            oldest = bot.group_sent_times[group_id][0]
            wait_time = max(0.5, 60.0 - (now - oldest) + 0.5)
            logger.warning("[群 %d] 达到频控安全线 (8条/60s)，主动延时 %.1f 秒再发，防止被 QQ 风控拦截...", group_id, wait_time)
            await asyncio.sleep(wait_time)
            # 唤醒后再次清理
            now = time.time()
            bot.group_sent_times[group_id] = [t for t in bot.group_sent_times[group_id] if now - t < 60.0]

        res = await bot.call_action(ws, "send_group_msg", {"group_id": group_id, "message": msg_payload}, timeout=3.0)
        
        # 如果不幸还是遇到了 299/1200 (每分钟只能发10条)，进行指数退避自动重试
        if res and res.get("status") == "failed" and "每分钟只能发10条" in str(res.get("message", "")):
            logger.warning("[群 %d] 触发 QQ 每分钟 10 条限制！等待 6 秒后自动重试...", group_id)
            await asyncio.sleep(6.0)
            res = await bot.call_action(ws, "send_group_msg", {"group_id": group_id, "message": msg_payload}, timeout=3.0)

        if res and res.get("status") == "ok":
            bot.group_sent_times[group_id].append(time.time())
            logger.info("[群 %d] ✅ 消息成功送达 QQ 群: %s", group_id, msg_payload[:40])
        else:
            logger.warning("[群 %d] ⚠️ 发送消息可能被拦截或异常: %s | 响应: %s", group_id, msg_payload[:40], res)
            # 被动容错熔断：如果响应中明确包含“禁言”或特定错误码，立刻给该群施加 60 秒禁言保护
            res_str = str(res or "").lower()
            if any(k in res_str for k in ["禁言", "banned", "1200"]):
                bot.group_bot_ban_expire[group_id] = time.time() + 60.0
                logger.warning("[群 %d] 检测到发信被禁言拦截，自动开启 60 秒免调用大模型熔断保护！", group_id)


async def send_meme_message(ws: aiohttp.ClientWebSocketResponse, group_id: Optional[int] = None, user_id: Optional[int] = None, b64_data: str = ""):
    """使用 OneBot 11 原生结构化消息发送图片，完全规避字符截断与 CQ 码乱码"""
    if not b64_data:
        return
    msg_payload = [{"type": "image", "data": {"file": f"base64://{b64_data}"}}]
    try:
        if group_id:
            await bot.call_action(ws, "send_group_msg", {"group_id": group_id, "message": msg_payload}, timeout=4.0)
            logger.info("[群 %d] ✅ 专属大肥鱼表情包图片已送达", group_id)
        elif user_id:
            await bot.call_action(ws, "send_private_msg", {"user_id": user_id, "message": msg_payload}, timeout=4.0)
            logger.info("[私聊 %d] ✅ 专属大肥鱼表情包图片已送达", user_id)
    except Exception as e:
        logger.warning("发送表情包失败: %s", e)


async def send_segmented_group_message(ws: aiohttp.ClientWebSocketResponse, group_id: int, text: str, is_command_report: bool = False, at_user_id: Optional[int] = None) -> str:
    """分段拟真发送：闲聊时智能切分 1~2 条连续发送，root 模式整块发送。

    返回实际发出去的正文（已削平、已剥标签），调用方拿它写回上下文，
    别再把没削过的原始回复存进去。
    """
    # 经过反模板闸削平套路，避免句式坍塌
    channel_mode = bot.get_channel_mode(group_id)
    if not is_command_report and channel_mode != "root":
        text = bot.de_template(text)
    # [MEME:xx] 是给选图用的，正文不能带出去，也不能被切段切开
    text, meme_tag = meme_catalog.extract_tag(text)
    if not text and not meme_tag:
        return ""
    bot.recent_bot_replies.append(text)
    bot.group_last_bot_reply_time[group_id] = time.time()

    if is_command_report or len(text) > 120 or channel_mode == "root":
        await send_group_message(ws, group_id, text, is_command_report=is_command_report, at_user_id=at_user_id)
        return text
        
    # 单群限频检查（防腾讯风控）
    if not bot.check_and_record_group_rate_limit(group_id):
        now = time.time()
        last_warn = bot.group_rate_limit_warned.get(group_id, 0.0)
        if now - last_warn > 30.0:
            bot.group_rate_limit_warned[group_id] = now
            logger.warning("[群 %d] 60s 内已发 %d 条，触发风控红线冷静期", group_id, bot.GROUP_MAX_MESSAGES_PER_WINDOW)
            await send_group_message(ws, group_id, "别排队艾特我啦，让本鲸宝喘口气缓一缓", is_command_report=False)
        return ""
        
    # 安全护栏过滤
    is_safe, text = safety_guard.sanitize_outgoing_text(text)
    text = safety_guard.strip_internal_monologue(text)
    if not text:
        return ""

    segments = split_reply_segments(text)
    # 25% 概率或强烈情绪时触发表情包
    should_send_meme = bool(meme_tag) or (random.random() < 0.25) or any(k in text for k in ["大肥鱼", "炸毛", "闹麻了", "草", "无敌了", "才不是", "哼", "气死"])
    # 情绪立绘或表情包选择：如果文本带有强烈情绪，优先匹配状态立绘，否则匹配随机表情包
    status_avatar = None
    if "气" in text or "死" in text or "滚" in text or "闭嘴" in text:
        status_avatar = bot.get_status_avatar_b64("angry")
    elif "害羞" in text or "夸" in text or "爱" in text or "喜欢" in text:
        status_avatar = bot.get_status_avatar_b64("shy")
    elif "为什么" in text or "怎么" in text or "思考" in text:
        status_avatar = bot.get_status_avatar_b64("think")

    final_pic_b64 = status_avatar
    if not final_pic_b64 and should_send_meme:
        # 模型给了情绪就按情绪选，没给就用正文关键词兜底，再不行退回老逻辑
        final_pic_b64 = meme_catalog.pick_b64(meme_tag, text) or bot.get_random_meme_b64(text)

    for i, seg in enumerate(segments):
        target_at = at_user_id if i == 0 else None
        await send_group_message(ws, group_id, seg, is_command_report=False, at_user_id=target_at)
        if i < len(segments) - 1:
            delay = min(1.4, max(0.6, len(seg) * 0.05))
            await asyncio.sleep(delay)
            
    # 文字发完后，如果触发了立绘或表情包，延迟 0.5s 单独甩出一张真实图片
    if final_pic_b64:
        await asyncio.sleep(0.5)
        await send_meme_message(ws, group_id=group_id, b64_data=final_pic_b64)

    return text


async def send_private_message(ws: aiohttp.ClientWebSocketResponse, user_id: int, text: str, is_command_report: bool = False):
    """向 NapCat 发送私聊消息并捕获发送状态"""
    if "[PASS]" in text:
        return
    if not is_command_report and bot.get_channel_mode(None) != "root":
        text = bot.de_template(text)
    bot.recent_bot_replies.append(text)

    clean_text = bot.strip_markdown(text).strip()
    clean_text = bot.obfuscate_urls_for_qq(clean_text)
    # 过滤末尾句号
    clean_text = bot.remove_trailing_period(clean_text)
    max_len = 2000 if is_command_report else 200
    if len(clean_text) > max_len:
        clean_text = clean_text[:max_len - 3] + "..."
    if not clean_text:
        return
        
    res = await bot.call_action(ws, "send_private_msg", {"user_id": user_id, "message": clean_text}, timeout=3.0)
    if res and res.get("status") == "ok":
        logger.info("[私聊 %d] ✅ 私信成功送达: %s", user_id, clean_text[:40])
    else:
        logger.warning("[私聊 %d] ⚠️ 私信发送可能异常: %s | 响应: %s", user_id, clean_text[:40], res)

