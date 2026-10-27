# -*- coding: utf-8 -*-
"""群消息入口：过滤、触发判定、调用模型、发送。

这里只做编排，状态和配置都在 bot 里。
"""

from typing import Optional
import aiohttp
import asyncio
import connectors
import cross_interaction
import cross_memory
import fun_features
import games
import media_resolver
import random
import re
import safety_guard
import time
from services import trace
from services import vision

# 由 bot.py 在启动时注入自己。直接 import bot 在 `python bot.py` 下会拿到
# 另一个模块实例（__main__ 与 bot 各一份），全局状态就分裂了。
bot = None


def bind(host):
    global bot
    bot = host


async def handle_group_message(ws: aiohttp.ClientWebSocketResponse, event: dict):
    """群消息主处理流水线"""
    group_id = event.get("group_id")
    sender_id = event.get("user_id")
    sender_nick = event.get("sender", {}).get("card") or event.get("sender", {}).get("nickname") or str(sender_id)
    sender_title = (event.get("sender", {}).get("title") or "").strip()
    sender_display = f"{sender_nick}【头衔: {sender_title}】" if sender_title else sender_nick
    raw_message = event.get("raw_message", "")
    
    if group_id not in bot.ALLOWED_GROUPS or sender_id == bot.BOT_QQ:
        return

    # 每条消息一个短 id，后续日志带上，排查时能串成一条线
    trace.begin(f"g{group_id}-{sender_id}")
        
    # 前置短路检查：如果机器人在该群处于禁言期（单人禁言或全员禁言），直接返回，不消耗 token
    if await bot.check_bot_banned_realtime(ws, group_id):
        bot.logger.debug("[群 %d] 机器人处于禁言保护期，忽略用户 %s 的消息，省下大模型 Token", group_id, sender_id)
        return
        
    is_admin = (sender_id == bot.ADMIN_QQ)
    now = time.time()

    # /off 总闸：群里除了主人，谁说话都不理（连 @ 也不回）
    if not is_admin and bot.get_group_config(group_id).get("muted"):
        return
    
    # 提取纯文本与精准 @ 判定（基于 OneBot 11 规范的 QQ 号精准判定）
    # 匹配针对机器人自己的 @ CQ 码（兼容任何可选属性）
    is_at = bool(re.search(rf"\[CQ:at,[^\]]*qq={bot.BOT_QQ}[^\]]*\]", raw_message))
    
    # 也检查 OneBot11 原始 message 数组节点（结构化消息最稳健）
    if not is_at and isinstance(event.get("message"), list):
        for seg in event["message"]:
            if isinstance(seg, dict) and seg.get("type") == "at":
                target_qq = str(seg.get("data", {}).get("qq", ""))
                if target_qq == str(bot.BOT_QQ):
                    is_at = True
                    break
                
    # 打印原始接收日志，方便精准观测
    bot.logger.info("[群 %d] 收到原始消息 raw_message=%r, is_at=%s", group_id, raw_message, is_at)

    # 引用回复机器人也算直接呼叫：后面 repo 问答、游戏指令、护栏都要用它，先算出来
    is_reply_to_bot = False
    if "reply" in event and isinstance(event["reply"], dict):
        is_reply_to_bot = (event["reply"].get("sender", {}).get("user_id") == bot.BOT_QQ)
    
    # 累加该群自上次机器人发言以来的消息条数
    bot.group_msg_count_since_bot[group_id] += 1

    # 捕获群聊中的图片进入两段式近期图片缓冲池，并尝试进行超轻量视觉转录
    m_imgs = re.findall(r'\[CQ:image,[^\]]*?url=([^,\]]+)', raw_message)
    if m_imgs:
        for u in m_imgs:
            u_clean = u.replace("&amp;", "&")
            bot.group_recent_images[group_id].append((u_clean, time.time(), sender_id, sender_nick))

    # 解析引用消息与合并转发
    reply_prefix = await bot.extract_reply_context(ws, event, raw_message)
    raw_message = await bot.expand_forward_messages(ws, raw_message)
    if reply_prefix and "[CQ:image" in reply_prefix:
        raw_message = f"{reply_prefix}\n{raw_message}"
        reply_prefix = None
        # 重新扫描图片
        m_imgs = re.findall(r'\[CQ:image,[^\]]*?url=([^,\]]+)', raw_message)

    # 只要消息中含有图片（包括群友发图、表情包、或者@机器人带图），均尝试通过 Gemini 视觉模型解析画面
    # 策略优化：
    # 1. 如果消息本身携带图片（m_imgs）且被 @/引用/追问中/或在主动看图，则直接对该图进行视觉解析！
    # 2. 如果消息没有图片，但包含“这图/看图/什么番/识图”等回溯词，才从近期图片池（group_recent_images）找上一张图解析。
    vision_desc = None
    session = bot.get_http_session()
    target_img_url = None
    user_conv_key = (group_id, sender_id)
    follow_up_info = bot.user_follow_up_state.get(user_conv_key)
    has_active_follow_up = bool(follow_up_info and time.time() < follow_up_info.get("expire", 0.0) and follow_up_info.get("remain", 0) > 0)
    
    if m_imgs:
        # 当消息本身带有图片时：只要是找机器人的（@、引用、追问）或者主人发言，或者文本提到看图/分析/找番等，一律直接解析！
        if is_at or is_reply_to_bot or has_active_follow_up or is_admin or bool(re.search(r"(?:什么番|哪部番|谁画的|出处|搜番|搜截图|找番|看图|这图|图里|识图|分析|看下|看看|表情包)", raw_message)):
            target_img_url = m_imgs[0].replace("&amp;", "&")
    elif (is_at or is_reply_to_bot or has_active_follow_up or is_admin) and bool(re.search(r"(?:什么番|哪部番|谁画的|出处|搜番|搜截图|找番|看图|这图|图里|识图|分析|看下|看看|表情包)", raw_message)):
        # 消息本身不带图片，回溯上一张图片
        now_ts = time.time()
        for item in reversed(bot.group_recent_images[group_id]):
            if len(item) == 4:
                img_u, img_t, img_sid, img_snick = item
            else:
                img_u, img_t, img_snick = item[:3]
                img_sid = None
            if now_ts - img_t < 120.0:
                if (img_sid and img_sid == sender_id) or img_snick == sender_nick or not target_img_url:
                    target_img_url = img_u
                    if (img_sid and img_sid == sender_id) or img_snick == sender_nick:
                        break

    if target_img_url:
        bot.logger.info("[群 %d] 捕获图片，调用 Gemini 识图感知画面: %s", group_id, target_img_url[:60])
        vision_desc = await vision.describe_image(session, target_img_url)
        if vision_desc:
            bot.logger.info("[群 %d] Gemini 识图成功: %s", group_id, vision_desc[:60])

    # 记下群友之间的 @，模型接话时用来判断谁跟谁熟
    m_ats = re.findall(r'\[CQ:at,[^\]]*qq=(\d+)[^\]]*\]', raw_message)
    for target_qq_str in m_ats:
        try:
            t_qq = int(target_qq_str)
            if t_qq != bot.BOT_QQ:
                cross_interaction.record_interaction(group_id, sender_id, sender_nick, t_qq, target_qq_str)
        except Exception:
            pass

    # 深度解析主流媒体短链 (抖音/小红书/B站短链/纯BV号)、GitHub 仓库与通用网页链接 (http/https)
    # 注意：如果消息是在询问当前项目的仓库地址，直接交由后面的 repo_ask_reply 处理，不在此处解析，避免刚发完又自我解析
    media_info = None
    is_asking_own_repo = bool((is_at or is_reply_to_bot) and bot.repo_ask_reply(bot.clean_cq_codes(raw_message).strip()))
    if not is_asking_own_repo and bot.http_session and any(prefix in raw_message for prefix in ("http://", "https://", "b23.tv", "douyin.com", "xhslink.com", "BV", "bv", "github.com")):
        try:
            media_info = await media_resolver.parse_media_content(bot.http_session, raw_message)
            if media_info and media_info.get("summary"):
                raw_message = f"{raw_message}\n{media_info['summary']}"
                # 无论是否处于作息或群聊静默，检测到有效视频/GitHub解析结果立即自动发送一条省流卡片
                summary_text = media_info["summary"].strip()
                bot.logger.info("[群 %d] 自动命中并秒发媒体/仓库解析卡片 (%s)", group_id, media_info.get("type"))
                await bot.send_group_message(ws, group_id, summary_text)
        except Exception as e:
            bot.logger.warning("[群 %d] 媒体/仓库解析发送异常: %s", group_id, e)

    # 清洗掉针对机器人的 @ CQ 码以及其他无关 CQ
    clean_body = re.sub(rf"\[CQ:at,[^\]]*qq={bot.BOT_QQ}[^\]]*\]", "", raw_message)
    clean_body = bot.clean_cq_codes(clean_body).strip()
    # 移除 reply 的 CQ 码残留
    clean_body = re.sub(r"\[CQ:reply,id=-?\d+\]", "", clean_body).strip()

    # 如果有图片内容但用户没有附加文本，将内容作为描述体（显式标记发图人，杜绝多人群聊混淆）
    if vision_desc and not clean_body:
        clean_body = f"[【发送人: {sender_nick}】发送了图片/表情包]: {vision_desc}"
    elif vision_desc:
        clean_body = f"{clean_body}\n[【发送人: {sender_nick}】附带的图片/表情包画面描述]: {vision_desc}".strip()

    # 如果用户只 @ 了机器人，没有附带文字也没有图片
    if is_at and not clean_body:
        clean_body = "在吗"
    elif not clean_body:
        return

    # 游戏指令或云崽生态专用指令，交给云崽等扩展插件处理，AI 机器人主动跳过，彻底避免抢答与刷屏
    if bot.is_game_or_yunzai_command(clean_body):
        bot.logger.debug("[群 %d] 识别为游戏指令: %r，主动转交云崽处理，跳过 AI 回复", group_id, clean_body)
        return

    # 问项目出处/源码/部署：直接甩仓库，不走模型，省得绕圈子或者漏链接
    # 只在真的在问机器人时才算数——群里聊别的仓库不该被它插一嘴
    repo_reply = bot.repo_ask_reply(clean_body) if (is_at or is_reply_to_bot) else None
    if repo_reply:
        bot.logger.info("[群 %d] 命中仓库问答，直接回地址", group_id)
        await bot.send_segmented_group_message(
            ws, group_id, repo_reply, at_user_id=sender_id if is_at else None
        )
        return

    # 4. 安全护栏：拦截提示词注入/越狱指令，以傲娇拟真口吻反讽
    #    主人自己折腾人设不算攻击，放行让模型照做
    if not is_admin:
        is_injected, snarky_reply = safety_guard.check_prompt_injection(clean_body)
        if is_injected and snarky_reply:
            bot.logger.info("[群 %d] 拦截群友 %s 的提示词注入/越狱请求: %s", group_id, sender_nick, clean_body)
            # 怼人话术就一行，别走分段发图那条路，免得拦截完还甩一张表情包
            await bot.send_group_message(ws, group_id, snarky_reply, at_user_id=sender_id)
            return

    # 规范化指令前缀与小写，兼容全角斜杠与输入法大小写
    norm_cmd = clean_body.replace("／", "/").strip()
    norm_lower = norm_cmd.lower()

    # 异步自动感知与事实抽取流水（DSH 思想：聊天即感知，无需显式唤醒）
    try:
        from services.fish_memory import fish_memory_engine
        fish_memory_engine.record_turn(
            group_id=str(group_id),
            person_id=str(sender_id),
            user_text=clean_body
        )
    except Exception as e:
        bot.logger.debug("群消息自动感知异常: %s", e)

    # 公开指令检测: /memory 或 /memary（所有人均可查看好感度与记忆总结）
    if norm_lower.startswith(("/memary", "/memory")):
        await bot.handle_memory_summary_command(ws, group_id=group_id)
        return

    # 1. 轻量文字游戏（免状态：roll、抽签、抛硬币）拦截
    cfg = bot.get_group_config(group_id)
    if cfg.get("enable_games", True):
        quick_game_reply = games.handle_quick_game_commands(clean_body)
        if quick_game_reply:
            await bot.send_group_message(ws, group_id, quick_game_reply, at_user_id=sender_id)
            return

        # 2. 数字炸弹状态机拦截
        handled_bomb, bomb_reply = games.process_number_bomb(group_id, clean_body, sender_id, sender_nick)
        if handled_bomb and bomb_reply:
            await bot.send_group_message(ws, group_id, bomb_reply, at_user_id=sender_id)
            return

    # 3. 两段式搜番/搜动画截图出处拦截
    if re.search(r"(?:什么番|哪部番|谁画的|出处|搜番|搜截图|找番)", clean_body):
        # 寻找群内 90 秒内的有效图片（优先匹配提问者自己发过的图）
        now_ts = time.time()
        target_img_url = None
        for item in reversed(bot.group_recent_images[group_id]):
            if len(item) == 4:
                img_u, img_t, img_sid, _ = item
            else:
                img_u, img_t = item[0], item[1]
                img_sid = item[2] if len(item) >= 3 and isinstance(item[2], int) else None
            if now_ts - img_t < 90.0:
                if img_sid == sender_id or not target_img_url:
                    target_img_url = img_u
                    if img_sid == sender_id:
                        break
        if target_img_url and bot.http_session:
            anime_info = await connectors.search_anime_by_image(bot.http_session, target_img_url)
            if anime_info and not anime_info.get("error") and not anime_info.get("uncertain"):
                rep = (
                    f"帮你看出来啦，这是动画《{anime_info['title']}》\n"
                    f"第 {anime_info['episode']} 集 · 画面大概在 {anime_info['time']}\n"
                    f"（匹配相似度: {anime_info['similarity']}%）"
                )
                await bot.send_group_message(ws, group_id, rep, at_user_id=sender_id)
                return
            elif anime_info and anime_info.get("uncertain"):
                await bot.send_group_message(ws, group_id, f"好像是动画截图，但画面不够典型，我不太确信是不是《{anime_info.get('data', {}).get('anilist', {}).get('title', {}).get('native', '某部番')}》", at_user_id=sender_id)
                return
            elif anime_info and anime_info.get("error"):
                # 以前这里是静默 return，群友等半天没反应，以为机器人装死
                err = anime_info["error"]
                if err == "quota":
                    tip = "今天看图的额度被我造完了，明天再来（叉腰）"
                elif err == "noresult":
                    tip = "图我扒到了，但这不像是动画截图啊，你是不是发错图了"
                elif err == "download":
                    tip = "图扒不下来，QQ 那边拦我了，你重发一张试试"
                else:
                    tip = "搜番的服务今天抽风了，过会儿再问"
                await bot.send_group_message(ws, group_id, tip, at_user_id=sender_id)
                return
        elif is_at:
            # 艾特了却没图可搜，别让人干等
            await bot.send_group_message(ws, group_id, "你要搜哪张图？先把图发出来我才能看", at_user_id=sender_id)
            return

    # 管理员快捷指令检测（忽略大小写，兼容全角斜杠与常用刹车词）
    if is_admin and (
        norm_lower.startswith(("/new", "/stop", "/root", "/user", "/on", "/off"))
        or norm_lower in ("stop", "new", "闭嘴", "停", "暂停")
    ):
        target_cmd = norm_cmd
        if norm_lower in ("stop", "闭嘴", "停", "暂停"):
            target_cmd = "/stop"
        elif norm_lower == "new":
            target_cmd = "/new"
        handled = await bot.handle_admin_command(ws, target_cmd, is_private=False, group_id=group_id)
        if handled:
            return

    # 管理员在群里直接下达管理命令拦截 (无需切 root 模式，支持随时修改/清除/重置指定群友的 memory 与好感度)
    if is_admin:
        # 1. 清除/重置某人 memory
        # 兼容: 重置3650199442memary / 清除 3650199442 memory / 删掉群友 3650199442 记忆 / 清空3650199442
        m_clear = re.search(r"(?:清除|删掉|清空|重置)\s*(?:用户|群友)?\s*(\d{5,11})\s*(?:的)?\s*(?:memory|memary|记忆|档案)?", clean_body, re.IGNORECASE)
        # 或者反过来匹配: memory 清除 3650199442 / 重置记忆 3650199442
        if not m_clear:
            m_clear = re.search(r"(?:memory|memary|记忆|档案)?\s*(?:清除|删掉|清空|重置)\s*(?:用户|群友)?\s*(\d{5,11})", clean_body, re.IGNORECASE)
        if m_clear:
            target_qq = int(m_clear.group(1))
            cleared = bot.clear_single_user_memory(target_qq, group_id=group_id)
            if cleared:
                report = f"已清除本群群友 (QQ {target_qq}) 的记忆档案"
            else:
                report = f"记忆库中暂无 (QQ {target_qq}) 的记录"
            await bot.send_group_message(ws, group_id, report, is_command_report=True)
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
            mem = bot.get_or_create_user_memory(target_qq, group_id=group_id)
            old_f = mem.get("favor", 50)
            new_f = max(0, min(100, old_f + actual_delta))
            mem["favor"] = new_f
            mem["updated_at"] = time.time()
            bot.save_user_memories()
            action_desc = f"扣除 {delta_val}" if is_sub else f"增加 {delta_val}"
            report = f"已为 (QQ {target_qq}) {action_desc} 点好感度，当前好感度: {old_f} -> {new_f}/100"
            await bot.send_group_message(ws, group_id, report, is_command_report=True)
            return

        # 3. 绝对值修改某人好感度: 改 123456 的好感度为 80 / 把 123456 好感度调至 0 / 设为 100 / 3650199442 好感度 80 / 调成 0 / 改为 0
        # 兼容正序与倒序: "把298152965好感度调至0" 或 "把好感度调至0 298152965"
        m_favor = re.search(r"(?:把|将|改|设置)?\s*(?:用户|群友)?\s*(\b\d{5,11}\b)\s*(?:的)?\s*(?:好感度|favor)?\s*(?:设为|改为|变更为|调至|调成|调为|调节为|设置为|为|\=|\s+)\s*(\d+)", clean_body, re.IGNORECASE)
        if m_favor:
            target_qq = int(m_favor.group(1))
            new_favor = max(0, min(100, int(m_favor.group(2))))
        else:
            m_favor = re.search(r"(?:好感度|favor)\s*(?:设为|改为|变更为|调至|调成|调为|调节为|设置为|为|\=|\s+)\s*(\d+)\s*(?:给|针对|对)?\s*(?:用户|群友)?\s*(\b\d{5,11}\b)", clean_body, re.IGNORECASE)
            if m_favor:
                # 倒序形式: group(1)是数值, group(2)是QQ
                new_favor = max(0, min(100, int(m_favor.group(1))))
                target_qq = int(m_favor.group(2))

        if m_favor:
            mem = bot.get_or_create_user_memory(target_qq, group_id=group_id)
            mem["favor"] = new_favor
            mem["updated_at"] = time.time()
            bot.save_user_memories()
            report = f"已将 (QQ {target_qq}) 的好感度设为 {new_favor}/100"
            await bot.send_group_message(ws, group_id, report, is_command_report=True)
            return

    # 拟真作息睡眠系统 (01:00 ~ 07:00 CST 为入睡期)
    # 取消强制硬叫醒机制；睡眠期内群友直接呼叫时带睡意梦话/困倦回复；主人或进入处理流程时带睡意语气词
    is_sleeping_now = bot.is_bot_sleeping(group_id)
    if is_sleeping_now:
        if not is_admin:
            # 普通群友在睡眠期：被 @ 或回复时，回复困倦梦话并继续睡（纯语气词与短句，去动作人机感）
            if is_at or bool(re.search(r"\[CQ:reply,id=-?\d+\]", raw_message)):
                key = (group_id, sender_id)
                last_sleep_reply = bot.user_sleep_reply_cd.get(key, 0.0)
                if now - last_sleep_reply > 180.0:
                    bot.user_sleep_reply_cd[key] = now
                    sleepy_quips = [
                        "呼呼...别吵我睡觉啦，困死了",
                        "唔...好困好困，明天睡醒再说啦",
                        "呼噜噜...zzz 别吵别吵",
                        "大半夜的不睡觉抓鱼是吧...困死了不理你",
                        "呼...困得眼睛都睁不开了，明天白天再找我玩嘛"
                    ]
                    await bot.send_group_message(ws, group_id, random.choice(sleepy_quips), at_user_id=sender_id)
                return
            else:
                # 睡眠期旁观消息静默，绝不主动插话
                return

    # 拼装包含引用的文本
    if reply_prefix:
        full_content = f"{reply_prefix} {clean_body}"
    else:
        full_content = clean_body
        
    # 超时重置时间线 (5 分钟未活动)
    if now - bot.group_last_activity[group_id] > bot.INACTIVITY_TIMEOUT:
        bot.group_timelines[group_id].clear()
    bot.group_last_activity[group_id] = now
    
    prefix = "[管理员] " if is_admin else ""
    record_item = {
        "sender": f"{prefix}{sender_display}",
        "content": full_content,
        "time": now
    }
    bot.group_timelines[group_id].append(record_item)
    bot.group_history_pool[group_id].append(record_item)
    # 异步非阻塞推入跨群公共话题（仅内存+脏标记）
    cross_memory.cross_group_mgr.record_topic(group_id, sender_display, full_content)
    bot.logger.info("[群 %d][%s] %s%s: %s", group_id, "被@" if is_at else "群聊", prefix, sender_display, full_content)
    
    # 检查是否明确 @ 了群里的其他人（非机器人）
    at_other_match = re.findall(r"\[CQ:at,[^\]]*qq=(\d+)[^\]]*\]", raw_message)
    is_at_other = any(int(q) != bot.BOT_QQ for q in at_other_match)
    
    # 检查是否艾特了主人（ADMIN_QQ）
    is_at_admin = any(int(q) == bot.ADMIN_QQ for q in at_other_match)
    
    # 检查是否引用回复了其他人（非机器人）
    is_reply_to_other = False
    is_reply_to_admin = False
    if "reply" in event and isinstance(event["reply"], dict):
        reply_sender = event["reply"].get("sender", {}).get("user_id")
        if reply_sender and reply_sender != bot.BOT_QQ:
            is_reply_to_other = True
            if reply_sender == bot.ADMIN_QQ:
                is_reply_to_admin = True

    # 针对主人的攻击/挑衅/还嘴的主动护短检测
    is_attacking_admin = False
    if not is_admin and (is_at_admin or is_reply_to_admin or any(kw in clean_body for kw in ("life", "主人", "管理员"))):
        # 简单攻击/嘲讽词快速特征检测
        hostile_keywords = (
            "菜", "傻", "笨", "蠢", "装", "狗", "滚", "死", "废物", "垃圾", "逆天", "大病", 
            "煞笔", "sb", "弱智", "脑瘫", "红温", "急了", "急了急了", "叫什么", "什么玩意", 
            "恶心", "服了", "典", "乐", "笑死", "破防", "你算什么", "下头", "闭嘴", "爬",
            "病娇", "小丑", "孤儿", "下头男", "什么成分", "装什么"
        )
        if any(hk in clean_body.lower() for hk in hostile_keywords):
            is_attacking_admin = True
            bot.logger.info("[群 %d] 发现群友 %s 疑似对主人(life) 进行攻击/阴阳怪气，触发自动护短护卫模式！", group_id, sender_nick)
            # 扣除攻击主人的群友 5 点好感度
            mem = bot.get_or_create_user_memory(sender_id, sender_nick, group_id=group_id)
            mem["favor"] = max(0, mem.get("favor", 50) - 5)
            bot.logger.info("[群 %d] 攻击主人行为惩罚：群友 %s 好感度降至 %d", group_id, sender_nick, mem["favor"])

    # 检查是否处于连续对话追问窗口（5分钟内免 @ 回答 5 次，有新的 @ 则重置）
    user_conv_key = (group_id, sender_id)
    follow_up_info = bot.user_follow_up_state.get(user_conv_key)
    is_follow_up = False

    if is_at_other or is_reply_to_other:
        # 该用户正在明确与群里的其他人对话，主动释放追问态，绝不插话打扰
        # 但显式 @ 了机器人还是要回——他是在问机器人怎么看他俩这番对话
        if not is_at and not is_reply_to_bot and user_conv_key in bot.user_follow_up_state:
            bot.user_follow_up_state.pop(user_conv_key, None)
            bot.logger.info("[群 %d] 用户 %s 明确与他人互动，主动退出追问窗口", group_id, sender_nick)
    elif is_at or is_reply_to_bot:
        # 有新的显式 @ 或回复机器人：直接重置为 5 分钟 + 5 次追问配额！
        bot.user_follow_up_state[user_conv_key] = {
            "expire": now + bot.FOLLOW_UP_WINDOW,
            "remain": bot.FOLLOW_UP_MAX_COUNT
        }
        bot.logger.info("[群 %d] 用户 %s 触发新 @，追问窗口重置为 %.0f 秒 / %d 次免 @ 配额", group_id, sender_nick, bot.FOLLOW_UP_WINDOW, bot.FOLLOW_UP_MAX_COUNT)
    elif follow_up_info and now < follow_up_info.get("expire", 0.0):
        # 处于 5 分钟窗口内，检查剩余次数配额
        remain_cnt = follow_up_info.get("remain", 0)
        if remain_cnt > 0:
            is_follow_up = True
            follow_up_info["remain"] = remain_cnt - 1
            left_secs = follow_up_info["expire"] - now
            bot.logger.info("[群 %d] 用户 %s 命中免 @ 追问（剩余可用配额 %d 次，窗口剩余 %.1fs）", group_id, sender_nick, remain_cnt - 1, left_secs)
        else:
            bot.logger.info("[群 %d] 用户 %s 5 分钟内 5 次免 @ 追问已耗尽，需重新 @ 激活", group_id, sender_nick)
    
    # 如果明确 @ 了别人、又没 @ 机器人，即便带了其他触发也不立即回复
    # 但 @ 了机器人就是明确在问机器人，同时提到别人也只是在给它补充上下文
    # 特例：如果是群友在攻击主人，立即破例强行介入还嘴护短
    if is_attacking_admin:
        should_reply_immediately = True
    elif is_at_other and not is_at and not is_reply_to_bot:
        should_reply_immediately = False
    elif is_admin and (is_at or is_reply_to_bot or is_follow_up):
        # 主人发言：只要被 @、被回复，或者处于免 @ 追问窗口内，一律立即触发，不进防抖缓冲
        should_reply_immediately = True
    else:
        should_reply_immediately = is_at or is_reply_to_bot or is_follow_up

    # 1. 直接呼叫模式（@、引用机器人、专属追问中 或 触发护短还嘴）：立即触发响应
    if should_reply_immediately:
        task = bot.group_debounce_tasks.get(group_id)
        if task and not task.done():
            task.cancel()

        extra_note = ""
        if is_attacking_admin:
            extra_note = (
                "【特别警报（最高优先级护主反击）】：\n"
                "该群友刚才在群里挑衅/辱骂/阴阳怪气你的最高主人(life)！\n"
                "你必须无条件立刻帮主人狠狠骂回去！严禁装清高、严禁讲道理、严禁拐弯抹角阴阳怪气！\n"
                "用互联网老哥真实对线的凶悍力道迎头痛击，善用常用谐音（如煞笔/脑瘫/小脑萎缩/叫什么叫/小丑等）正面硬刚回喷，给主人狠狠出气！"
            )
        elif is_sleeping_now:
            extra_note = (
                "【当前为夜间作息睡眠时段】：\n"
                "你正处于睡眠中刚被叫醒的状态，整个人很困、睡意朦胧、哈欠连天。\n"
                "要求：只能通过真实的口癖与语气词呈现睡意（例如句首或句中带哈欠声、唔……、呼、困死了、大半夜的等自然口语），严禁带括号动作描写（如严禁写括号揉眼睛/打哈欠等动作），保持真实松弛的对话。"
            )

        asyncio.create_task(process_group_trigger(
            ws, group_id,
            is_at=is_at or is_reply_to_bot or is_attacking_admin,
            sender_nick=sender_display,
            is_admin=is_admin,
            target_uid=sender_id,
            user_text=clean_body,
            is_follow_up=is_follow_up,
            extra_instruction=extra_note
        ))
        return
        
    # 2. 旁观模式（未直接找机器人）：严格门控过滤，绝不乱插嘴
    # 过滤短语无意义废话
    if bot.is_low_info_message(clean_body):
        bot.group_last_msg_time[group_id] = now
        return
        
    # 检查管理员 /stop 设置的群主动插话静默保护期
    silence_exp = bot.group_silence_until.get(group_id, 0.0)
    if now < silence_exp:
        bot.logger.debug("[群 %d] 处于管理员 /stop 静默保护期（剩余 %.0fs），保持静默不插话", group_id, silence_exp - now)
        return

    # 意图门控：结合冷热场动态权重送入大模型自主思考判定插话；若本条带有识别到的图片画面，且有一定信息量，也交由大模型自主评判
    should_trigger, is_mention_bot = bot.should_active_interrupt(clean_body, group_id=group_id, msg_count_since=bot.group_msg_count_since_bot[group_id])
    if vision_desc and len(vision_desc) >= 5:
        # 群友发了图片/表情包，允许进入大模型自主判断（AI 会自行决定是回梗还是 [PASS] 静默）
        should_trigger = True
        bot.logger.info("[群 %d] 捕获到群友发图/表情包，送入大模型自主决策是否插话评价", group_id)

    # 如果检测到群友分享了多媒体/视频/GitHub/图文卡片，且不是问项目自身地址，则自动触发插话进行省流与锐评
    is_media_share = bool(media_info and media_info.get("summary"))
    if is_media_share and not is_asking_own_repo:
        should_trigger = True
        bot.logger.info("[群 %d] 捕获到群友分享视频/GitHub/媒体卡片 (%s)，自动触发解析与评价", group_id, media_info.get("type"))

    bot.group_last_msg_time[group_id] = now
    if not should_trigger:
        return

    # 冷却时间门控：若处于主动插话 CD 中，保持静默（媒体卡片分享 CD 缩短至 3 秒；普通旁观插话 8 秒）
    min_cd = 3.0 if is_media_share else 8.0
    if now - bot.group_last_active_reply[group_id] < min_cd:
        bot.logger.info("[群 %d] 主动插话处于冷却期 (%.0fs)，保持静默。", group_id, min_cd)
        return
        
    async def debounced_step(target_gid: int, nick: str, mention_flag: bool, uid: int, text_body: str):
        try:
            # 缩短至 1.0 秒轻微防抖，接话更迅速不被冲走
            await asyncio.sleep(1.0)
            is_adm = (uid == bot.ADMIN_QQ)
            await process_group_trigger(ws, target_gid, is_at=False, sender_nick=nick, is_admin=is_adm, is_mention=mention_flag, target_uid=uid, user_text=text_body)
        except asyncio.CancelledError:
            pass

    old_task = bot.group_debounce_tasks.get(group_id)
    if old_task and not old_task.done():
        old_task.cancel()
    bot.group_debounce_tasks[group_id] = asyncio.create_task(debounced_step(group_id, sender_display, is_mention_bot, sender_id, clean_body))


async def process_group_trigger(ws: aiohttp.ClientWebSocketResponse, group_id: int, is_at: bool, sender_nick: str, is_admin: bool = False, is_mention: bool = False, target_uid: Optional[int] = None, user_text: str = "", is_follow_up: bool = False, extra_instruction: str = ""):
    """汇总群时间线向模型发起决策"""
    timeline = bot.group_timelines[group_id]
    if not timeline and not user_text:
        return
        
    # 闲聊只传当前发言和少量未读上下文，避免历史越堆越长，也防止认错人
    # 如果是 root 模式则提供完整时间线供技术分析，否则仅传递当前群友最新发言
    channel_mode = bot.get_channel_mode(group_id)
    if is_admin and channel_mode == "root":
        dialogue_text = "\n".join([f"{m['sender']}: {m['content']}" for m in timeline])
    elif is_admin:
        # 主人追问/呼叫时只喂最近几句：单独一个「乐乐」递过去，模型只能当寒暄敷衍
        recent_lines = list(timeline)[-8:] if timeline else []
        dialogue_text = "\n".join([f"{m['sender']}: {m['content']}" for m in recent_lines]) or (user_text or "")
    else:
        # 群友直接找机器人时也给几行上下文，免得它把一句「乐乐」当成初次见面打招呼
        # 追问态下对方常只丢半句，上下文给宽一点才接得住
        recent_lines = list(timeline)[-(8 if is_follow_up else 6):] if timeline else []
        dialogue_text = "\n".join([f"{m['sender']}: {m['content']}" for m in recent_lines]) or (user_text or "")
    
    bot.logger.info("[群 %d] 触发轻量会话分析 (is_at=%s, is_follow_up=%s, is_admin=%s), 发言内容: %s", group_id, is_at, is_follow_up, is_admin, dialogue_text[:40])
    
    # 异步触发「对方正在输入中」状态，消除纯等待的生硬机器感
    if hasattr(bot, "send_input_status"):
        asyncio.create_task(bot.send_input_status(ws, group_id))
    
    current_task = asyncio.current_task()
    if current_task:
        bot.running_tasks.add(current_task)
        
    # 群聊限流机制已彻底移除：自由回复，不再截断或强制静默
    # extra_instruction 由调用方显式传入（例如触发护短时的最高警报）

    try:
        reply = await bot.call_deepseek(
            dialogue_text,
            is_at=is_at,
            is_admin=is_admin,
            is_private=False,
            is_mention=is_mention,
            target_uid=target_uid,
            sender_nick=sender_nick,
            group_id=group_id,
            is_follow_up=is_follow_up,
            latest_line=user_text,
            extra_system_instruction=extra_instruction
        )
    finally:
        if current_task:
            bot.running_tasks.discard(current_task)

    if not reply:
        return

    # 如果回复中带有 [PASS]（或者模型啰嗦解释了为什么保持沉默）
    # 只要判定包含 [PASS]，不管后面带不带括号解释，都必须彻底保持静默！
    if "[PASS]" in reply:
        bot.logger.info("[群 %d] 模型输出包含 [PASS]，保持绝对静默不发送群消息。", group_id)
        return

    # 额外拦截模型常见的内心括号自我独白（如“（根据规则保持沉默）”、“（保持静默）”）
    if any(silent_kw in reply for silent_kw in ["保持沉默", "保持静默", "无需回复", "不便插话", "选择沉默"]):
        bot.logger.info("[群 %d] 拦截到模型静默解释独白: %s，直接丢弃。", group_id, reply)
        return

    # 旁观插话时（非 @ 且非管理员）：若模型生成的内容属于没搞懂语境的反问、敷衍套话，一律丢弃
    # 追问态不算旁观：人家就是来找你说话的，「你说啥」这种反问至少是个回应，别拦
    if not is_at and not is_admin and not is_follow_up:
        confused_patterns = [
            r"你在说[啥什么]", r"什么意思[呀呢啊]?", r"不知道你在说", r"没搞[懂明]",
            r"没听懂", r"谁进不去", r"哪款游戏", r"发生了什么事[呀呢啊]?", r"你在聊[啥什么]",
            # 催问式空话：什么都没接住还占一条屏，比沉默更败好感
            r"刚问啥了", r"你刚才?说(啥|什么)", r"你倒是(说|问|讲)", r"把问题问出来"
        ]
        if any(re.search(pat, reply) for pat in confused_patterns):
            bot.logger.info("[群 %d] 拦截到旁观插话中没搞懂的困惑反问套话: %s，保持静默直接丢弃", group_id, reply)
            return

    # 机器人每次在群里发话后，重置该群的消息计数
    bot.group_msg_count_since_bot[group_id] = 0
    if not is_at and not is_admin:
        bot.group_last_active_reply[group_id] = time.time()

    # 拟真发话停顿：被@或追问时直连秒回，旁观插话保留 0.3s 微停顿
    if not is_at and not is_admin and not is_follow_up:
        await asyncio.sleep(0.3)
    # 当群友主动艾特机器人呼叫时，回复必须带上 [CQ:at] 艾特该群友；若为主动旁观插话则自然接话不强行艾特
    at_target = target_uid if is_at else None
    sent_text = await bot.send_segmented_group_message(ws, group_id, reply, is_command_report=(is_admin and bot.get_channel_mode(group_id) == "root"), at_user_id=at_target)

    # 写回上下文的是实际发出去的那句：削平过、标签剥过，跟群里看到的一致
    clean_reply_record = bot.remove_trailing_period(bot.strip_markdown(sent_text or "")).strip()
    if clean_reply_record:
        bot_record = {
            "sender": "小鲸鱼",
            "content": clean_reply_record,
            "time": time.time()
        }
        bot.group_timelines[group_id].append(bot_record)
        bot.group_history_pool[group_id].append(bot_record)

    # 成功发回后，更新追问窗口状态
    if target_uid:
        # 异步更新当前群友的记忆与好感度（结合群友发言与机器人回复由 AI 动态判定）
        content_to_analyze = user_text or (timeline[-1]["content"] if timeline else "")
        asyncio.create_task(bot.update_user_memory_async(target_uid, sender_nick, content_to_analyze, group_id=group_id, bot_reply=clean_reply_record))

