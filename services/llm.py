# -*- coding: utf-8 -*-
"""调模型：群聊回复、戳一戳、好感度判定。

提示词在 prompts.py，全局状态在 bot 里，这里只负责发请求和解析结果。
"""

from meme_manager import meme_manager
import connectors
import cross_interaction
import cross_memory

from typing import Optional
from typing import Tuple
import aiohttp
import asyncio
import json
import logging
import random
import re
import time

# 由 bot.py 在启动时注入自己。直接 import bot 在 `python bot.py` 下会拿到
# 另一个模块实例（__main__ 与 bot 各一份），全局状态就分裂了。
bot = None


def bind(host):
    global bot
    bot = host

logger = logging.getLogger("qqbot.llm")

async def call_deepseek(timeline_text: str, is_at: bool, is_admin: bool = False, is_private: bool = False, is_mention: bool = False, target_uid: Optional[int] = None, sender_nick: str = "", group_id: Optional[int] = None) -> Optional[str]:
    """请求 SiliconFlow DeepSeek-V3 模型"""
    headers = {
        "Authorization": f"Bearer {bot.SILICONFLOW_API_KEY}",
        "Content-Type": "application/json"
    }
    
    identity_note = f"（补充认知：你在 QQ 上的名字/昵称叫「小鲸鱼」或「鲸宝」，你的 QQ 号是 {bot.BOT_QQ}，你叫 life bot fish 是因为部署账号的昵称，那也是你！严禁把自己的名字当成别人或山寨货）\n"

    # 群友记忆与好感度上下文注入
    user_mem_prompt = ""
    if target_uid and target_uid != bot.BOT_QQ:
        mem = bot.get_or_create_user_memory(target_uid, sender_nick, group_id=group_id)
        if target_uid == bot.ADMIN_QQ:
            user_mem_prompt = f"【当前交谈者身份】：专属主人兼创造者 life（QQ {bot.ADMIN_QQ}），拥有绝对最高权限与天然极高好感度。\n"
        else:
            notes_str = "、".join(mem.get("notes", [])) if mem.get("notes") else "初次接触"
            user_mem_prompt = (
                f"【当前交谈群友信息】：昵称: {mem.get('nickname')}, 好感度: {mem.get('favor')}/100\n"
                f"【长期印象（只是你对他的背景认知，不是他这一轮说过的话，绝对不能拿来当对话内容回应）】：{notes_str}\n"
            )

    # 注入群内焦点互动关系（仅群聊生效）
    if group_id and not is_private:
        relations_hint = cross_interaction.get_group_relations_summary(group_id, current_user_id=target_uid)
        if relations_hint:
            user_mem_prompt += relations_hint

    channel_mode = bot.get_channel_mode(group_id if not is_private else None)
    is_root_work = is_admin and channel_mode == "root"
    if is_admin and is_private:
        if channel_mode == "root":
            user_prompt = (
                f"{identity_note}"
                f"【当前交谈者身份】：专属主人兼创造者「{sender_nick}」(QQ {bot.ADMIN_QQ})\n"
                f"以下是你们的历史对话记录：\n{timeline_text}\n\n"
                f"【核心交互原则】：\n"
                f"1. 你依然是主人身边那条熟悉的大肥鱼/小鲸鱼，在 /root 模式下你是他的私人技术搭档与秘书；\n"
                f"2. 懂主人的日常口语、黑话、意图与暗语，绝不咬文嚼字，绝不打官腔背书；\n"
                f"3. 像天天开黑写代码的老朋友一样交流，直接奔着解决问题去，干脆利落自然回复（1~2句，绝不用句号）："
            )
        else:
            user_prompt = (
                f"{identity_note}"
                f"以下是你与专属主人「{sender_nick}」(QQ {bot.ADMIN_QQ}) 的私聊历史：\n{timeline_text}\n\n"
                # 记忆块放时间线后面：历史是累加的公共前缀，放前面才能被缓存命中
                f"{user_mem_prompt}"
                f"【主人发来私信】：\n"
                f"当前跟你说话的是你的专属主人兼创造者。请清楚认同主人身份，态度自然真实、听主人的话，结合好感度自然交流（20~40 字）："
            )
    elif is_admin and is_at:
        if channel_mode == "root":
            user_prompt = (
                f"{identity_note}"
                f"以下是当前群聊对话记录：\n{timeline_text}\n\n"
                f"【主人「{sender_nick}」(QQ {bot.ADMIN_QQ}) 在群里找你】：\n"
                f"当前是 /root 工作模式，你是主人的技术老搭档兼秘书。懂主人的大白话与口语意图，像熟人开黑一样干脆利落接话（1~2句，绝不用句号）："
            )
        else:
            user_prompt = (
                f"{identity_note}"
                f"以下是当前群聊对话记录：\n{timeline_text}\n\n"
                f"{user_mem_prompt}"
                f"【注意识别对话者身份】：当前是你的专属主人兼创造者 life (QQ {bot.ADMIN_QQ}) 在群里呼叫你！\n"
                f"（清楚知道对方是你的主人 life，态度自然真实、俏皮亲近，听主人的话，绝不搞错称呼）\n"
                f"请结合当前对主人的极高好感度自然回复（20~35 字，绝不用句号）："
            )
    elif is_at or is_mention:
        user_prompt = (
            f"{identity_note}"
            f"群友「{sender_nick}」发来消息：\n{timeline_text}\n\n"
            f"{user_mem_prompt}"
            f"请以大肥鱼口吻极简回复（默认1句，最多2句，绝不用句号，别写小作文）："
        )
    else:
        user_prompt = (
            f"{identity_note}"
            f"群友在群聊中的近期发言：\n{timeline_text}\n\n"
            f"【旁观规则（最高优先级）】：\n"
            f"1. 你正在旁观。若群友是在私聊互动、打游戏开黑/组队、讨论内部黑话、或你没有十足把握理解其前因后果，【必须直接且仅输出 [PASS] 彻底闭嘴】；\n"
            f"2. 严禁不懂装懂、严禁答非所问、严禁发出反问套话（如「你在说什么呀」、「我也打不开」）；\n"
            f"3. 只有当你 100% 搞懂了话题并能给出极其一针见血的 1 句短接话时才说；插不上话输出 [PASS]："
        )
    
    # 联网检索增强：当对话涉及最新时效词且群开启搜索时，自动注入轻量网页搜索上下文
    web_search_context = ""
    cfg = bot.get_group_config(group_id)
    if cfg.get("enable_search", True) and bot.http_session:
        time_keywords = ("今天", "最新", "热搜", "发布会", "刚刚", "最近发生", "发生什么事", "有什么新闻")
        if any(kw in timeline_text for kw in time_keywords):
            # 提取简短检索关键词
            m_q = re.search(r'(?:今天|最新|刚刚|查查|搜搜|看下)?\s*([^\n，。？！]{3,20})', timeline_text)
            query_str = m_q.group(1).strip() if m_q else timeline_text[-20:].strip()
            if query_str:
                search_res = await connectors.search_web_brief(bot.http_session, query_str, limit=2)
                if search_res:
                    snippets_str = " | ".join([f"{r['title']}: {r['snippet']}" for r in search_res])
                    web_search_context = f"\n【实时联网搜索参考（仅供知识辅助，依然保持自身人设）】：{snippets_str}\n"

    # System Prompt 保持绝对静态：热梗、面板指令这些随消息变的内容全部走 user 末尾，
    # 这样每次请求的前缀一致，服务端不用反复重算开头那一段
    system_content = bot.sanitize_text(bot.ROOT_SYSTEM_PROMPT if is_root_work else bot.SYSTEM_PROMPT)

    # 随消息变化的上下文，统一挂在 user 消息末尾
    dynamic_context = ""
    if not is_root_work:
        # 本群最近说过的话回喂给模型，光靠代码削句子治不了「换个说法还是同一个梗」
        if group_id:
            repeat_hint = bot.get_anti_repeat_hint(group_id)
            if repeat_hint:
                dynamic_context += f"\n{repeat_hint}\n"
        memes_context = bot.meme_manager.get_prompt_snippet(timeline_text)
        if memes_context:
            dynamic_context += f"\n{memes_context}\n"
        # 面板指令两千多字符，只有真在问用法时才带，平时一个字都不发
        if bot.needs_games_kb(timeline_text):
            dynamic_context += f"\n【云崽指令清单（群友在问用法，按这个答）】：\n{bot.GAMES_KB}\n"
    # 群文件上下文增强（当群友询问群文件时，仅拉取文件列表标题与元信息，绝对不读文件正文）
    group_files_context = ""
    if group_id and bot.current_ws and any(kw in timeline_text for kw in ("群文件", "文件列表", "群里有没有", "群里发过", "发群文件", "找文件")):
        files_summary = await bot.get_group_files_brief(bot.current_ws, group_id)
        if files_summary:
            group_files_context = f"\n【本群文件列表（仅供参考文件名与上传人，未读取文件正文）】：\n{files_summary}\n"

    user_prompt = bot.sanitize_text(user_prompt + web_search_context + group_files_context)
    # 注入跨群公共话题记忆（仅供认知背景，不破坏当前群独立性）
    if group_id:
        cross_topics_hint = cross_memory.cross_group_mgr.get_recent_topics_prompt(exclude_group_id=group_id, limit=3)
        if cross_topics_hint:
            user_prompt = bot.sanitize_text(user_prompt + cross_topics_hint)
    if dynamic_context:
        user_prompt = bot.sanitize_text(user_prompt + dynamic_context)
    max_tokens_val = 1500 if is_root_work else bot.MAX_OUTPUT_TOKENS
    temperature_val = 0.75 if is_root_work else 0.8
    payload = {
        "model": bot.MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt}
        ],
        "max_tokens": max_tokens_val,
        "temperature": temperature_val
    }
    
    if bot.http_session is None or bot.http_session.closed:
        bot.http_session = aiohttp.ClientSession()

    t0 = time.time()
    try:
        async with bot.http_session.post(bot.SILICONFLOW_API_URL, headers=headers, json=payload, timeout=20) as resp:
            elapsed = time.time() - t0
            if resp.status != 200:
                err_text = await resp.text()
                logger.error("API 调用失败 (%d, 耗时 %.2fs): %s", resp.status, elapsed, err_text)
                return None
            data = await resp.json()
            if "usage" in data:
                bot.token_tracker.record_usage(data["usage"], bot.MODEL_NAME, group_id=group_id)
                # 上游支持前缀缓存时会回报命中数；没有就说明这段前缀白写了
                usage = data["usage"]
                hit = usage.get("prompt_cache_hit_tokens", usage.get("cached_tokens"))
                if hit is not None:
                    logger.info("前缀缓存命中 %s / 输入 %s tokens", hit, usage.get("prompt_tokens"))
            reply = data["choices"][0]["message"]["content"].strip()
            logger.info("模型生成完成 (耗时 %.2fs): %s", elapsed, reply[:30])
            return reply
    except asyncio.CancelledError:
        logger.info("DeepSeek 请求被中止。")
        raise
    except Exception as e:
        logger.error("调用大模型异常 (耗时 %.2fs): %s", time.time() - t0, e)
        return None

async def call_deepseek_poke(sender_nick: str, is_admin: bool, group_id: Optional[int] = None) -> Optional[str]:
    """戳一戳的回应。

    为了让一句「戳我干嘛」不背着两千多字的 System Prompt 去烧一次 API，
    普通群友直接走本地语料；只有 root 模式才真的问模型（那里要判断有没有活干）。
    """
    channel_mode = bot.get_channel_mode(group_id)
    is_root_work = bool(is_admin and channel_mode == "root")

    if not is_root_work:
        corpus = bot._POKE_ADMIN_QUIPS if is_admin else bot._POKE_QUIPS
        quip = random.choice(corpus)
        logger.info("戳一戳本地回应（不走模型）: %s", quip)
        return quip

    headers = {
        "Authorization": f"Bearer {bot.SILICONFLOW_API_KEY}",
        "Content-Type": "application/json"
    }
    user_prompt = (
        "主人在群里戳了你一下。当前是 root 工作模式，简明回应主人有什么任务要安排（1 句，10~20 字，严禁演戏）："
    )
    sys_content = bot.ROOT_SYSTEM_PROMPT
    payload = {
        "model": bot.MODEL_NAME,
        "messages": [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": user_prompt}
        ],
        "max_tokens": 60,
        "temperature": 0.7
    }
    
    if bot.http_session is None or bot.http_session.closed:
        bot.http_session = aiohttp.ClientSession()

    t0 = time.time()
    try:
        async with bot.http_session.post(bot.SILICONFLOW_API_URL, headers=headers, json=payload, timeout=15) as resp:
            elapsed = time.time() - t0
            if resp.status != 200:
                logger.error("戳一戳 API 调用失败 (%d, 耗时 %.2fs)", resp.status, elapsed)
                return None
            data = await resp.json()
            if "usage" in data:
                bot.token_tracker.record_usage(data["usage"], bot.MODEL_NAME, group_id=group_id)
            reply = data["choices"][0]["message"]["content"].strip()
            logger.info("戳一戳动态响应完成 (耗时 %.2fs): %s", elapsed, reply[:30])
            return reply
    except Exception as e:
        logger.error("戳一戳调用大模型异常: %s", e)
        return None

async def evaluate_favor_with_ai(user_id: int, sender_nick: str, dialogue_snippet: str, bot_reply: str = "", group_id: Optional[int] = None) -> Tuple[int, str]:
    """请求 SiliconFlow 大模型动态分析群友真实态度并给出好感变化 delta 与印象"""
    if not dialogue_snippet.strip():
        return 0, ""
        
    headers = {
        "Authorization": f"Bearer {bot.SILICONFLOW_API_KEY}",
        "Content-Type": "application/json"
    }
    
    sys_prompt = (
        "你是群聊好感度判定器。根据群友发言和小鲸鱼的回复，判断该群友对小鲸鱼的真实态度。\n"
        "只输出 JSON：{\"delta\": -5~5 的整数, \"impression\": 10 字以内印象，没有就填 null}\n"
        "扣分：骂人、叫大肥鱼、阴阳怪气、找茬对线、想吃鱼。\n"
        "0 分：普通水群、就事论事的闲聊，绝不无脑加分。\n"
        "加分：友好调侃 +1，夸奖投喂 +2，表白 +4。"
    )
    user_prompt = f"群友「{sender_nick}」发言：{dialogue_snippet}\n小鲸鱼回复：{bot_reply or '（无）'}"
    
    payload = {
        "model": bot.MODEL_NAME,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 80,
        "temperature": 0.1
    }
    
    try:
        if bot.http_session is None or bot.http_session.closed:
            bot.http_session = aiohttp.ClientSession()
        async with bot.http_session.post(bot.SILICONFLOW_API_URL, headers=headers, json=payload, timeout=8) as resp:
            if resp.status == 200:
                data = await resp.json()
                if "usage" in data:
                    bot.token_tracker.record_usage(data["usage"], bot.MODEL_NAME, group_id=group_id)
                raw_text = data["choices"][0]["message"]["content"].strip()
                m = re.search(r"\{.*?\}", raw_text, re.DOTALL)
                if m:
                    res_json = json.loads(m.group(0))
                    delta = int(res_json.get("delta", 0))
                    delta = max(-5, min(5, delta))
                    impression = str(res_json.get("impression") or "").strip()
                    if impression.lower() in ["null", "none", "无"]:
                        impression = ""
                    return delta, impression
    except Exception as e:
        logger.warning("AI 动态好感度判定异常: %s", e)
    return 0, ""

def _is_favor_worthy(text: str) -> bool:
    """这句话值不值得花一次判定请求"""
    if not text or not text.strip():
        return False
    plain = re.sub(r"\[CQ:[^\]]*\]", "", text).strip()
    if len(plain) < 4:
        return False
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", plain):
        return False
    # 哈哈哈 / 66666 这种复读，情绪信息量为零
    if len(set(plain)) <= 2:
        return False
    return plain not in bot._FAVOR_NOISE

async def update_user_memory_async(user_id: int, sender_nick: str, dialogue_snippet: str, group_id: Optional[int] = None, bot_reply: str = ""):
    """由 AI 大模型异步动态判定群友的好感度变化与印象画像（按群隔离）"""
    if user_id == bot.BOT_QQ:
        return
    # 纯水聊/开黑噪音不打分，省掉一次完整请求
    if not _is_favor_worthy(dialogue_snippet) and dialogue_snippet != "戳了戳小鲸鱼":
        return

    key = (group_id, user_id)
    now_ts = time.time()
    if now_ts - bot._favor_last_run.get(key, 0.0) < bot.FAVOR_MIN_INTERVAL:
        buf = bot._favor_pending.setdefault(key, [])
        buf.append(dialogue_snippet)
        del buf[:-5]  # 只留最近 5 句，防止长期不说话时无限攒
        return
    pending = bot._favor_pending.pop(key, [])
    if pending:
        dialogue_snippet = "；".join(pending[-2:] + [dialogue_snippet])
    bot._favor_last_run[key] = now_ts

    mem = bot.get_or_create_user_memory(user_id, sender_nick, group_id=group_id)
    
    # 0. 特殊固定动作快速通道：戳一戳
    if dialogue_snippet == "戳了戳小鲸鱼":
        favor_delta = 1
        new_note = "喜欢戳小鲸鱼尾巴互动"
    else:
        # 1. 委托大模型深度理解真实语境并给出动态判定
        favor_delta, new_note = await evaluate_favor_with_ai(user_id, sender_nick, dialogue_snippet, bot_reply=bot_reply, group_id=group_id)

    # 主人特殊保护：如果用户是专属主人兼开发者，不录入外部混乱标签，好感度保底 80，保持最高默契
    if user_id == bot.ADMIN_QQ:
        mem["nickname"] = "life"
        mem["traits"] = "主人兼创造者"
        mem["notes"] = ["专属主人兼创造者 life"]
        old_favor = mem.get("favor", 80)
        mem["favor"] = max(80, min(100, old_favor + max(0, favor_delta)))
        mem["updated_at"] = time.time()
        logger.info("[好感判定] 主人(%s) 好感维持: %d/100 (免脏标签污染)", bot.ADMIN_QQ, mem["favor"])
        async with bot.memory_write_lock:
            bot.save_user_memories()
        return

    old_favor = mem.get("favor", 50)
    mem["favor"] = max(0, min(100, old_favor + favor_delta))
    if new_note and new_note not in mem["notes"]:
        mem["notes"].append(new_note)
        # 记忆条目与字符预算自愈引擎 (源自 dsh-companion 协议)：
        # 当条目超过 6 条或总字符超过 120 字时，自动合并压缩为高密度事实
        total_chars = sum(len(n) for n in mem["notes"])
        if len(mem["notes"]) > 6 or total_chars > 120:
            compact_notes = "；".join(mem["notes"])
            mem["notes"] = [compact_notes[:100]]  # 截取提炼高密度关键印象，永不超额
            
    mem["updated_at"] = time.time()
    logger.info("[好感判定] 群 %s 用户 %s(%s): delta=%+d, impression=%r, 好感度: %d -> %d/100", 
                group_id or "私聊", sender_nick, user_id, favor_delta, new_note, old_favor, mem["favor"])
    async with bot.memory_write_lock:
        bot.save_user_memories()

