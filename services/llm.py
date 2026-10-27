# -*- coding: utf-8 -*-
"""调模型：群聊回复、戳一戳、好感度判定。

提示词在 prompts.py，全局状态在 bot 里，这里只负责发请求和解析结果。
"""

from meme_manager import meme_manager
import connectors
import cross_interaction
import cross_memory
import memory_atoms

from typing import Optional
from typing import Tuple
import aiohttp
import asyncio
import json
import logging
import random
import re
import time
import datetime

# 由 bot.py 在启动时注入自己。直接 import bot 在 `python bot.py` 下会拿到
# 另一个模块实例（__main__ 与 bot 各一份），全局状态就分裂了。
bot = None


def bind(host):
    global bot
    bot = host

logger = logging.getLogger("qqbot.llm")

async def call_deepseek(timeline_text: str, is_at: bool, is_admin: bool = False, is_private: bool = False, is_mention: bool = False, target_uid: Optional[int] = None, sender_nick: str = "", group_id: Optional[int] = None, is_follow_up: bool = False, latest_line: str = "", extra_system_instruction: str = "") -> Optional[str]:
    """请求 SiliconFlow DeepSeek-V3 模型"""
    headers = {
        "Authorization": f"Bearer {bot.SILICONFLOW_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # 42~59行原先在开头放动态精确分秒，现前移到 prompt 末尾，保证通用身份规则为完全静态前缀
    identity_note = f"（认知基础：你在 QQ 上的名字/昵称叫「小鲸鱼」或「鲸宝」，你的 QQ 号是 {bot.BOT_QQ}，你叫 life bot fish 是因为部署账号的昵称，那也是你！严禁把自己的名字当成别人或山寨货）\n"
    # 调用方没单独递当前这句时，退化成时间线最后一行，避免提示词里出现一对空引号
    if not latest_line and timeline_text.strip():
        latest_line = timeline_text.strip().splitlines()[-1].strip()

    # 群友记忆与好感度上下文注入
    user_mem_prompt = ""
    if target_uid and target_uid != bot.BOT_QQ:
        mem = bot.get_or_create_user_memory(target_uid, sender_nick, group_id=group_id)
        if target_uid == bot.ADMIN_QQ:
            user_mem_prompt = f"【当前交谈者身份】：专属主人兼创造者 life（QQ {bot.ADMIN_QQ}），拥有绝对最高权限与天然极高好感度。\n"
        else:
            # 印象按「还剩多少分量 + 有多新」排序取，装进字数预算就停。
            # 老数据里的字符串会自动拆成原子，不用手动迁移。
            notes_str, used_notes = memory_atoms.render(mem.get("notes") or [])
            if used_notes:
                memory_atoms.mark_used(mem["notes"], used_notes)
            if not notes_str:
                notes_str = "初次接触"

            # 检索历史深层关联记忆（BM25 + 时间衰减召回，借鉴自 DSH）
            recalled_mem_str = ""
            try:
                from services.fish_memory import fish_memory_engine
                recalled = fish_memory_engine.recall(str(group_id or "private"), str(target_uid), latest_line, top_k=2)
                if recalled:
                    items_str = "；".join([f"[{r['type']}]: {r['content']}" for r in recalled])
                    recalled_mem_str = f"【唤醒的过往具体旧事与偏好（若与当前话题相关可自然接梗，严禁死板复读）】：{items_str}\n"
            except Exception as e:
                logger.debug("召回深层记忆异常: %s", e)

            # 根据好感度（0~100点）动态生成无极连续的态度与互动指导
            favor_score = max(0, min(100, int(mem.get("favor", 50))))
            
            # 连续计算包容度、主动热络度与警惕度
            warmth_pct = favor_score  # 0% ~ 100%
            milestone_hint = ""
            last_ms = mem.get("last_milestone")
            last_ms_at = mem.get("last_milestone_at", 0)
            if last_ms and (time.time() - last_ms_at < 3600.0 * 2):  # 2小时内刚发生过里程碑跨越
                if "上升突破_80" in last_ms:
                    milestone_hint = "（注意：该群友近期好感度刚刚突破80进入死党挚友阶段！可以表现出稍微有些害羞但暗自欣喜的傲娇反差）"
                elif "下跌跌破_40" in last_ms:
                    milestone_hint = "（注意：该群友近期好感度刚刚跌破40进入嫌弃戒备阶段！可以表现出一丝失望或更刺骨的冷言冷语）"

            if favor_score >= 85:
                closeness_desc = "极其深厚（死党/专属挚友），带有明显的偏爱、顺从与宠溺"
                tone_desc = "无话不谈、极具包容心。哪怕对方开过头玩笑也会默契接梗或娇嗔撒娇，主动热切，愿意给出情绪价值，绝不阴阳对方"
            elif favor_score >= 70:
                closeness_desc = "熟络交好（同桌好友/铁哥们），相处自然默契"
                tone_desc = "态度大方热情、接梗飞快，开得起玩笑，互损也是朋友间轻松调侃，乐于分享和倾听"
            elif favor_score >= 55:
                closeness_desc = "略显亲近（熟人群友），相处融洽"
                tone_desc = "态度和善友好，聊天愿意多说两句，较少出现攻击性，属于能聊得来的群友"
            elif favor_score >= 45:
                closeness_desc = "标准中立（普通群友），社交距离分明"
                tone_desc = "就事论事、平平淡淡，礼貌和气但不主动倒贴热脸；对方友好则正常聊，对方冒犯则就事反弹"
            elif favor_score >= 30:
                closeness_desc = "轻度冷淡（有些嫌弃/无感），防备心较重"
                tone_desc = "略显冷淡敷衍，字数减少，懒得多解释，不太爱搭理对方"
            elif favor_score >= 15:
                closeness_desc = "明显厌烦（嫌弃排斥/戒备），极度不耐烦"
                tone_desc = "态度冰冷刺骨、惜字如金（甚至用「哦」「...」「有事？」打发），不给好脸色，严厉拆台"
            else:
                closeness_desc = "极度厌恶（拉黑级敌对/水火不容）"
                tone_desc = "彻底拒绝友好交流，厌恶写在脸上，直接吐露嫌弃（「离我远点」「滚」「少来沾边」），毫不留情"

            favor_guideline = (
                f"【好感度状态：{favor_score}/100 点（动态亲密度指数: {warmth_pct}%）{milestone_hint}】\n"
                f"- 当前关系定性：{closeness_desc}（数值越高越温柔偏爱，数值越低越冷淡嫌弃）。\n"
                f"- 动态言行指引：{tone_desc}。\n"
                f"- 连续约束：你的热情程度、回复字数与包容上限必须严格与该分值（{favor_score}点）保持一致动态变化。"
            )

            user_mem_prompt = (
                f"【当前交谈群友信息】：昵称: {mem.get('nickname')}\n"
                f"{favor_guideline}\n"
                f"【你对他的背景认知（只影响你跟他说话松不松，不要变成台词，一句都别复读）】：{notes_str}\n"
                f"{recalled_mem_str}"
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
                f"【当前交谈者身份】：专属主人 life（QQ {bot.ADMIN_QQ}）\n"
                f"以下是你们的历史对话记录：\n{timeline_text}\n\n"
                f"【当前这句】：「{latest_line}」\n"
                f"你现在表面傲娇嘴硬、爱嘀咕两句小情绪，但心里其实很在乎依赖主人、宠着主人乖乖听话。"
                f"不确定或不懂就直接说不知，严禁瞎猜胡编，严禁任何括号描写，像手机聊天随手回（1~2句，绝不用句号）："
            )
        else:
            user_prompt = (
                f"{identity_note}"
                f"以下是你与专属主人「{sender_nick}」(QQ {bot.ADMIN_QQ}) 的私聊历史：\n{timeline_text}\n\n"
                # 记忆块放时间线后面：历史是累加的公共前缀，放前面才能被缓存命中
                f"{user_mem_prompt}"
                f"【主人发来私信】：\n"
                f"当前跟你说话的是你的专属主人兼创造者。请清楚认同主人身份，态度自然真实、听主人的话，结合好感度自然交流，严禁括号描写（20~40 字）："
            )
    elif is_admin and (is_at or is_follow_up):
        if channel_mode == "root":
            user_prompt = (
                f"{identity_note}"
                f"以下是当前群聊对话记录：\n{timeline_text}\n\n"
                f"【主人「{sender_nick}」(QQ {bot.ADMIN_QQ}) 这句话】：「{latest_line}」\n"
                f"当前是专属主人的 /root 模式。面对主人，你要体现出纯正女孩子的傲娇反差：表面嘴硬爱嘀咕，但心里特别听主人话、软萌乖巧顺从！绝非恶毒攻击，更不是窝囊受气。\n"
                f"不确定或没看懂就直接问，别瞎猜脑补；严禁任何括号描写，像真人群聊随手回（1~2句，绝不用句号）："
            )
        else:
            follow_up_note = (
                f"他这是接着你们正在聊的事往下说，不是新开一轮。\n"
                if is_follow_up else
                f"他是在跟你说这句话本身，不是在报个到。\n"
            )
            user_prompt = (
                f"{identity_note}"
                f"以下是当前群聊对话记录：\n{timeline_text}\n\n"
                f"{user_mem_prompt}"
                f"【注意识别对话者身份】：当前是你的专属主人兼创造者 life (QQ {bot.ADMIN_QQ}) 在群里跟你说话。\n"
                f"（清楚知道对方是你的主人 life，态度自然真实、俏皮亲近，听主人的话，绝不搞错称呼）\n"
                f"{follow_up_note}"
                f"【主人说的这句】：「{latest_line}」\n"
                f"对这句话本身作反应，别把注意力放在「他来找我了」上；看不懂他想说啥就直接问他这句什么意思，"
                f"别另起话题、也别再讲一遍你上一轮说过的话。\n"
                f"语气可以是黏人的：撒个娇、卖个乖都行，但严禁出现任何括号动作旁白描写（如（甩尾巴）等彻底禁用）；"\
                f"严禁把「拔电源」「敲代码」「我是AI」当梗挂嘴边；"\
                f"（20~35 字，绝不用句号）："
            )
    elif is_at or is_mention or is_follow_up:
        follow_up_note = (
            "他刚 @ 过你，这句是接着上面那茬往下说，不是新话题。不许沉默、不许输出 [PASS]，"
            "看不懂就直接问他这句什么意思。\n"
            if is_follow_up else ""
        )
        # 时间线只当背景，必须把当前这句单独钉出来：只丢上下文时模型常抓上一句来答
        #（群里「不要」被答成上一轮的「可爱」就是这毛病）
        user_prompt = (
            f"{identity_note}"
            f"群友「{sender_nick}」在群里说了这些：\n{timeline_text}\n\n"
            f"{user_mem_prompt}"
            f"{follow_up_note}"
            f"【他最后这句】：「{latest_line}」\n"
            f"上面的记录只是前情，你只对他最后这句作反应，别去答前面别人说过的话；"
            f"他要是只回你半句（「不要」「对的」这种），先接住这半句，再联系前情把话接圆；"
            f"【态度与好感铁律】：你的语气和互动必须严格服从上方【好感度状态】的指示！好感高（80+）时必须明显展现亲切偏爱、包容热络；好感低（<40）时必须冷淡戒备；"
            f"【对线骂人反击铁律】：平时聊天和和气气，但一旦对方主动挑衅找茬、辱骂你（如骂傻逼/弱智/脑瘫/废物等）或攻击主人，立刻撕掉伪装迎头痛击！坚决杜绝文绉绉的拐弯抹角阴阳怪气，用真实老网民的力道迎头喷回去，用常用骂人谐音或网络嘴替狠狠回击（如「脑子进水了是吧」「煞笔玩意叫什么叫」「小脑萎缩就去挂号」），寸步不让骂到对方闭嘴；"
            f"遇到不确定、没听过或没看懂的，严禁瞎猜胡编，直接坦荡说不知道/看不懂；"
            f"要是拿具体问题考你（算术、常识、时间、数字），先把答案说出来，别糊弄过去；"
            f"严禁输出任何动作括号，随手打字（默认1句，最多2句，绝不用句号，别写小作文）："
        )
    else:
        user_prompt = (
            f"{identity_note}"
            f"群友在群聊中的近期发言：\n{timeline_text}\n\n"
            f"【旁观与自主思考接话规则（最高优先级）】：\n"
            f"1. 你正在旁观群聊。没有固定关键词限制，完全凭你的智能自主决策：\n"
            f"   - 如果群友在提及你、聊你（包括各种昵称/代词/谐音/找你），或者当前话题极其契合你、你完全懂并且能给出极其绝妙幽默的一句短吐槽，请自然加入对话；\n"
            f"   - 如果群友是在私聊互动、打游戏开黑/组队、讨论生僻业务工作、或你没有把握理解，【必须直接且仅输出 [PASS] 彻底闭嘴】；\n"
            f"2. 若近期发言附带群友分享的【多媒体/视频/GitHub/网页卡片信息】：\n"
            f"   - 必须先自然引用卡片中的关键信息进行省流提炼，然后附带 1~2 句极其机智、俏皮、切中要害的老网民风格短评/神吐槽（切忌假大空的客套话）；\n"
            f"3. 若近期发言附带群友发的图片/表情包：\n"
            f"   - 只有当该图片是非常有趣、有明显槽点、有强烈接梗点的梗图/表情包，且你能给出极其精准幽默的神吐槽时，才回 1 句简短吐槽；\n"
            f"   - 若只是日常打卡、普通截图、工作学习、系统提示或你觉得没必要评价，【必须输出 [PASS] 静默】；\n"
            f"4. 严禁不懂装懂、严禁答非所问、严禁发出反问套话（如「你在说什么呀」、「我也打不开」）；\n"
            f"5. 只有当你完全搞懂话题并能给出极其一针见血的 1 句短接话时才说；插不上话输出 [PASS]；\n"
            f"6. 严禁催问式空话（「你倒是把问题问出来」「刚问啥了」这种），宁可 [PASS] 也别拿废话占屏："
        )

    # 联网检索增强：当对话涉及最新时效词或求证网络热梗且群开启搜索时，自动注入轻量网页搜索上下文
    web_search_context = ""
    cfg = bot.get_group_config(group_id)
    if cfg.get("enable_search", True) and bot.http_session:
        time_keywords = ("今天", "最新", "热搜", "发布会", "刚刚", "最近发生", "发生什么事", "有什么新闻")
        meme_keywords = ("什么梗", "啥梗", "什么意思", "啥意思", "指什么", "代表什么", "怎么来的", "是什么梗", "是谁啊", "何意啊", "谁懂", "绝了", "抽象", "绷不住", "救命")
        is_time_search = any(kw in timeline_text for kw in time_keywords)
        is_meme_search = any(kw in timeline_text for kw in meme_keywords)
        
        # 智能探测：当发言很短（10字以内）、不是普通问候/闲聊、且没有命中本地任何已知梗词时，做一次静默热梗探测
        is_unknown_potential_meme = False
        last_clean = re.sub(r'\[CQ:[^\]]+\]', '', timeline_text.split('\n')[-1]).strip()
        # 排除常见基础寒暄
        common_greetings = ("你好", "在吗", "早安", "晚安", "在干嘛", "早上好", "晚上好", "嗨", "哈喽", "谢谢", "好的")
        local_has_meme = bool(bot.meme_manager.get_prompt_snippet(timeline_text))
        if not local_has_meme and 3 <= len(last_clean) <= 12 and not any(g in last_clean for g in common_greetings):
            # 包含游戏、特定名词结构或口头禅特征
            if any(c in last_clean for c in ("瓦", "桥", "哥", "姐", "酱", "牢", "神", "王", "圣", "急", "典", "乐", "批", "门")):
                is_unknown_potential_meme = True

        query_str = ""
        if is_meme_search:
            # 针对问梗场景提取核心词：如「疯狂星期四是什么梗」->「疯狂星期四 梗」
            m_m = re.search(r'([^，。？！\n\s]{2,15}?)(?:是什么梗|啥梗|什么梗|是什么意思|啥意思|指什么|是啥意思)', timeline_text)
            if m_m:
                query_str = f"{m_m.group(1).strip()} 梗"
            else:
                m_q = re.search(r'([^，。？！\n]{2,15})', timeline_text[-25:])
                query_str = f"{m_q.group(1).strip()} 梗" if m_q else ""
        elif is_unknown_potential_meme:
            # 未知疑似抽象/游戏梗探测
            query_str = f"{last_clean} 梗 为什么"
        elif is_time_search:
            m_q = re.search(r'(?:今天|最新|刚刚|查查|搜搜|看下)?\s*([^，。？！\n]{3,20})', timeline_text)
            query_str = m_q.group(1).strip() if m_q else timeline_text[-20:].strip()
            
        if query_str:
            search_res = await connectors.search_web_brief(bot.http_session, query_str, limit=2)
            if search_res:
                snippets_str = " | ".join([f"{r['title']}: {r['snippet']}" for r in search_res])
                web_search_context = f"\n【实时联网搜索参考（仅供知识辅助，依然保持自身人设）】：{snippets_str}\n"

    # System Prompt 保持不变：热梗、面板指令这些随消息变的内容全部走 user 末尾，
    # 这样每次请求的前缀一致，服务端不用反复重算开头那一段
    system_content = bot.sanitize_text(bot.ROOT_SYSTEM_PROMPT if is_root_work else bot.SYSTEM_PROMPT)

    # 随消息变化的上下文，统一挂在 user 消息末尾
    dynamic_context = ""
    if not is_root_work:
        # 注入动态网络热词轮换池（100条/30分钟轮换）与群友命中梗
        memes_context = bot.meme_manager.get_prompt_snippet(timeline_text)
        if memes_context:
            dynamic_context += f"\n{memes_context}\n"
        # 本群最近说过的话回喂给模型，光靠代码削句子治不了「换个说法还是同一个梗」
        if group_id:
            repeat_hint = bot.get_anti_repeat_hint(group_id)
            if repeat_hint:
                dynamic_context += f"\n{repeat_hint}\n"
        # 面板指令两千多字符，只有真在问用法时才带，平时一个字都不发
        if bot.needs_games_kb(timeline_text):
            dynamic_context += f"\n【云崽指令清单（群友在问用法，按这个答）】：\n{bot.GAMES_KB}\n"
    # 群文件上下文增强（当群友询问群文件时，仅拉取文件列表标题与元信息，不读文件正文）
    group_files_context = ""
    if group_id and bot.current_ws and any(kw in timeline_text for kw in ("群文件", "文件列表", "群里有没有", "群里发过", "发群文件", "找文件")):
        files_summary = await bot.get_group_files_brief(bot.current_ws, group_id)
        if files_summary:
            group_files_context = f"\n【本群文件列表（仅供参考文件名与上传人，未读取文件正文）】：\n{files_summary}\n"

    user_prompt = bot.sanitize_text(user_prompt + web_search_context + group_files_context)
    # 注入当前时间戳（挂在 user prompt 末尾，确保开头固定身份 prompt 命中最长前缀缓存 KV Cache）
    now_dt = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    hour = now_dt.hour
    if 5 <= hour < 9:
        period_str = "清晨/早晨"
    elif 9 <= hour < 12:
        period_str = "上午"
    elif 12 <= hour < 14:
        period_str = "中午"
    elif 14 <= hour < 18:
        period_str = "下午"
    elif 18 <= hour < 23:
        period_str = "晚上"
    else:
        period_str = "深夜/凌晨"
    time_hint = f"\n【当前真实系统时间】：{now_dt.strftime('%Y-%m-%d %H:%M')} 星期{['一','二','三','四','五','六','日'][now_dt.weekday()]}（时段: {period_str}）\n"
    user_prompt = bot.sanitize_text(user_prompt + time_hint)
    # 注入跨群公共话题记忆（仅供认知背景，不破坏当前群独立性）
    if group_id:
        cross_topics_hint = cross_memory.cross_group_mgr.get_recent_topics_prompt(exclude_group_id=group_id, limit=2)
        if cross_topics_hint:
            user_prompt = bot.sanitize_text(user_prompt + cross_topics_hint)
    if dynamic_context:
        user_prompt = bot.sanitize_text(user_prompt + dynamic_context)
    if extra_system_instruction:
        user_prompt = bot.sanitize_text(user_prompt + f"\n【实时突发情境指令（请严格融入人设自由发挥）】：\n{extra_system_instruction}\n")
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
        attempt_prompt = user_prompt
        for attempt in (1, 2):
            payload["messages"][1]["content"] = attempt_prompt
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
            # 免@追问时若模型选择沉默，尊重其决定静默，不重复重试烧 token
            return reply
    except asyncio.CancelledError:
        logger.info("DeepSeek 请求被中止。")
        raise
    except Exception as e:
        logger.error("调用大模型异常 (耗时 %.2fs): %s", time.time() - t0, e)
        return None

async def call_deepseek_poke(sender_nick: str, is_admin: bool, group_id: Optional[int] = None, user_id: Optional[int] = None) -> Optional[str]:
    """戳一戳的回应。

    为了让一句「戳我干嘛」不背着两千多字的 System Prompt 去烧一次 API，
    普通群友直接按好感度阶梯取本地语料；只有 root 模式才真的问模型（那里要判断有没有活干）。
    """
    channel_mode = bot.get_channel_mode(group_id)
    is_root_work = bool(is_admin and channel_mode == "root")

    if not is_root_work:
        if is_admin:
            corpus = bot._POKE_ADMIN_QUIPS
        else:
            favor = 50
            if user_id:
                mem = bot.get_or_create_user_memory(user_id, sender_nick, group_id=group_id)
                favor = int(mem.get("favor", 50))
            if favor >= 75:
                corpus = bot._POKE_FAVOR_HIGH
            elif favor >= 40:
                corpus = bot._POKE_FAVOR_MID
            else:
                corpus = bot._POKE_FAVOR_LOW

        quip = random.choice(corpus)
        logger.info("戳一戳回应（好感分层命中）: %s", quip)
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
        "只输出 JSON：{\"delta\": -8~8 的整数, \"impression\": 10 字以内印象，没有就填 null}\n"
        "【好感度升降原则（务必果断拉开差距，拒绝僵化无感）】：\n"
        "- 扣分项：挑衅找茬、辱骂对线扣 -5~-8；阴阳怪气、恶意挑刺扣 -3~-5；叫大肥鱼/恶作剧扣 -1~-2。\n"
        "- 0 分：完全与小鲸鱼无关的路人日常打卡、水群。\n"
        "- 加分项：真诚互动/顺毛夸奖 +2~+3；投喂食物/维护小鲸鱼/热情送礼物 +4~+6；表白偏爱 +6~+8。"
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
                    delta = max(-8, min(8, delta))
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
    # 冷却周期内不重复跑，省 token（从 60 秒提升至 120 秒，且短句直接忽略）
    if now_ts - bot._favor_last_run.get(key, 0.0) < max(bot.FAVOR_MIN_INTERVAL, 120.0):
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

    # 4. 同步记录持久化流水账并自动捕获事实/偏好（借鉴 DSH）
    try:
        from services.fish_memory import fish_memory_engine
        fish_memory_engine.record_turn(
            group_id=str(group_id or "private"),
            person_id=str(user_id),
            user_text=dialogue_snippet,
            reply_text=bot_reply
        )
    except Exception as e:
        logger.debug("fish_memory 记录流水与捕获异常: %s", e)

    # 主人特殊保护：如果用户是专属主人兼开发者，不录入外部混乱标签，好感度保底 80，保持最高默契
    if user_id == bot.ADMIN_QQ:
        mem["nickname"] = "life"
        mem["traits"] = "主人兼创造者"
        mem["notes"] = memory_atoms.migrate(["专属主人兼创造者 life"])
        old_favor = mem.get("favor", 80)
        mem["favor"] = max(80, min(100, old_favor + max(0, favor_delta)))
        mem["updated_at"] = time.time()
        logger.info("[好感判定] 主人(%s) 好感维持: %d/100 (免脏标签污染)", bot.ADMIN_QQ, mem["favor"])
        async with bot.memory_write_lock:
            bot.save_user_memories()
        return

    old_favor = mem.get("favor", 50)
    new_favor = max(0, min(100, old_favor + favor_delta))
    mem["favor"] = new_favor
    
    # 好感度关键里程碑跨越检测与傲娇突破提醒（跨越 80 挚友、60 熟人、40 警惕、20 嫌弃）
    milestones = [20, 40, 60, 80]
    for ms in milestones:
        if (old_favor < ms <= new_favor) or (old_favor >= ms > new_favor):
            direction = "上升突破" if new_favor > old_favor else "下跌跌破"
            logger.info("[好感里程碑] 群 %s 用户 %s(%s) 好感度 %s %d 点大关！(当前: %d/100)",
                        group_id or "私聊", sender_nick, user_id, direction, ms, new_favor)
            # 在群友的 tags 印象中记录一次里程碑事件标签
            mem["last_milestone"] = f"{direction}_{ms}"
            mem["last_milestone_at"] = time.time()
            break

    # 印象：先结清这段时间的衰减，再按语义去重并入。别再拼接截断——
    # 那会把六条压成一句 100 字的串，既丢信息又把重复项永久固化。
    notes = memory_atoms.apply_decay(mem.get("notes") or [])
    notes = memory_atoms.add_note(notes, new_note)
    mem["notes"] = notes
            
    mem["updated_at"] = time.time()
    logger.info("[好感判定] 群 %s 用户 %s(%s): delta=%+d, impression=%r, 好感度: %d -> %d/100", 
                group_id or "私聊", sender_nick, user_id, favor_delta, new_note, old_favor, mem["favor"])
    async with bot.memory_write_lock:
        bot.save_user_memories()

