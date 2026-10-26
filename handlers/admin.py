# -*- coding: utf-8 -*-
"""管理指令：/root、/new、记忆清理、自然语言指挥。

状态都在 bot 里，这里只做解析和分发。
"""

import asyncio
import time
from typing import Optional

import aiohttp
from meme_manager import meme_manager

# 由 bot.py 在启动时注入自己。直接 import bot 在 `python bot.py` 下会拿到
# 另一个模块实例（__main__ 与 bot 各一份），全局状态就分裂了。
bot = None


def bind(host):
    global bot
    bot = host


async def handle_admin_command(ws: aiohttp.ClientWebSocketResponse, cmd: str, is_private: bool, group_id: Optional[int] = None) -> bool:
    """处理管理员快捷指令并汇报结果（大小写不敏感，秒级汇报）"""
    cmd = cmd.strip()
    cmd_lower = cmd.lower()

    # 0. /off /on - 一键关停或恢复本群全部功能
    if cmd_lower in ("/off", "/on", "/off ", "/on "):
        if is_private or not group_id:
            await bot.send_private_message(ws, bot.ADMIN_QQ, "这个指令得在群里发，私聊没有群配置")
            return True
        turn_on = cmd_lower.startswith("/on")
        cfg = bot.get_group_config(group_id)
        # enable_sleep 语义是"启用作息"，不是"启用功能"，关功能时反而要把它打开
        for key in list(cfg.keys()):
            if key.startswith("enable_") and key != "enable_sleep":
                cfg[key] = turn_on
        cfg["enable_sleep"] = True
        cfg["muted"] = not turn_on
        bot.save_group_configs()
        if turn_on:
            reply = "全开了，群里该说说该闹闹"
        else:
            reply = "全关了，从现在起群里我就当没听见，发 /on 才回来"
        await bot.send_group_message(ws, group_id, reply, is_command_report=True)
        return True


    # 1. /new - 重置上下文 (支持 /new all 重置所有群与私聊)
    if cmd_lower.startswith("/new"):
        args = cmd.split()
        if len(args) > 1 and args[1].lower() in ["all", "所有", "全部"]:
            bot.group_timelines.clear()
            bot.admin_private_timeline.clear()
            bot.group_history_pool.clear()
            bot.admin_private_history.clear()
            bot.user_follow_up_state.clear()
            bot.group_silence_until.clear()
            reply_text = "已重置所有群与私聊的上下文、历史记录及追问状态"
            if is_private:
                await bot.send_private_message(ws, bot.ADMIN_QQ, reply_text)
            else:
                await bot.send_group_message(ws, group_id, reply_text)
            return True
        if is_private:
            bot.admin_private_timeline.clear()
            bot.admin_private_history.clear()
            reply_text = "已清空私聊上下文与历史池"
            await bot.send_private_message(ws, bot.ADMIN_QQ, reply_text)
        else:
            bot.group_timelines[group_id].clear()
            bot.group_history_pool[group_id].clear()
            # 清空该群追问状态并解除静音
            for k in list(bot.user_follow_up_state.keys()):
                if k[0] == group_id:
                    bot.user_follow_up_state.pop(k, None)
            bot.group_silence_until.pop(group_id, None)
            reply_text = f"已重置本群 ({group_id}) 上下文与发言历史池"
            await bot.send_group_message(ws, group_id, reply_text)
        return True

    # 2. /stop - 一键紧急刹车中止所有思考与排队任务并开启静默
    elif cmd_lower.startswith("/stop"):
        stopped_count = 0
        # 1. 取消所有群的防抖任务
        for gid, task in list(bot.group_debounce_tasks.items()):
            if task and not task.done():
                task.cancel()
                stopped_count += 1
        # 2. 取消所有运行中的大模型思考/生成请求
        for task in list(bot.running_tasks):
            if not task.done():
                task.cancel()
                stopped_count += 1
                
        # 3. 清理追问窗口并开启静默保护，杜绝刚叫停又自动插话
        if group_id:
            for k in list(bot.user_follow_up_state.keys()):
                if k[0] == group_id:
                    bot.user_follow_up_state.pop(k, None)
            bot.group_silence_until[group_id] = time.time() + 600.0  # 10 分钟静默期
            bot.group_msg_count_since_bot[group_id] = 0
            reply_text = f"已一键急刹车，中止了 {stopped_count} 个任务，并开启本群静默（不再主动插话）" if stopped_count > 0 else "当前无进行中任务，已清空追问并开启本群静默，保持安静"
        else:
            bot.user_follow_up_state.clear()
            for gid in bot.ALLOWED_GROUPS:
                bot.group_silence_until[gid] = time.time() + 600.0
            reply_text = f"已一键急刹车，中止了 {stopped_count} 个任务，并开启全局群静默" if stopped_count > 0 else "当前无进行中任务，已清空追问并开启全局群静默"

        if is_private:
            await bot.send_private_message(ws, bot.ADMIN_QQ, reply_text)
        else:
            await bot.send_group_message(ws, group_id, reply_text)
        return True

    # 3. /root - 切换到工作模式 (支持按群独立切换)
    elif cmd_lower.startswith("/root"):
        target_gid = None
        parts = cmd.split()
        if len(parts) > 1 and parts[1].isdigit():
            target_gid = int(parts[1])
        elif not is_private:
            target_gid = group_id

        bot.update_group_config(target_gid, "mode", "root")
        desc = f"群 {target_gid} 已切换至 root 技术伙伴模式" if target_gid else "私聊已切换至 root 技术伙伴模式"
        reply_text = await generate_agent_command_reply("切换工作模式", desc, is_private=is_private, group_id=target_gid)
        if is_private:
            await bot.send_private_message(ws, bot.ADMIN_QQ, reply_text)
        else:
            await bot.send_group_message(ws, group_id, reply_text)
        return True

    # 4. /user - 切换回肥鱼娱乐模式 (支持按群独立切换)
    elif cmd_lower.startswith("/user"):
        target_gid = None
        parts = cmd.split()
        if len(parts) > 1 and parts[1].isdigit():
            target_gid = int(parts[1])
        elif not is_private:
            target_gid = group_id

        bot.update_group_config(target_gid, "mode", "user")
        desc = f"群 {target_gid} 已切回日常大肥鱼人设" if target_gid else "私聊已切回日常大肥鱼人设"
        reply_text = await generate_agent_command_reply("切换娱乐模式", desc, is_private=is_private, group_id=target_gid)
        if is_private:
            await bot.send_private_message(ws, bot.ADMIN_QQ, reply_text)
        else:
            await bot.send_group_message(ws, group_id, reply_text)
        return True

    # 5. /memes 或 /reload_memes - 手动重载与刷新网络热梗轮换池
    elif cmd_lower.startswith("/reload_memes") or cmd_lower.startswith("/memes"):
        meme_manager.load_pool(force=True)
        count = len(meme_manager.current_pool)
        total = len(meme_manager.all_memes)
        reply_text = f"热梗词库已重新洗牌刷新，当前轮换池: {count} 条，总词库: {total} 条"
        if is_private:
            await bot.send_private_message(ws, bot.ADMIN_QQ, reply_text)
        else:
            await bot.send_group_message(ws, group_id, reply_text)
        return True

    return False


_MEMORY_CACHE = {}
_MEMORY_CACHE_TTL = 20.0
_MEMORY_PER_PAGE = 12

# 群成员资料缓存：群名片、QQ 昵称、头衔、身份都在这里，省得每条都拉一遍
_MEMBER_CACHE = {}
_MEMBER_CACHE_TTL = 300.0

_ROLE_LABEL = {"owner": "群主", "admin": "管理员"}


async def _fetch_group_members(ws, group_id: int) -> dict:
    """拉群成员列表，返回 {uid_str: 成员信息}。拿不到就返回空，排版降级但不断。"""
    now = time.time()
    cached = _MEMBER_CACHE.get(group_id)
    if cached and now - cached[0] < _MEMBER_CACHE_TTL:
        return cached[1]
    info = {}
    try:
        res = await bot.call_action(ws, "get_group_member_list", {"group_id": group_id}, timeout=8.0)
        for m in (res or {}).get("data") or []:
            uid = m.get("user_id")
            if uid:
                info[str(uid)] = m
    except Exception as e:
        bot.logger.warning("[记忆] 拉群成员列表失败（排版降级）: %s", e)
    _MEMBER_CACHE[group_id] = (now, info)
    return info


def _dedupe_notes(notes) -> str:
    """印象是多轮打分攒的，「爱对线」这种会重复好几遍，显示前按顿号切开去重。"""
    seen, out = set(), []
    for item in notes or []:
        for part in str(item).replace("，", "、").split("、"):
            part = part.strip()
            if part and part not in seen:
                seen.add(part)
                out.append(part)
    return "、".join(out)


def _format_member(uid: str, mem: dict, member: dict) -> str:
    """一个人三行：名字+QQ号 / 身份+头衔 / 好感+印象。折叠记录里一行塞太满反而看不清。"""
    card = (member.get("card") or "").strip()
    qq_nick = (member.get("nickname") or "").strip()
    fallback = (mem.get("nickname") or "").strip()
    name = card or qq_nick or fallback or uid

    head = name
    if card and qq_nick and card != qq_nick:
        head += f"（QQ昵称 {qq_nick}）"
    head += f"  QQ {uid}"

    role = _ROLE_LABEL.get(member.get("role"), "群员")
    title = (member.get("title") or "").strip()
    meta = role + (f" · 头衔「{title}」" if title else "")

    favor = int(mem.get("favor", 50))
    notes = _dedupe_notes(mem.get("notes")) or "无印象"
    tail = f"好感 {favor}/100 {_favor_tier(favor)}｜{notes}"
    return "\n".join([head, meta, tail])


def _favor_tier(favor: int) -> str:
    if favor >= 80:
        return "恋慕"
    if favor >= 65:
        return "亲近"
    if favor >= 40:
        return "普通"
    return "炸毛"


async def handle_memory_summary_command(ws: aiohttp.ClientWebSocketResponse, group_id: Optional[int] = None, user_id: Optional[int] = None):
    """公开指令: /memory 或 /memary 生成当前群友记忆库与好感度总结（按群隔离）"""
    gid_key = str(group_id) if group_id else "private"

    # 20 秒内重复发同一个指令直接复用上一次的结果，省掉重复排序
    cached = _MEMORY_CACHE.get(gid_key)
    if cached and time.time() - cached[0] < _MEMORY_CACHE_TTL:
        title, entries = cached[1], cached[2]
    else:
        scope = bot.user_memories.get(gid_key, {})
        if not scope:
            await (bot.send_group_message(ws, group_id, "当前群聊暂无群友记忆记录", is_command_report=True)
                   if group_id else
                   bot.send_private_message(ws, user_id, "私聊暂无记忆记录", is_command_report=True))
            return

        members = await _fetch_group_members(ws, group_id) if group_id else {}
        # 好感高的排前面，主人固定第一
        rows = sorted(scope.items(), key=lambda kv: (-int(kv[1].get("favor", 50)), kv[0]))
        rows.sort(key=lambda kv: 0 if kv[0] == str(bot.ADMIN_QQ) else 1)

        group_name = bot.group_names_cache.get(group_id, f"群 {group_id}") if group_id else "私聊"
        title = f"群友记忆 · {group_name} · {len(scope)} 人" if group_id else f"私聊记忆 · {len(scope)} 人"
        entries = []
        for uid, mem in rows:
            entries.append((uid, _format_member(uid, mem, members.get(uid, {}))))
        _MEMORY_CACHE[gid_key] = (time.time(), title, entries)

    # 折叠成一条聊天记录，别在群里刷一整屏
    nodes = bot.build_forward_nodes(entries, title=title, bot_qq=bot.BOT_QQ)
    sent = await bot.send_forward_message(ws, nodes, group_id=group_id, user_id=user_id)
    if sent:
        return

    # 协议端不支持合并转发时退回分页文本，功能不能因此没了
    pages = []
    for start in range(0, len(entries), _MEMORY_PER_PAGE):
        chunk = entries[start:start + _MEMORY_PER_PAGE]
        lines = [title] if start == 0 else [f"{title}（{start + 1}-{start + len(chunk)}）"]
        lines += [body for _uid, body in chunk]
        pages.append("\n\n".join(lines))
    if group_id:
        for i, page in enumerate(pages):
            if i:
                await asyncio.sleep(0.25)
            await bot.send_group_message(ws, group_id, page, is_command_report=True)
    elif user_id:
        await bot.send_private_message(ws, user_id, "\n\n".join(pages), is_command_report=True)


async def generate_agent_command_reply(action_type: str, detail: str = "", is_private: bool = False, group_id: Optional[int] = None) -> str:
    """由大模型根据当前情境临场随机生成拟真执行反馈，杜绝死板模板读条感"""
    headers = {
        "Authorization": f"Bearer {bot.SILICONFLOW_API_KEY}",
        "Content-Type": "application/json"
    }
    
    channel_mode = bot.get_channel_mode(None)
    if channel_mode == "root":
        prompt = (
            f"主人（开发者 life）下达了管理指令：{action_type}，细节：{detail}。\n"
            f"当前是 root 工作模式，请用专业、干练、利落的工程师口吻临场给出 1 句简明执行确认（10~25字以内，拒绝废话，绝不用句号）："
        )
    else:
        prompt = (
            f"主人（life）在{'私聊' if is_private else '群里'}下达了管理指令：{action_type}，执行结果细节：{detail}。\n"
            f"请以大肥鱼/小鲸鱼随性、生动的口吻临场随机给出一句回复（每次措辞自拟，拒绝死板模板，可俏皮、可利落，1 句，15~30字以内，绝不用句号）："
        )

    payload = {
        "model": bot.MODEL_NAME,
        "messages": [
            {"role": "system", "content": bot.ROOT_SYSTEM_PROMPT if bot.get_channel_mode(group_id if not is_private else None) == "root" else bot.SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 50,
        "temperature": 0.85
    }

    try:
        if bot.http_session is None or bot.http_session.closed:
            bot.http_session = aiohttp.ClientSession()
        async with bot.http_session.post(bot.SILICONFLOW_API_URL, headers=headers, json=payload, timeout=8) as resp:
            if resp.status == 200:
                data = await resp.json()
                if "usage" in data:
                    bot.token_tracker.record_usage(data["usage"], bot.MODEL_NAME, group_id=group_id)
                reply = data["choices"][0]["message"]["content"].strip()
                return bot.remove_trailing_period(reply)
    except Exception as e:
        bot.logger.debug("临场生成指令反馈异常: %s", e)
    
    # 极简保底（防止网络超时卡壳）
    return f"已完成 {action_type} ({detail})" if detail else f"已搞定 {action_type}"

