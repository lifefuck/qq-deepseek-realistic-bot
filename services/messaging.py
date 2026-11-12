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
from typing import Optional, Tuple

import aiohttp

from services import affect
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
_CJK_GAP_RE = re.compile(r"([\u4e00-\u9fff])\s+([\u4e00-\u9fff])")
# 虚词旁边的空格八成是模型手滑，连起来就好；往里塞逗号会造出「写，的」这种语病
_CJK_FUNC_CHARS = "的了着过是在有和与或都也却很就才而并把被我你他她它这那哪"


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
    def _join_gap(m):
        a, b = m.group(1), m.group(2)
        if a in _CJK_FUNC_CHARS or b in _CJK_FUNC_CHARS:
            return a + b
        return a + "，" + b

    t = _CJK_GAP_RE.sub(_join_gap, t)      # 「显卡没用 认命吧」→「显卡没用，认命吧」
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

# 链接内部含 ? ~ ! 这些「像标点」的字符，必须在分段前整段保护起来
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s，。！？、；：\"'）)】]+")
_URL_SLOT_OPEN, _URL_SLOT_CLOSE = "\ue000", "\ue001"


def _mask_urls(text: str) -> Tuple[str, list]:
    """把链接换成不含强标点的占位符。

    _SEG_END_RE 会在 ? ~ ! 处下刀，`.../video/BV1xx?p=2&t=30` 就被劈成
    「你看这个 …BV1xx?」+「p=2&t=30」两条残句，链接直接点不开。
    """
    urls: list = []

    def _repl(m):
        urls.append(m.group(0))
        return f"{_URL_SLOT_OPEN}{len(urls) - 1}{_URL_SLOT_CLOSE}"

    return _URL_IN_TEXT_RE.sub(_repl, text), urls


def _unmask_urls(text: str, urls: list) -> str:
    def _repl(m):
        try:
            return urls[int(m.group(1))]
        except (IndexError, ValueError):
            return m.group(0)

    return re.sub(rf"{_URL_SLOT_OPEN}(\d+){_URL_SLOT_CLOSE}", _repl, text)


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


_MAX_BUBBLE_CHARS = 10   # 合并目标：真人聊天很少一口气超过十个字
_HARD_BUBBLE_CHARS = 16  # 分句超过这个长度才考虑断；再往下硬切会切出「满爆能器是吧」这种残词

# 换气点：这些字后面天然可以停一下（「冻成冰壳子吧」→ 停在「吧」后）
_PAUSE_AFTER = "吧呢啊呀嘛啦了哦喔呗哦哈"
# 这些字前面往往是新的一句开头（「还行」「就是把」「都三个小时」）
_PAUSE_BEFORE = "都就还也又再才把给让被跟在很太更最别没不"


def _avoid_token_cut(s: str, cut: int) -> int:
    """别把 CQ 码、链接、英文单词从中间劈开——群里会看见半截 [CQ:at,q。"""
    if cut <= 0 or cut >= len(s):
        return cut
    # 链接整段不切：宁可这一条长一点，也不能发出去半截 URL
    for m in re.finditer(r"https?://\S+", s):
        if m.start() < cut < m.end():
            return m.start() if m.start() > 0 else m.end()
    open_i = s.rfind("[", 0, cut)
    if open_i != -1 and s.find("]", open_i) >= cut:
        return open_i
    # 只在真正落在英文/数字串中间时挪位；切在串尾或空格旁就别动
    prev_ascii = s[cut - 1].isascii() and not s[cut - 1].isspace()
    next_ascii = s[cut].isascii() and not s[cut].isspace()
    if prev_ascii and next_ascii:
        start = cut
        while start > 0 and s[start - 1].isascii() and not s[start - 1].isspace():
            start -= 1
        end = cut
        while end < len(s) and s[end].isascii() and not s[end].isspace():
            end += 1
        if start > 0:
            return start
        return end
    return cut


def _split_long_part(part: str) -> list[str]:
    """分句内部没有逗号也要断：模型经常一口气写 25 字不带标点。

    shape_short 以前只在逗号处下刀，模型写的分句本身超过 10 字就永远切不动，
    于是「一句 2~10 字」这条规则形同虚设（实测中位 21 字）。这里补第二刀：
    优先停在虚词换气点，没有就在 10 字处硬断。
    """
    part = (part or "").strip()
    if len(part) <= _HARD_BUBBLE_CHARS:
        return [part] if part else []
    out: list[str] = []
    while len(part) > _HARD_BUBBLE_CHARS:
        hi = min(len(part) - 4, _HARD_BUBBLE_CHARS)
        lo = 7  # 6 字以下断出来就是个残句（「星染这是要」），不如不断
        cut = None
        # 先找「吧/呢/啊…」这种天然换气点，再退而求其次找新句开头
        for i in range(hi, lo - 1, -1):
            if part[i - 1] in _PAUSE_AFTER:
                cut = i
                break
        if cut is None:
            for i in range(hi, lo - 1, -1):
                if part[i] in _PAUSE_BEFORE:
                    cut = i
                    break
        if cut is None:
            # 英文/数字串前也是天然断点（「…属于是 | QQ 频道」）：切在串前面
            for m in re.finditer(r"[A-Za-z0-9]{2,}", part[:hi + 1]):
                if m.start() >= lo:
                    cut = m.start()
        if cut is None:
            # 再退一步：切在串后面，至少别让后半句一直长下去
            for m in re.finditer(r"[A-Za-z0-9]{2,}", part):
                if lo <= m.end() <= len(part) - 4:
                    cut = m.end()
                    break
        if cut is None:
            # 实在找不到换气点就别硬切：宁可这一条长两三字，也不能切出半个词
            break
        cut = _avoid_token_cut(part, cut)
        if cut <= 2 or cut >= len(part):
            break
        out.append(part[:cut].strip(" ，,、"))
        part = part[cut:].strip(" ，,、")
    if part:
        out.append(part)
    return [p for p in out if p]


# CQ 码和裸链接：内部可能带逗号，切句前一并保护
_TOKEN_RE = re.compile(r"\[CQ:[^\]]*\]|https?://[^\s，。！？、；\u4e00-\u9fff]+")
_TOKEN_RE_PLACEHOLDER = re.compile(r"\x00(\d+)\x00")


def _mask_tokens(s: str):
    """把 CQ 码和链接换成占位符，避免按逗号切句时把它们切成半截。"""
    keep: list[str] = []

    def repl(m):
        keep.append(m.group(0))
        return f"\x00{len(keep) - 1}\x00"

    return _TOKEN_RE.sub(repl, s), keep


def _unmask_tokens(s: str, keep: list[str]) -> str:
    return _TOKEN_RE_PLACEHOLDER.sub(lambda m: keep[int(m.group(1))], s)


def shape_short(segments: list[str], limit: int = 4) -> list[str]:
    """把「一句话塞三个逗号」的长句切成几条短气泡。

    真人打字是一段一段蹦出来的：先说「刚吃完饭」，再补「去买了杯咖啡」。
    模型爱写「刚吃完饭，然后去楼下买了杯咖啡，顺手把快递也拿了」这种一整条，
    切段器只在句末标点处下刀，切完仍是十几个字一条，所以这里补两刀：
    先按逗号拆，再按虚词/字数断长分句，最后把碎片合并到 10 字左右。
    """
    out: list[str] = []
    for seg in segments:
        seg = (seg or "").strip()
        if not seg:
            continue
        # CQ 码和链接里自带逗号，直接按逗号切会把 [CQ:at,qq=...] 拆成两半，
        # 发出去就是半截代码。先整体换成占位符，切完再换回来
        masked, keep = _mask_tokens(seg)
        parts = [p.strip() for p in re.split(r"[，,、；;]", masked) if p.strip()] or [masked]
        pieces: list[str] = []
        for part in parts:
            pieces.extend(_split_long_part(part))
        buf = ""
        for p in pieces:
            if not buf:
                buf = p
            elif len(buf) + len(p) + 1 <= _MAX_BUBBLE_CHARS:
                buf = f"{buf}，{p}"
            else:
                out.append(_unmask_tokens(buf, keep))
                buf = p
        if buf:
            out.append(_unmask_tokens(buf, keep))
    if len(out) > limit:
        out = out[: limit - 1] + ["，".join(out[limit - 1:])]
    return [s.strip(" ，,、") for s in out if s.strip(" ，,、")]


# 群聊里被用烂的模板词：它们本身不算错，但连着几条都用就成了「AI 味」的证据
_CLICHE_WORDS = ("这波属于是", "属于是", "这波", "整不会", "绷不住", "破防了")
_CLICHE_NEUTRAL = {"这波属于是": "就是", "属于是": "就是", "这波": "", "整不会": "", "绷不住": "", "破防了": ""}


def de_cliche(text: str, recent: list[str] | None = None) -> str:
    """把最近几句已经用过的模板词删掉，而不是把整句毙掉。

    模型不会因为提示词里写着「别用这波」就不写；但看见自己刚说过，
    再让代码把重复的那个词摘掉，句子还在，腔调就换了。
    """
    if not text:
        return text
    recent_text = " ".join(recent or ())
    out = text
    for w in _CLICHE_WORDS:
        if w not in out:
            continue
        if w in recent_text or out.count(w) > 1:
            out = out.replace(w, _CLICHE_NEUTRAL[w])
    out = re.sub(r"[，,]{2,}", "，", out).strip(" ，,、")
    # 删词是减法，删空了就退回原句——宁可土一点也不能把一句话弄没
    return out if len(out) >= 3 else text


def _fold_overflow(segs: list[str], limit: int = 3) -> list[str]:
    """超过 limit 段就把剩下的并成最后一条。
    以前是 segs[:3] 裸切，第 4 段起直接凭空消失——用户看到的是半截回答。"""
    if len(segs) <= limit:
        return segs
    head = segs[: limit - 1]
    tail = "".join(segs[limit - 1:])
    return head + [tail]


def split_reply_segments(text: str) -> list[str]:
    """按强标点（！？”。~）、换行切成 1~3 条短消息，像真人连续打字。
    不拿空格当分隔符，也不把中英混排切断。若回复包含换行则优先按行分段发。"""
    clean = bot.strip_markdown(text).strip()
    clean = bot.remove_trailing_period(clean)
    if not clean:
        return []
    clean, _urls = _mask_urls(clean)
    lines = [l.strip() for l in clean.split('\n') if l.strip()]
    if len(lines) > 1:
        return [_unmask_urls(x, _urls) for x in _fold_overflow(lines)]
    if len(clean) <= 16:
        return [_unmask_urls(clean, _urls)]

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
        return [_unmask_urls(x, _urls) for x in _fold_overflow(parts)]
    longer = _split_long_clause(clean)
    if longer:
        return [_unmask_urls(x, _urls) for x in longer]
    return [_unmask_urls(clean, _urls)]


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
    if safety_guard.is_pass_signal(text):
        return
    clean_text = bot.strip_markdown(text).strip()
    # 兜底闸：走单发通道的文本不许夹带 CQ 码（合法的 @ 除外）
    clean_text = bot.strip_outgoing_cq(clean_text)
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


async def send_segmented_group_message(ws: aiohttp.ClientWebSocketResponse, group_id: int, text: str, is_command_report: bool = False, at_user_id: Optional[int] = None, max_segments: Optional[int] = None) -> str:
    """分段发送：闲聊时切成 1~2 条连续发出，长汇报与代码块整块发送。

    max_segments 用来压段数：冷场冒泡只许一句，切成两条就成了自己给自己续话。
    返回实际发出去的正文（已削平、已剥标签），调用方拿它写回上下文，
    别再把没削过的原始回复存进去。
    """
    # 先在最原始的文本上判静默信号：下面的洗文和标点归一会把「[PASS]」搓成「PASS]」，
    # 等到出站闸再认就晚了，群里已经出现一条 PASS
    if safety_guard.is_pass_signal(text):
        return ""
    # 先削 AI 腔再走反模板闸：书面连接词留着会被 skeleton 当成句式特征
    if not is_command_report:
        text = bot.scrub_ai_flavor(text)
        # 先归一标点再切段：空格断句的回复会被整条发出去，看着最像机器
        text = normalize_punctuation(text)
    if not is_command_report:
        text = bot.de_template(text, group_id)
        # 模板词限流放在最后一道：前几步可能又把「这波」拼回来
        text = de_cliche(text, list(bot.recent_bot_replies.get(group_id, ()))[-6:])
    # [MEME:xx] 是给选图用的，正文不能带出去，也不能被切段切开
    text, meme_tag = meme_catalog.extract_tag(text)
    # 出站兜底：模型复读群友的 CQ 码（尤其带 raw= 的新版表情码）会被 NapCat
    # 原样当文本发出去，群里看到的就是一句「报错」。切段前先剥干净。
    text = bot.strip_outgoing_cq(text)
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
    if max_segments:
        segments = segments[:max_segments]
    # 图片比表情更抢眼：概率压到 4%（抽稀防机械甩图），触发词保留
    should_send_meme = bool(meme_tag) or (random.random() < 0.04) or any(k in text for k in ["大肥鱼", "炸毛", "闹麻了", "卧槽", "无敌了", "才不是", "气死"])
    # 立绘是另一套：只在真炸毛/真害羞时上，别让「怎么」「为什么」这种口头词也甩一张图

    status_avatar = None
    if any(k in text for k in ["气死", "滚啊", "闭嘴", "炸毛", "火大"]):
        status_avatar = bot.get_status_avatar_b64("angry")
    elif any(k in text for k in ["害羞", "脸红", "好耶"]):
        status_avatar = bot.get_status_avatar_b64("shy")

    final_pic_b64 = status_avatar
    if not final_pic_b64 and should_send_meme:
        # 模型给了情绪就按情绪选，没给就用正文关键词兜底，再不行退回老逻辑
        final_pic_b64 = meme_catalog.pick_b64(meme_tag, text) or bot.get_random_meme_b64(text)

    # 群聊不做拟真延迟：群里要的是接得上话，模型吐完就发。
    # 拟真节奏只留给私聊（见 send_private_message），一对一才需要「看完想了想」。
    for i, seg in enumerate(segments):
        target_at = at_user_id if i == 0 else None
        # 错字池已停用：纠错补发被砍掉之后，故意打错字会永久留在群里
        payload = seg
        await send_group_message(ws, group_id, payload, is_command_report=False,
                                 at_user_id=target_at, emoji_ratio=profile["emoji_keep"])

    affect.on_bot_spoke(group_id)
    affect.save()

    # 文字发完后，如果触发了立绘或表情包，隔半秒单独甩出一张真实图片
    if final_pic_b64:
        await send_meme_message(ws, group_id=group_id, b64_data=final_pic_b64)

    return text


async def send_private_message(ws: aiohttp.ClientWebSocketResponse, user_id: int, text: str, is_command_report: bool = False):
    """向 NapCat 发送私聊消息并捕获发送状态（支持多句拆分连发，像真人聊天）"""
    if safety_guard.is_pass_signal(text):
        return
    # 私聊这条链路以前不剥梗标签，群聊剥、私聊漏，主人私聊里会看到 [MEME:嘲讽]
    # 私聊没有配图需求，标签直接扔掉
    text, _private_meme_tag = meme_catalog.extract_tag(text)
    if not text:
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
        clean_text = bot.strip_outgoing_cq(clean_text)
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
    # 私聊也不做拟真延迟：全砍。要快就一条条立刻发。
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
        # 手滑纠正也一起砍：多等两秒只为了补一个错字，不划算

    affect.on_bot_spoke("private")
    affect.save()

