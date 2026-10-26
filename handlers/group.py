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
    
    # 累加该群自上次机器人发言以来的消息条数
    bot.group_msg_count_since_bot[group_id] += 1

    # 捕获群聊中的图片进入两段式近期图片缓冲池，并尝试进行超轻量视觉转录
    m_imgs = re.findall(r'\[CQ:image,[^\]]*url=([^ control\,\]]+)', raw_message)
    if m_imgs:
        for u in m_imgs:
            u_clean = u.replace("&amp;", "&")
            bot.group_recent_images[group_id].append((u_clean, time.time(), sender_nick))
            # 只记下来源，等机器人真要接话时再取摘要，不为每张图白调一次视觉模型

    # 记下群友之间的 @，模型接话时用来判断谁跟谁熟
    m_ats = re.findall(r'\[CQ:at,[^\]]*qq=(\d+)[^\]]*\]', raw_message)
    for target_qq_str in m_ats:
        try:
            t_qq = int(target_qq_str)
            if t_qq != bot.BOT_QQ:
                cross_interaction.record_interaction(group_id, sender_id, sender_nick, t_qq, target_qq_str)
        except Exception:
            pass

    # 解析引用消息与合并转发
    reply_prefix = await bot.extract_reply_context(ws, event, raw_message)
    raw_message = await bot.expand_forward_messages(ws, raw_message)

    # 深度解析主流媒体短链 (抖音/小红书/B站短链)
    if bot.http_session and ("douyin.com" in raw_message or "xhslink.com" in raw_message or "b23.tv" in raw_message):
        try:
            media_info = await media_resolver.parse_media_content(bot.http_session, raw_message)
            if media_info and media_info.get("summary"):
                raw_message = f"{raw_message}\n{media_info['summary']}"
        except Exception:
            pass

    # 清洗掉针对机器人的 @ CQ 码以及其他无关 CQ
    clean_body = re.sub(rf"\[CQ:at,[^\]]*qq={bot.BOT_QQ}[^\]]*\]", "", raw_message)
    clean_body = bot.clean_cq_codes(clean_body).strip()
    # 移除 reply 的 CQ 码残留
    clean_body = re.sub(r"\[CQ:reply,id=-?\d+\]", "", clean_body).strip()

    # 如果用户只 @ 了机器人，没有附带文字，给一个默认问候
    if is_at and not clean_body:
        clean_body = "在吗"
    elif not clean_body:
        return

    # 游戏指令或云崽生态专用指令，交给云崽等扩展插件处理，AI 机器人主动跳过，彻底避免抢答与刷屏
    if bot.is_game_or_yunzai_command(clean_body):
        bot.logger.debug("[群 %d] 识别为游戏指令: %r，主动转交云崽处理，跳过 AI 回复", group_id, clean_body)
        return

    # 问项目出处/源码/部署：直接甩仓库，不走模型，省得绕圈子或者漏链接
    repo_reply = bot.repo_ask_reply(clean_body)
    if repo_reply:
        bot.logger.info("[群 %d] 命中仓库问答，直接回地址", group_id)
        await bot.send_segmented_group_message(
            ws, group_id, repo_reply, at_user_id=sender_id if is_at else None
        )
        return

    # 4. 安全护栏：拦截提示词注入/越狱指令，以傲娇拟真口吻反讽
    is_injected, snarky_reply = safety_guard.check_prompt_injection(clean_body)
    if is_injected and snarky_reply:
        bot.logger.info("[群 %d] 拦截群友 %s 的提示词注入/越狱请求: %s", group_id, sender_nick, clean_body)
        await bot.send_segmented_group_message(ws, group_id, snarky_reply, at_user_id=sender_id)
        return

    # 规范化指令前缀与小写，兼容全角斜杠与输入法大小写
    norm_cmd = clean_body.replace("／", "/").strip()
    norm_lower = norm_cmd.lower()

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
        # 寻找群内 90 秒内的有效图片
        now_ts = time.time()
        target_img_url = None
        for img_u, img_t, img_sender in reversed(bot.group_recent_images[group_id]):
            if now_ts - img_t < 90.0:
                target_img_url = img_u
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

    # 被 @ 还甩了张图过来，当斗图处理，直接从库存里挑一张回去，不浪费模型调用
    if is_at and m_imgs and not re.search(r"(?:什么番|哪部番|出处|搜番|搜截图)", clean_body):
        if await fun_features.meme_battle(ws, group_id, sender_id, clean_body):
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
        if not m_favor:
            m_favor = re.search(r"(?:好感度|favor)\s*(?:设为|改为|变更为|调至|调成|调为|调节为|设置为|为|\=|\s+)\s*(\d+)\s*(?:给|针对|对)?\s*(?:用户|群友)?\s*(\b\d{5,11}\b)", clean_body, re.IGNORECASE)
            if m_favor:
                # 倒序形式: group(1)是数值, group(2)是QQ
                new_favor = max(0, min(100, int(m_favor.group(1))))
                target_qq = int(m_favor.group(2))
                mem = bot.get_or_create_user_memory(target_qq, group_id=group_id)
                mem["favor"] = new_favor
                mem["updated_at"] = time.time()
                bot.save_user_memories()
                report = f"已将 (QQ {target_qq}) 的好感度设为 {new_favor}/100"
                await bot.send_group_message(ws, group_id, report, is_command_report=True)
                return
        if m_favor:
            target_qq = int(m_favor.group(1))
            new_favor = max(0, min(100, int(m_favor.group(2))))
            mem = bot.get_or_create_user_memory(target_qq, group_id=group_id)
            mem["favor"] = new_favor
            mem["updated_at"] = time.time()
            bot.save_user_memories()
            report = f"已将 (QQ {target_qq}) 的好感度设为 {new_favor}/100"
            await bot.send_group_message(ws, group_id, report, is_command_report=True)
            return

    # 拟真作息睡眠系统 (01:00 ~ 07:00 CST 为入睡期)
    if bot.is_bot_sleeping():
        if is_admin:
            # 主人专属唤醒特权
            wake_words = ["醒醒", "起床", "别睡了", "起来了", "起来", "快醒醒", "别睡啦", "上工", "开工", "干活"]
            is_wake = any(w in clean_body for w in wake_words) or clean_body.startswith(("/root", "/user"))
            if is_wake:
                bot.sleep_override_until = now + 1800.0  # 临时唤醒 30 分钟
                bot.logger.info("[作息系统] 主人 %s 触发专属叫醒词，大肥鱼临时唤醒 30 分钟接活！", sender_nick)
                wake_reply = random.choice([
                    "(迷迷糊糊揉眼睛，从被窝钻出来) 主人我醒啦，大半夜怎么啦～",
                    "(打了个长长的哈欠) 呼...主人叫我呀，我醒了！有啥急事快吩咐～",
                    "(被子一掀瞬间精神) 到！主人我在呢，大半夜需要我做什么呀～"
                ])
                await bot.send_group_message(ws, group_id, wake_reply, at_user_id=sender_id)
                return
            # 主人正常交谈自动唤醒接活
            bot.sleep_override_until = now + 1800.0
        else:
            # 普通群友在睡眠期（选项 B：睡意梦话提示并继续睡）
            if is_at or bool(re.search(r"\[CQ:reply,id=-?\d+\]", raw_message)):
                key = (group_id, sender_id)
                last_sleep_reply = bot.user_sleep_reply_cd.get(key, 0.0)
                if now - last_sleep_reply > 180.0:
                    bot.user_sleep_reply_cd[key] = now
                    sleepy_quips = [
                        "呼呼...别吵我睡觉（拉起被子翻身继续睡）",
                        "唔...好困，明天睡醒再说啦 (打哈欠潜水)",
                        "呼噜噜...zzz (闭着眼睛吐了个泡泡)",
                        "大半夜不睡觉抓鱼是吧...困死了不理你 (把头缩进被窝)",
                        "呼...困得眼睛睁不开，明天白天再找我玩嘛 (翻身)"
                    ]
                    await bot.send_group_message(ws, group_id, random.choice(sleepy_quips), at_user_id=sender_id)
                return
            else:
                # 旁观消息静默入睡，绝不主动插话
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
    
    # 检查是否直接呼叫机器人：显式 @ 机器人，或者回复引用了机器人之前的发言
    is_reply_to_bot = False
    if "reply" in event and isinstance(event["reply"], dict):
        is_reply_to_bot = (event["reply"].get("sender", {}).get("user_id") == bot.BOT_QQ)
    
    # 检查是否明确 @ 了群里的其他人（非机器人）
    at_other_match = re.findall(r"\[CQ:at,[^\]]*qq=(\d+)[^\]]*\]", raw_message)
    is_at_other = any(int(q) != bot.BOT_QQ for q in at_other_match)
    
    # 检查是否引用回复了其他人（非机器人）
    is_reply_to_other = False
    if "reply" in event and isinstance(event["reply"], dict):
        reply_sender = event["reply"].get("sender", {}).get("user_id")
        if reply_sender and reply_sender != bot.BOT_QQ:
            is_reply_to_other = True

    # 检查是否处于连续对话追问窗口（5分钟内免 @ 回答 5 次，有新的 @ 则重置）
    user_conv_key = (group_id, sender_id)
    follow_up_info = bot.user_follow_up_state.get(user_conv_key)
    is_follow_up = False

    if is_at_other or is_reply_to_other:
        # 该用户正在明确与群里的其他人对话，主动释放追问态，绝不插话打扰
        if user_conv_key in bot.user_follow_up_state:
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
    
    # 如果明确 @ 了别人，即便带了其他触发也不立即回复
    if is_at_other:
        should_reply_immediately = False
    else:
        should_reply_immediately = is_at or is_reply_to_bot or is_follow_up

    # 1. 直接呼叫模式（@、引用机器人 或 专属追问中）：立即触发响应
    if should_reply_immediately:
        if not is_admin:
            # 疲劳度衰减检查：防止同一群友短时间内连续疯狂 @ 调戏
            is_fatigued, is_first_trigger = bot.check_user_fatigue(group_id, sender_id)
            if is_fatigued:
                if is_first_trigger:
                    tired_quips = [
                        "哎呀别连环艾特啦，本鱼头好大要歇会儿 (翻身潜水)",
                        "歇会儿歇会儿，你手不酸我嘴都酸了！(闭麦60秒)",
                        "吵死啦！本大肥鱼也是需要休息的，等会再理你 (溜走)",
                        "连着喊我不累嘛，本鲸宝要去喝口水冷静一下"
                    ]
                    await bot.send_group_message(ws, group_id, random.choice(tired_quips), at_user_id=sender_id)
                bot.logger.info("[群 %d] 用户 %s 触发疲劳防调戏限制，本次略过", group_id, sender_nick)
                return

            key = (group_id, sender_id)
            last_at = bot.user_last_at_time.get(key, 0.0)
            # 追问状态下防抖设为 2.0 秒，非追问常规被@为 2.5 秒基础冷却
            cd = 2.0 if is_follow_up else bot.DIRECT_AT_CD
            if now - last_at < cd:
                bot.logger.info("[群 %d] 用户 %s 处于冷却中（%.1f秒），略过。", group_id, sender_nick, cd)
                return
            bot.user_last_at_time[key] = now
        
        task = bot.group_debounce_tasks.get(group_id)
        if task and not task.done():
            task.cancel()

        asyncio.create_task(process_group_trigger(ws, group_id, is_at=is_at or is_reply_to_bot, sender_nick=sender_display, is_admin=is_admin, target_uid=sender_id, user_text=clean_body, is_follow_up=is_follow_up))
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

    # 意图门控：结合冷热场动态权重、代称、讨论疑问词综合判定插话
    should_trigger, is_mention_bot = bot.should_active_interrupt(clean_body, group_id=group_id, msg_count_since=bot.group_msg_count_since_bot[group_id])
    bot.group_last_msg_time[group_id] = now
    if not should_trigger:
        return


    # 冷却时间门控：若未直接提及机器人且处于 CD 中，保持静默（若直接提及机器人或代称，CD 缩短为 3 秒；普通插话 10 秒）
    min_cd = 3.0 if is_mention_bot else 10.0
    if now - bot.group_last_active_reply[group_id] < min_cd:
        bot.logger.info("[群 %d] 主动插话处于冷却期 (%.0fs)，保持静默。", group_id, min_cd)
        return
        
    async def debounced_step(target_gid: int, nick: str, mention_flag: bool, uid: int, text_body: str):
        try:
            # 缩短至 1.0 秒轻微防抖，接话更迅速不被冲走
            await asyncio.sleep(1.0)
            await process_group_trigger(ws, target_gid, is_at=False, sender_nick=nick, is_admin=False, is_mention=mention_flag, target_uid=uid, user_text=text_body)
        except asyncio.CancelledError:
            pass

    old_task = bot.group_debounce_tasks.get(group_id)
    if old_task and not old_task.done():
        old_task.cancel()
    bot.group_debounce_tasks[group_id] = asyncio.create_task(debounced_step(group_id, sender_display, is_mention_bot, sender_id, clean_body))


async def process_group_trigger(ws: aiohttp.ClientWebSocketResponse, group_id: int, is_at: bool, sender_nick: str, is_admin: bool = False, is_mention: bool = False, target_uid: Optional[int] = None, user_text: str = "", is_follow_up: bool = False):
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
        recent_lines = list(timeline)[-6:] if timeline else []
        dialogue_text = "\n".join([f"{m['sender']}: {m['content']}" for m in recent_lines]) or (user_text or "")
    
    bot.logger.info("[群 %d] 触发轻量会话分析 (is_at=%s, is_follow_up=%s, is_admin=%s), 发言内容: %s", group_id, is_at, is_follow_up, is_admin, dialogue_text[:40])
    
    current_task = asyncio.current_task()
    if current_task:
        bot.running_tasks.add(current_task)
        
    try:
        reply = await bot.call_deepseek(dialogue_text, is_at=is_at, is_admin=is_admin, is_private=False, is_mention=is_mention, target_uid=target_uid, sender_nick=sender_nick, group_id=group_id, is_follow_up=is_follow_up, latest_line=user_text)
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
    if not is_at and not is_admin:
        confused_patterns = [
            r"你在说[啥什么]", r"什么意思[呀呢啊]?", r"不知道你在说", r"没搞[懂明]",
            r"没听懂", r"谁进不去", r"哪款游戏", r"发生了什么事[呀呢啊]?", r"你在聊[啥什么]"
        ]
        if any(re.search(pat, reply) for pat in confused_patterns):
            bot.logger.info("[群 %d] 拦截到旁观插话中没搞懂的困惑反问套话: %s，保持静默直接丢弃", group_id, reply)
            return
        
    # 机器人每次在群里发话后，重置该群的消息计数
    bot.group_msg_count_since_bot[group_id] = 0
    if not is_at and not is_admin:
        bot.group_last_active_reply[group_id] = time.time()
    
    await asyncio.sleep(1.0)
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

