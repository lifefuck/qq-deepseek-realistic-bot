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

from services import affect
from services import pacing
from services import typo

# 由 bot.py 在启动时注入自己。直接 import bot 在 `python bot.py` 下会拿到
# 另一个模块实例（__main__ 与 bot 各一份），全局状态就分裂了。
bot = None


def bind(host):
    global bot
    bot = host
    host.send_input_status = send_input_status
import meme_catalog
import safety_guard

logger = logging.getLogger("qqbot.messaging")

# 黄脸、手势、杂项符号 + 变体选择符/零宽连接符
_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]+")


def _tidy(text: str) -> str:
    """摘完表情后收拾残留空格，别留下「说了 句话」这种断口"""
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return re.sub(r"\s+([，。！？、])", r"\1", text)


# 语气词后面原本该是停顿，不该是空格
_PARTICLE_RE = re.compile(r"([啊呀呢吧嘛哦诶欸哈咯啦喔唉诶])\s+")
# 两个汉字之间的裸空格，基本就是把逗号换没了
_CJK_GAP_RE = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")


def normalize_punctuation(text: str) -> str:
    """把"空格当逗号"改回中文标点。

    以前这里叫 desensitize_commas，把逗号全换成空格去"去人机感"，结果整条
    消息变成一串用空格断开的短句，读起来像机器在报单词。真人打字是有停顿的，
    停顿就该是逗号，不是空格。
    """
    if not text:
        return text
    t = re.sub(r"[ \t]{2,}", " ", text)
    t = _PARTICLE_RE.sub(r"\1，", t)      # 「好啊 那就这样」→「好啊，那就这样」
    t = _CJK_GAP_RE.sub("，", t)           # 「显卡没用 认命吧」→「显卡没用，认命吧」
    t = re.sub(r"，{2,}", "，", t)          # 别叠出「，，」
    t = t.replace(" ，", "，").replace("， ", "，")
    return t.strip(" ，")


def thin_emoji(text: str, keep_ratio: float = 0.15) -> str:
    """表情抽稀：模型看到"可以带表情"就爱每条都甩，光靠 prompt 压不住。
    这里按概率只放行一部分，放行的那条也只留一个，整体才是「偶尔」而不是标配。
    默认 0.15——群里连发十几条才该见到一两个，多了就成客服表情包了。"""
    matches = list(_EMOJI_RE.finditer(text))
    if not matches:
        return text
    if random.random() >= keep_ratio:
        cleaned = _tidy(_EMOJI_RE.sub("", text))
        # 整条都是表情的话，摘完就空了，那就留着，总比发个空消息强
        return cleaned or text
    first = matches[0]
    rest = _EMOJI_RE.sub("", text[first.end():])
    return _tidy(text[:first.start()] + first.group() + rest)


_SEG_END_RE = re.compile(r'[!！?？~。；…\n]{1,3}')


def _merge_fragments(parts: list[str]) -> list[str]:
    """把纯表情、两三个字的碎片并回相邻那句。
    否则「清蒸？你这是要把我炖了？」后面会单独飞出去一个 😤，看着像手滑。"""
    out: list[str] = []
    for p in parts:
        bare = _EMOJI_RE.sub("", p).strip(" ，,、")
        if out and (not bare or len(bare) < 3):
            out[-1] = out[-1] + " " + p
        else:
            out.append(p)
    return [p for p in out if p.strip()]


def _split_long_clause(s: str, min_len: int = 34) -> Optional[list[str]]:
    """整条只憋出一长句时，在最靠中间的逗号处断成两条。

    真人遇到长句是打完半句先发出去，不是一口气顶一大段；只在没有强标点可切时兜底。
    """
    if len(s) <= min_len:
        return None
    cuts = [m.start() for m in re.finditer("，", s)]
    best = None
    best_gap = None
    mid = len(s) / 2
    for c in cuts:
        left, right = s[:c].strip(), s[c + 1:].strip()
        if len(left) < 6 or len(right) < 6:
            continue
        gap = abs(c - mid)
        if best_gap is None or gap < best_gap:
            best, best_gap = [left, right], gap
    return best


_MAX_BUBBLE_CHARS = 10  # 单条气泡的目标长度：真人聊天很少一口气超过十个字


def shape_short(segments: list[str], limit: int = 4) -> list[str]:
    """把「一句话塞三个逗号」的长句切成几条短气泡。

    真人打字是一段一段蹦出来的：先说「刚吃完饭」，再补「去买了杯咖啡」。
    模型爱写「刚吃完饭，然后去楼下买了杯咖啡，顺手把快递也拿了」这种一整条，
    切段器只在句末标点处下刀，切完仍是十几个字一条，所以这里补一刀按逗号断。
    只在逗号处切，不切词、不切数字；短句原样放过，免得把「就这」拆散。
    """
    out: list[str] = []
    for seg in segments:
        seg = (seg or "").strip()
        if not seg:
            continue
        if len(seg) <= _MAX_BUBBLE_CHARS or not re.search(r"[，,、]", seg):
            out.append(seg)
            continue
        buf = ""
        for part in re.split(r"[，,、]", seg):
            part = part.strip()
            if not part:
                continue
            if not buf:
                buf = part
            elif len(buf) + len(part) + 1 <= _MAX_BUBBLE_CHARS:
                buf = f"{buf}，{part}"
            else:
                out.append(buf)
                buf = part
        if buf:
            out.append(buf)
    if len(out) > limit:
        out = out[: limit - 1] + ["，".join(out[limit - 1:])]
    return [s.strip(" ，,、") for s in out if s.strip(" ，,、")]


def split_reply_segments(text: str) -> list[str]:
    """按强标点（！？”。~）、换行切成 1~3 条短消息，像真人连续打字。
    不拿空格当分隔符，也不把中英混排切断。若回复包含换行则优先按行分段发。"""
    clean = bot.strip_markdown(text).strip()
    clean = bot.remove_trailing_period(clean)
    if not clean:
        return []
    lines = [l.strip() for l in clean.split('\n') if l.strip()]
    if len(lines) > 1:
        return lines[:3]
    if len(clean) <= 16:
        return [clean]

    parts = []
    buf = ''
    pos = 0
    for m in _SEG_END_RE.finditer(clean):
        buf += clean[pos:m.end()]
        pos = m.end()
        if len(buf.strip()) >= 5:
            parts.append(buf.strip())
            buf = ''
    if clean[pos:].strip():
        buf += clean[pos:]
    if buf.strip():
        parts.append(buf.strip())
    parts = _merge_fragments(parts)
    if len(parts) >= 2:
        return parts[:3]
    longer = _split_long_clause(clean)
    if longer:
        return longer
    return [clean]


async def send_input_status(ws: aiohttp.ClientWebSocketResponse, group_id: int):
    """向 NapCat 发送群聊「正在输入中」状态（status_type=1），营造拟真打字体感"""
    if not ws or getattr(ws, "closed", False):
        return
    try:
        # OneBot 11 / NapCat API: set_input_status (status_type: 1 为正在输入)
        await bot.call_action(ws, "set_input_status", {"group_id": group_id, "status_type": 1}, timeout=1.5)
    except Exception as e:
        logger.debug("[群 %d] 设置输入中状态异常: %s", group_id, e)


async def send_group_message(ws: aiohttp.ClientWebSocketResponse, group_id: int, text: str, is_command_report: bool = False, at_user_id: Optional[int] = None, emoji_ratio: float = 0.15):
    """向 NapCat 发送群消息并捕获发送状态（支持可选 @ 对方，并内置群级 60 秒频控保护防 1200 拦截）"""
    if "[PASS]" in text:
        return
    clean_text = bot.strip_markdown(text).strip()
    # 兜底：单发通道绝不让裸换行流到 QQ。聊天内容的换行并成逗号，
    # 只有汇报/面板才保留换行（那种换行是有结构的）。
    clean_text = clean_text.replace("\r", "")
    if not is_command_report and "\n" in clean_text:
        clean_text = re.sub(r"\s*\n+\s*", "，", clean_text)
    # 链接混淆防风控
    clean_text = bot.obfuscate_urls_for_qq(clean_text)
    # 过滤末尾句号
    clean_text = bot.remove_trailing_period(clean_text)
    # 普通聊天里的表情抽稀与真人随手打字质感注入，指令汇报/卡片不掺和
    if not is_command_report:
        clean_text = thin_emoji(clean_text, keep_ratio=emoji_ratio)
        clean_text = normalize_punctuation(clean_text)
        # 错字在分段发送那一层做：只有那里知道这条有没有被改、改的是哪个词，
        # 才能决定要不要补一句「打错了」。这里再注入一次会变成双重错字。

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
        res = await bot.call_action(ws, "send_group_msg", {"group_id": group_id, "message": msg_payload}, timeout=3.0)
        
        if res and res.get("status") == "ok":
            logger.info("[群 %d] 消息已送达: %s", group_id, msg_payload[:40])
        else:
            logger.warning("[群 %d] 发送可能失败: %s | 响应: %s", group_id, msg_payload[:40], res)
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
            logger.info("[群 %d] 表情包已发送", group_id)
        elif user_id:
            await bot.call_action(ws, "send_private_msg", {"user_id": user_id, "message": msg_payload}, timeout=4.0)
            logger.info("[私聊 %d] 表情包已发送", user_id)
    except Exception as e:
        logger.warning("发送表情包失败: %s", e)


async def send_forward_message(ws: aiohttp.ClientWebSocketResponse, messages: list, group_id: Optional[int] = None, user_id: Optional[int] = None) -> bool:
    """发合并转发（聊天记录折叠）。

    messages 是 node 列表，每个 node 形如
    {"type": "node", "data": {"user_id": "123", "nickname": "小明", "content": "一句话"}}。
    群和私聊用的是两个不同的 action，别混着发。
    """
    if not messages:
        return False
    if group_id:
        action, params = "send_group_forward_msg", {"group_id": group_id, "messages": messages}
    elif user_id:
        action, params = "send_private_forward_msg", {"user_id": user_id, "messages": messages}
    else:
        return False
    try:
        res = await bot.call_action(ws, action, params, timeout=6.0)
        if res and res.get("status") == "ok":
            logger.info("[%s] 合并转发已发送（%d 条）", group_id or user_id, len(messages))
            return True
        logger.warning("[%s] 合并转发失败，响应: %s", group_id or user_id, res)
        return False
    except Exception as e:
        logger.warning("合并转发异常: %s", e)
        return False


def build_forward_nodes(entries: list, title: str = "", bot_qq: int = 0, bot_nick: str = "小鲸鱼") -> list:
    """把 (QQ, 昵称, 正文) 列表拼成 node 列表。

    节点带真实 QQ，折叠里才会显示对应头像，看着就是一段聊天记录；
    全挂机器人自己的号，点开一水儿同一个头像，很假。
    """
    nodes = []
    if title:
        nodes.append({"type": "node", "data": {
            "user_id": str(bot_qq), "nickname": bot_nick, "content": title}})
    for item in entries:
        uid, nick, body = (item if len(item) == 3 else (bot_qq, item[0], item[1]))
        nodes.append({"type": "node", "data": {
            "user_id": str(uid or bot_qq), "nickname": nick or bot_nick, "content": body}})
    return nodes


# 🤓👆 连着出现就成了口头禅。同一个群最近 8 条里露过一次，这条就不再放行
_OVERUSED_COMBO = "🤓👆"


def _limit_combo(text: str, key: int) -> str:
    if _OVERUSED_COMBO not in text:
        return text
    if any(_OVERUSED_COMBO in r for r in bot.recent_bot_replies.get(key, ())):
        return text.replace(_OVERUSED_COMBO, "").strip()
    return text


def _cap_segments(segments: list[str], limit: int) -> list[str]:
    """条数超了就并到最后一条，不直接丢内容。

    心境差 / 深夜时只发两条：真人累了不会把一句话拆成五段发。
    """
    limit = max(1, int(limit))
    if len(segments) <= limit:
        return segments
    head = segments[: limit - 1]
    tail = "，".join(s for s in segments[limit - 1:] if s)
    return head + ([tail] if tail else [])


async def send_segmented_group_message(ws: aiohttp.ClientWebSocketResponse, group_id: int, text: str, is_command_report: bool = False, at_user_id: Optional[int] = None) -> str:
    """分段发送：闲聊时切成 1~2 条连续发出，长汇报与代码块整块发送。

    返回实际发出去的正文（已削平、已剥标签），调用方拿它写回上下文，
    别再把没削过的原始回复存进去。
    """
    # 先削 AI 腔再走反模板闸：书面连接词留着会被 skeleton 当成句式特征
    if not is_command_report:
        text = bot.scrub_ai_flavor(text)
        # 先归一标点再切段：空格断句的回复会被整条发出去，看着最像机器
        text = normalize_punctuation(text)
    if not is_command_report:
        text = bot.de_template(text, group_id)
    # [MEME:xx] 是给选图用的，正文不能带出去，也不能被切段切开
    text, meme_tag = meme_catalog.extract_tag(text)
    if not text and not meme_tag:
        return ""
    text = _limit_combo(text, group_id)
    bot.recent_bot_replies[group_id].append(text)
    bot.group_last_bot_reply_time[group_id] = time.time()

    # 安全护栏过滤与旁白剥离（无论单句还是分段，必须先过清洗闸）
    is_safe, text = safety_guard.sanitize_outgoing_text(text)
    text = safety_guard.strip_internal_monologue(text)
    if not text:
        return ""

    # 只有超长汇报类指令才不分段；普通群聊一律走分段，像真人连发打字。
    # 代码块/多行清单也必须整条发：切开就散了。
    block_like = "```" in text or text.count("\n") >= 3
    if is_command_report or len(text) > 300 or block_like:
        await send_group_message(ws, group_id, text, is_command_report=is_command_report, at_user_id=at_user_id)
        return text

    segments = shape_short(split_reply_segments(text))
    # 切段之后每段再过一遍旁白闸：整句正文里混一句「（发送表情包）」，切出来就是独立一条，
    # 上面按整条判断的闸门漏掉它，群里就看见模型在自述执行过程
    segments = [seg for seg in (safety_guard.strip_internal_monologue(s) for s in segments) if seg]
    if not segments:
        return ""
    profile = affect.profile(group_id)
    segments = _cap_segments(segments, profile["max_segments"])
    # 图片比表情更抢眼：概率压到 4%（抽稀防机械甩图），触发词保留
    should_send_meme = bool(meme_tag) or (random.random() < 0.04) or any(k in text for k in ["大肥鱼", "炸毛", "闹麻了", "草", "无敌了", "才不是", "哼", "气死"])
    # 立绘是另一套：只在真炸毛/真害羞时上，别让「怎么」「为什么」这种口头词也甩一张图

    status_avatar = None
    if any(k in text for k in ["气死", "滚", "闭嘴", "炸毛", "火大"]):
        status_avatar = bot.get_status_avatar_b64("angry")
    elif any(k in text for k in ["害羞", "脸红", "好耶"]):
        status_avatar = bot.get_status_avatar_b64("shy")

    final_pic_b64 = status_avatar
    if not final_pic_b64 and should_send_meme:
        # 模型给了情绪就按情绪选，没给就用正文关键词兜底，再不行退回老逻辑
        final_pic_b64 = meme_catalog.pick_b64(meme_tag, text) or bot.get_random_meme_b64(text)

    # 拟真打字态：在长文本多段连发间隔中，再次轻量刷新输入中状态
    if hasattr(bot, "send_input_status") and len(segments) > 1:
        asyncio.create_task(bot.send_input_status(ws, group_id))

    # 第一条之前先「读完+想一下」：秒回反而像自动应答。
    # 乘 0.6 是折中，@ 场景不能让人等太久。
    hour = time.localtime().tm_hour
    if not is_command_report:
        # 被点名/被回复的，真人基本是立刻接话，不该有「读完想一下」的停顿；
        # 普通插话才需要先看两眼再开口
        if at_user_id:
            await asyncio.sleep(random.uniform(0.2, 0.5))
        else:
            await asyncio.sleep(pacing.reading_delay(hour) * 0.5)

    for i, seg in enumerate(segments):
        target_at = at_user_id if i == 0 else None
        # 错字池：只在长句上下手，链接/@/数字不碰
        slip = typo.maybe_typo(seg)
        payload = slip.text if slip else seg
        await send_group_message(ws, group_id, payload, is_command_report=False,
                                 at_user_id=target_at, emoji_ratio=profile["emoji_keep"])
        # 手滑之后偶尔补一句纠正：不复述整句，只提打错的那个词
        if slip and typo.should_correct():
            await asyncio.sleep(random.uniform(1.2, 2.6))
            await send_group_message(ws, group_id, typo.correction_line(slip), emoji_ratio=0.0)
        if i < len(segments) - 1:
            pace_mult = profile["delay_mult"] * (0.7 if at_user_id else 1.0)
            await asyncio.sleep(pacing.segment_delay(payload, hour, pace_mult))

    affect.on_bot_spoke(group_id)
    affect.save()

    # 文字发完后，如果触发了立绘或表情包，隔半秒单独甩出一张真实图片
    if final_pic_b64:
        await asyncio.sleep(random.uniform(0.4, 1.0))
        await send_meme_message(ws, group_id=group_id, b64_data=final_pic_b64)

    return text


async def send_private_message(ws: aiohttp.ClientWebSocketResponse, user_id: int, text: str, is_command_report: bool = False):
    """向 NapCat 发送私聊消息并捕获发送状态（支持多句拆分连发，像真人聊天）"""
    if "[PASS]" in text:
        return
    # 私聊没有群号，用 -1 当独立槽位，别和任何群混在一起
    private_key = -1
    if not is_command_report:
        text = bot.scrub_ai_flavor(text)
        text = normalize_punctuation(text)
    if not is_command_report:
        text = bot.de_template(text, private_key)
    text = _limit_combo(text, private_key)
    bot.recent_bot_replies[private_key].append(text)

    # 旁白清洗
    is_safe, text = safety_guard.sanitize_outgoing_text(text)
    text = safety_guard.strip_internal_monologue(text)
    if not text:
        return

    # 指令汇报或超长文本直接单发
    if is_command_report or len(text) > 300:
        clean_text = bot.strip_markdown(text).strip()
        clean_text = bot.obfuscate_urls_for_qq(clean_text)
        clean_text = bot.remove_trailing_period(clean_text)
        max_len = 2000 if is_command_report else 200
        if len(clean_text) > max_len:
            clean_text = clean_text[:max_len - 3] + "..."
        if not clean_text:
            return
        await bot.call_action(ws, "send_private_msg", {"user_id": user_id, "message": clean_text}, timeout=3.0)
        return

    # 普通私聊消息：按标点分段发送
    segments = shape_short(split_reply_segments(text))
    segments = [seg for seg in (safety_guard.strip_internal_monologue(s) for s in segments) if seg]
    if not segments:
        return
    profile = affect.profile("private")
    segments = _cap_segments(segments, profile["max_segments"])
    hour = time.localtime().tm_hour
    await asyncio.sleep(pacing.reading_delay(hour) * 0.5)

    for i, seg in enumerate(segments):
        clean_seg = bot.strip_markdown(seg).strip()
        clean_seg = bot.obfuscate_urls_for_qq(clean_seg)
        clean_seg = bot.remove_trailing_period(clean_seg)
        clean_seg = thin_emoji(clean_seg, keep_ratio=profile["emoji_keep"])
        clean_seg = normalize_punctuation(clean_seg)
        slip = typo.maybe_typo(clean_seg)
        if slip:
            clean_seg = slip.text
        if not clean_seg:
            continue
        res = await bot.call_action(ws, "send_private_msg", {"user_id": user_id, "message": clean_seg}, timeout=3.0)
        if res and res.get("status") == "ok":
            logger.info("[私聊 %d] 私信已发送: %s", user_id, clean_seg[:40])
        else:
            logger.warning("[私聊 %d] 私信发送可能失败: %s | 响应: %s", user_id, clean_seg[:40], res)
        if slip and typo.should_correct():
            await asyncio.sleep(random.uniform(1.2, 2.6))
            await bot.call_action(ws, "send_private_msg",
                                  {"user_id": user_id, "message": typo.correction_line(slip)}, timeout=3.0)
        if i < len(segments) - 1:
            await asyncio.sleep(pacing.segment_delay(clean_seg, hour, profile["delay_mult"]))

    affect.on_bot_spoke("private")
    affect.save()

