# -*- coding: utf-8 -*-
"""自然语言动作层：把「骂回去 / 无差别攻击 / 夸谁」这类要求真的执行掉。

以前这些请求都掉进闲聊分支，模型只能回一句「攻击谁啊，你又怎么了」——
不是它不肯干，是根本没有执行通道。这里补一层最小可用的动作分发：
识别意图 → 定位人 → 生成一句话 → @ 出去。

只在被直接呼叫（@/引用）或主人发话时触发，避免群友聊到「骂」字就被拦。
"""
import asyncio
import json
import logging
import random
import re
import time
from typing import Dict, Optional

import connectors
import meme_catalog
from services import vision


class _BotProxy:
    """`python bot.py` 跑起来时 __main__ 和 bot 是两个模块对象，
    自己 import bot 会读到第二份全局状态（还会把 bot.py 再执行一遍）。
    按项目惯例由 bot.py 启动时 bind 注入。
    """

    _host = None

    def bind(self, host):
        type(self)._host = host

    def __getattr__(self, name):
        host = type(self)._host
        if host is None:
            raise RuntimeError("handlers.actions 尚未 bind(bot)")
        return getattr(host, name)


bot = _BotProxy()
bind = bot.bind   # bot.py 的注入循环按模块级 bind() 调用

# 不要在导入期碰 bot.logger：bot.py 会反过来导入 handlers，这时它还没初始化完
logger = logging.getLogger("actions")

# 「无差别攻击所有人」「群嘲一下」「挨个骂」
RE_ATTACK_ALL = re.compile(
    r"群嘲|无差别|挨个(骂|怼|喷)"
    r"|(攻击|骂|怼|喷).{0,4}(所有人|全部人|群里|全群|他们)"
    r"|(所有人|每个人).{0,6}(攻击|骂|怼|喷)"
)
# 「骂一下小明」「给我喷那个神人」「怼回去」——后者由 llm 的空转重试处理
RE_TARGET = re.compile(r"^(?:给我|帮我|去)?\s*(?:骂|怼|喷|嘲讽|损)(?:一下|一句)?\s*(.+)$")

# 「查一下今天有什么新闻」「搜搜那个梗」「XX 是什么」
RE_SEARCH = re.compile(
    r"^(?:给我|帮我|你)?(?:去)?(?:查|搜|搜索|查一下|搜一下|查查|搜搜)(?:一下|一查)?\s*(.+)$"
    r"|^(.{2,20}?)(?:是什么|啥意思|怎么回事|多少钱|几点|在哪)"
)
# 「B站热榜」「有什么好看的」
RE_BILI = re.compile(r"(b站|B站|哔哩).{0,4}(热榜|热门|排行|好看)|(热榜|热门|排行).{0,3}(b站|B站)|有什么好看的")

# 「这张图啥意思」「看看这个截图」
RE_IMAGE = re.compile(r"(这张图|这图|图里|看图|识图|图是啥|什么图|什么番|这截图|图里的)")
# 「发个表情包」「来张图」「斗图」
RE_MEME = re.compile(r"(发|来|整|甩)(个|张|一张)?(表情包|表情|图)|斗图|发表情")
# 群管理：只认主人的话，且目标必须能对上具体的人
RE_BAN = re.compile(r"^(?:给我|帮我)?\s*(?:把)?\s*(?:禁言|闭嘴|让他闭嘴)\s*(.*)$")
RE_KICK = re.compile(r"^(?:给我|帮我)?\s*(?:把)?\s*(?:踢|踹)(?:出去|掉|了)?\s*(.*)$")

# 模型抽风时的兜底：宁可普通，也别空转
_FALLBACK_LINES = (
    "就你这发言水平也好意思开口",
    "说话前先过过脑子",
    "你这逻辑自己听着不别扭吗",
)

# 骂人只能损事，不能祸及家人/身体；模型偶尔越线，这里硬拦
_BAD_LINE = re.compile(r"(全家|你妈|你爸|爹妈|祖宗|家里人|一家人|去死|死了|断子绝孙|傻逼|煞笔)")


def _clean_line(line: str) -> str:
    line = re.sub(r"[（(][^（）()]*[）)]", "", line or "").strip()
    if _BAD_LINE.search(line):
        return ""
    return line


def _recent_active(group_id: int, limit: int, exclude: set) -> list:
    """最近说过话的人，按时间倒序去重。只用于「无差别攻击」选靶子。"""
    out, seen = [], set()
    for m in reversed(bot.group_timelines.get(group_id, ())):
        uid = m.get("sender_id")
        if not uid or uid in seen or uid in exclude:
            continue
        seen.add(int(uid))
        nick = bot.group_member_nicks.get(group_id, {}).get(int(uid)) or m.get("sender", "")
        nick = re.sub(r"\(QQ.*?\)|\(你自己\)", "", str(nick)).strip() or str(uid)
        out.append((int(uid), nick))
        if len(out) >= limit:
            break
    return out


def _resolve_target(group_id: int, text: str) -> Optional[tuple]:
    """把「小明」「老王」这类称呼对回 QQ 号。"""
    body = re.sub(r"[\s，。！？!?~～]", "", text)
    nicks = bot.group_member_nicks.get(group_id, {})
    # 长的先匹配，避免「阿明」命中「阿明明」
    for uid, nick in sorted(nicks.items(), key=lambda kv: -len(str(kv[1]))):
        if nick and str(nick) in body:
            return int(uid), str(nick)
    # 名片缓存没刷到（新群/刚重启）时，从最近发言人里对
    for m in reversed(bot.group_timelines.get(group_id, ())):
        s = re.sub(r"\(QQ.*?\)|\(你自己\)", "", str(m.get("sender", ""))).strip()
        if s and s in body and m.get("sender_id"):
            return int(m["sender_id"]), s
    # 数字兜底：只在原句里没有量词时认（「我踢了80000步」不能把步数当 QQ 号踢人）
    if not _NUM_UNIT.search(text):
        m = re.search(r"(\d{5,12})", body)
        if m:
            return int(m.group(1)), str(m.group(1))
    return None


async def _gen_lines(people: list, mood: str) -> dict:
    """一次调用生成若干句，返回 {qq: 台词}。"""
    roster = "\n".join(f"{qq}\t{nick}" for qq, nick in people)
    prompt = (
        f"群里这几个人最近在说话：\n{roster}\n\n"
        f"现在要用一句话挨个{mood}他们，一人一句，不要重样。\n"
        f"要求：只损他们说话的方式或内容（抬杠、复读、装懂、话多），"
        f"不扯家人、不骂身体、不诅咒；每句 6~14 个字，口语，不用句号。\n"
        f'只输出 JSON：{{"items":[{{"qq":"QQ号","line":"台词"}}]}}'
    )
    payload = bot.get_base_chat_payload(
        model=bot.MODEL_NAME,
        messages=[
            {"role": "system", "content": bot.SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        max_tokens=300,
        temperature=0.95,
        effort="high",
    )
    payload["response_format"] = {"type": "json_object"}
    try:
        async with bot.llm_semaphore:
            async with bot.get_http_session().post(
                bot.SILICONFLOW_API_URL,
                headers={"Authorization": f"Bearer {bot.SILICONFLOW_API_KEY}",
                         "Content-Type": "application/json"},
                json=payload, timeout=30,
            ) as resp:
                if resp.status != 200:
                    logger.warning("[动作层] 生成失败 HTTP %d", resp.status)
                    return {}
                data = await resp.json()
    except Exception as e:
        logger.warning("[动作层] 生成异常: %s", e)
        return {}

    raw = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    try:
        obj = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        return {str(it.get("qq")).strip(): str(it.get("line", "")).strip()
                for it in obj.get("items", []) if it.get("line")}
    except Exception:
        logger.warning("[动作层] JSON 解析失败: %r", raw[:120])
        return {}


async def _chat(user_prompt: str, max_tokens: int = 120, effort: str = "low",
                temperature: float = 0.7) -> str:
    """一次短问答，只拿来把搜索结果说成人话。"""
    payload = bot.get_base_chat_payload(
        model=bot.MODEL_NAME,
        messages=[
            {"role": "system", "content": bot.SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
        effort=effort,
    )
    try:
        async with bot.llm_semaphore:
            async with bot.get_http_session().post(
                bot.SILICONFLOW_API_URL,
                headers={"Authorization": f"Bearer {bot.SILICONFLOW_API_KEY}",
                         "Content-Type": "application/json"},
                json=payload, timeout=25,
            ) as resp:
                if resp.status != 200:
                    logger.warning("[动作层] 问答失败 HTTP %d", resp.status)
                    return ""
                data = await resp.json()
    except Exception as e:
        logger.warning("[动作层] 问答异常: %s", e)
        return ""
    return (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()


async def _do_search(ws, group_id: int, query: str) -> None:
    res = await connectors.search_web_brief(bot.get_http_session(), query, limit=3)
    if not res:
        await bot.call_action(ws, "send_group_msg",
                              {"group_id": group_id, "message": "没搜到，换个说法再问"})
        return
    ctx = "\n".join(f"- {r['title']}：{(r.get('snippet') or '')[:120]}" for r in res)
    ans = await _chat(
        f"群友问：{query}\n我只搜到这些：\n{ctx}\n\n"
        f"用一两句口语回答他，总长不超过 40 字，别复述「我搜到」这种过程，"
        f"没有把握就说不知道，别编。",
        max_tokens=120, temperature=0.5,
    )
    if not ans:
        ans = f"搜到一条：{res[0]['title']}"
    for line in [x.strip() for x in ans.split("\n") if x.strip()][:2]:
        await bot.call_action(ws, "send_group_msg", {"group_id": group_id, "message": line})
    logger.info("[动作层] 搜索 %r -> %s", query, ans[:40])


async def _do_bili(ws, group_id: int) -> None:
    vids = await connectors.fetch_bilibili_hot_videos(bot.get_http_session(), limit=3)
    titles = [v.get("title", "").strip() for v in vids if v.get("title")]
    if not titles:
        await bot.call_action(ws, "send_group_msg",
                              {"group_id": group_id, "message": "B站这会儿抓不到，等会儿再看"})
        return
    head = "B站现在火的是" + "、".join(f"「{t}」" for t in titles[:2])
    await bot.call_action(ws, "send_group_msg", {"group_id": group_id, "message": head})
    if len(titles) > 2:
        await bot.call_action(ws, "send_group_msg",
                              {"group_id": group_id, "message": f"还有个「{titles[2]}」"})
    logger.info("[动作层] B站热榜 %d 条", len(titles))



async def _do_image(ws, group_id: int, sender_id: int) -> None:
    """看群友刚发的图。图在 group_recent_images 里，优先取他自己发的。"""
    pool = list(bot.group_recent_images.get(group_id, ()))
    if not pool:
        await bot.send_group_message(ws, group_id, "哪有图，我瞎了", at_user_id=sender_id)
        return
    now = time.time()
    url, owner = None, None
    for item in reversed(pool):
        u, ts = item[0], item[1]
        if now - ts > 300:
            continue
        sid = item[2] if len(item) >= 3 else None
        if url is None:
            url, owner = u, sid
        if sid == sender_id:
            url, owner = u, sid
            break
    if not url or not bot.http_session:
        await bot.send_group_message(ws, group_id, "图裂了，重发一张", at_user_id=sender_id)
        return
    desc = await vision.describe_image(bot.http_session, url)
    if not desc:
        await bot.send_group_message(ws, group_id, "这图我看不明白", at_user_id=sender_id)
        return
    desc = re.sub(r"\s+", " ", desc).strip()
    await bot.send_group_message(ws, group_id, desc[:80], at_user_id=sender_id)
    logger.info("[动作层] 识图 -> %s", desc[:50])


async def _do_meme(ws, group_id: int, sender_id: int, body: str) -> None:
    b64 = meme_catalog.pick_b64(None, body)
    if not b64:
        await bot.send_group_message(ws, group_id, "库存空了，明天再整", at_user_id=sender_id)
        return
    await bot.send_meme_message(ws, group_id=group_id, b64_data=b64)
    logger.info("[动作层] 发了一张表情包")


def _parse_duration(text: str) -> tuple:
    """「10分钟」「半小时」「1小时」→ 秒；解析不出按 10 分钟。"""
    if "半小时" in text:
        return 1800, text.replace("半小时", "").replace("半钟头", "")
    m = re.search(r"(\d+)\s*(秒|分钟|分|小时|钟头|天)", text)
    if not m:
        return 600, text
    n, unit = int(m.group(1)), m.group(2)
    mult = {"秒": 1, "分钟": 60, "分": 60, "小时": 3600, "钟头": 3600, "天": 86400}[unit]
    return max(60, min(n * mult, 12 * 3600)), (text[:m.start()] + text[m.end():])


_ADMIN_CACHE: Dict[tuple, tuple] = {}  # (群,人) -> (判定结果, 时间戳)


async def _is_group_admin(ws, group_id: int, uid: int) -> bool:
    if uid == bot.ADMIN_QQ:
        return True
    # 原来每条带「禁言/踢」的普通发言都同步等一次 no_cache 查询（最长 5 秒），
    # 无权限的人每刷一次就白等 5s，消息链路也跟着卡。缓存 60 秒。
    key = (group_id, uid)
    hit = _ADMIN_CACHE.get(key)
    if hit and time.time() - hit[1] < 60:
        return hit[0]
    res = await bot.call_action(ws, "get_group_member_info",
                                {"group_id": group_id, "user_id": uid, "no_cache": True}, timeout=2.0)
    ok = bool(res and res.get("status") == "ok"
              and (res.get("data") or {}).get("role") in ("admin", "owner"))
    _ADMIN_CACHE[key] = (ok, time.time())
    return ok


# 「该不该禁言」「踢不踢」这种讨论句不是命令：以前命中关键词加一个名字就真禁/真踢
_ASK_WORDS = re.compile(r"[?？]|吗|呢|吧|该不该|要不要|是不是|能不能|可不可以|如果|假如|要不|是否|该禁|该踢")


def _is_imperative(body: str) -> bool:
    return not _ASK_WORDS.search(body)


# 数字兜底会把「我踢了80000步」的步数当 QQ 号踢人；后面跟量词的不算号码
_NUM_UNIT = re.compile(r"\d{5,12}\s*(?:步|元|块|个|人|分|秒|天|年|岁|次|条|页|米|度|斤|万|千|层|抽|连)")


async def _do_ban(ws, group_id: int, sender_id: int, body: str) -> None:
    if not await _is_group_admin(ws, group_id, sender_id):
        await bot.send_group_message(ws, group_id, "你说话不好使", at_user_id=sender_id)
        return
    secs, rest = _parse_duration(body)
    target = _resolve_target(group_id, rest)
    if not target:
        await bot.send_group_message(ws, group_id, "禁谁啊，说个名字", at_user_id=sender_id)
        return
    qq, nick = target
    if qq in (bot.BOT_QQ, bot.ADMIN_QQ):
        await bot.send_group_message(ws, group_id, "这个不行，换个人", at_user_id=sender_id)
        return
    # 群管本来就能自己禁言，但「借机器人的号去处置别的管理/群主」会直接把矛盾
    # 引到机器人头上（而且禁群主必然失败，回一句「我禁不动他」更难看）
    if qq != sender_id and await _is_group_admin(ws, group_id, qq):
        await bot.send_group_message(ws, group_id, "他也是管理，我不掺和", at_user_id=sender_id)
        return
    res = await bot.call_action(ws, "set_group_ban",
                                {"group_id": group_id, "user_id": qq, "duration": secs}, timeout=6.0)
    if res and res.get("status") == "ok":
        await bot.send_group_message(ws, group_id, f"{nick} 闭嘴 {secs // 60} 分钟")
    else:
        logger.warning("[动作层] 禁言失败: %s", res)
        await bot.send_group_message(ws, group_id, "我禁不动他，看看我是不是管理员")


async def _do_kick(ws, group_id: int, sender_id: int, body: str) -> None:
    if not await _is_group_admin(ws, group_id, sender_id):
        await bot.send_group_message(ws, group_id, "你说话不好使", at_user_id=sender_id)
        return
    target = _resolve_target(group_id, body)
    if not target:
        await bot.send_group_message(ws, group_id, "踢谁，说清楚", at_user_id=sender_id)
        return
    qq, nick = target
    if qq in (bot.BOT_QQ, bot.ADMIN_QQ):
        await bot.send_group_message(ws, group_id, "这个不行，换个人", at_user_id=sender_id)
        return
    if qq != sender_id and await _is_group_admin(ws, group_id, qq):
        await bot.send_group_message(ws, group_id, "他也是管理，我不掺和", at_user_id=sender_id)
        return
    res = await bot.call_action(ws, "set_group_kick",
                                {"group_id": group_id, "user_id": qq, "reject_add_request": False}, timeout=6.0)
    if res and res.get("status") == "ok":
        await bot.send_group_message(ws, group_id, f"{nick} 送走了")
    else:
        logger.warning("[动作层] 踢人失败: %s", res)
        await bot.send_group_message(ws, group_id, "踢不动，权限不够")


async def _send(ws, group_id: int, qq: int, line: str) -> None:
    line = _clean_line(line)
    if not line:
        line = random.choice(_FALLBACK_LINES)
    # 以前直接 call_action("send_group_msg")，绕过 PASS 闸、CQ 剥离、群级发送锁和
    # 禁言熔断——动作层恰恰是最容易连发撞风控的地方。统一走 send_group_message。
    await bot.send_group_message(ws, group_id, line, at_user_id=qq or None)
    logger.info("[动作层] @%d 已发出: %s", qq, line)


async def maybe_run(ws, group_id: int, sender_id: int, sender_nick: str,
                    is_admin: bool, text: str, addressed: bool) -> bool:
    """识别到动作意图并执行完返回 True（调用方直接 return，不再走闲聊）。"""
    if not addressed and not is_admin:
        return False
    body = (text or "").strip()
    if not body or len(body) > 40:
        return False

    # 无差别攻击：挑最近说过话的，别逮着同一个人刷
    if RE_ATTACK_ALL.search(body):
        people = _recent_active(group_id, 3, {bot.BOT_QQ, bot.ADMIN_QQ, sender_id})
        if not people:
            await bot.call_action(ws, "send_group_msg",
                                  {"group_id": group_id, "message": "群里就你一个活人，攻击谁"})
            return True
        lines = await _gen_lines(people, "损")
        logger.info("[动作层] 无差别攻击 %d 人，生成 %d 句", len(people), len(lines))
        for qq, nick in people:
            line = lines.get(str(qq)) or random.choice(_FALLBACK_LINES)
            await _send(ws, group_id, qq, line)
            await asyncio.sleep(random.uniform(0.8, 1.6))
        return True

    # 指着名骂：必须真 @ 到人，别对着空气骂
    m = RE_TARGET.match(body)
    if m:
        who = m.group(1).strip()
        if who in ("你", "回去"):   # 「骂你」「骂回去」交给闲聊，它自己能接
            return False
        target = _resolve_target(group_id, who)
        if not target:
            return False
        qq, nick = target
        # 这条路径零权限门槛：普通群友能借机器人的号当众辱骂任何人，包括主人。
        # 骂群管/主人这条拦住，其余随意。
        if qq in (bot.BOT_QQ, bot.ADMIN_QQ) or await _is_group_admin(ws, group_id, qq) and sender_id != bot.ADMIN_QQ:
            await bot.send_group_message(ws, group_id, "这个我不敢骂，换个人", at_user_id=sender_id)
            return True
        lines = await _gen_lines([target], "损")
        await _send(ws, group_id, qq, lines.get(str(qq)) or random.choice(_FALLBACK_LINES))
        return True

    # 群管理：破坏性动作必须点名到具体的人，权限在 _do_* 里查
    if ("禁言" in body or "闭嘴" in body) and len(body) <= 20 and _is_imperative(body) \
            and _resolve_target(group_id, body):
        await _do_ban(ws, group_id, sender_id, body)
        return True
    if re.search(r"(踢|踹)", body) and len(body) <= 20 and _is_imperative(body) \
            and _resolve_target(group_id, body):
        await _do_kick(ws, group_id, sender_id, body)
        return True

    # 看图：「这张图啥意思」——图已经在 group_recent_images 里躺着了
    if RE_IMAGE.search(body):
        await _do_image(ws, group_id, sender_id)
        return True

    # 「发个表情包」
    if RE_MEME.search(body) and len(body) <= 12:
        await _do_meme(ws, group_id, sender_id, body)
        return True

    # 「B站热榜」这类固定来源，先于搜索匹配，不然「B站」会被当成搜索词
    if RE_BILI.search(body):
        await _do_bili(ws, group_id)
        return True

    # 联网查东西：以前只有「梗」才触发搜索，其他问题全靠模型硬编
    # 群配置里 enable_search=False 时动作层照旧联网搜索（这个键原来只在 llm 里生效）
    if not bot.get_group_config(group_id).get("enable_search", True):
        return False
    m = RE_SEARCH.search(body)
    if m:
        query = (m.group(1) or m.group(2) or "").strip(" ，。,.")
        # 「这是什么意思」这种空指代搜不出东西，还会显得乱接话
        if len(query) >= 3 and not re.match(r"^[这那你我他她它]", query):
            await _do_search(ws, group_id, query)
            return True

    # 「今天天气怎么样」「明天什么新闻」——没有明确动词，但确实是问实时信息
    if re.search(r"(天气|气温|下雨|新闻|股价|金价|汇率|比分|赛程|上映|航班)", body):
        await _do_search(ws, group_id, body)
        return True

    return False
