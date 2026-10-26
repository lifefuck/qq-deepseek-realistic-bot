#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
重启前的冒烟测试。不起网络、不发真消息，用假的 call_action 把关键链路跑一遍。

用法：.venv/bin/python tests/smoke.py
退出码 0 表示全过。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot
import fun_features
import group_stats
import meme_catalog
import prompts
import safety_guard

FAILED = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILED.append(name)


class FakeWS:
    pass


async def main():
    calls = []

    async def fake_action(ws, action, params, timeout=2.0):
        calls.append((action, params))
        if action == "get_group_member_list":
            # 给 /memary 用的假成员资料：一个群主带头衔，一个没名片
            return {"status": "ok", "retcode": 0, "data": [
                {"user_id": 100000, "nickname": "QQ昵称0", "card": "群名片0",
                 "title": "摸鱼冠军", "role": "owner"},
                {"user_id": 100001, "nickname": "QQ昵称1", "card": "", "title": "", "role": "member"},
            ]}
        return {"status": "ok", "retcode": 0}

    bot.call_action = fake_action

    # 1. 标签解析：正常、全角、缺右括号、无标签
    check("tag 正常", meme_catalog.extract_tag("笑死 [MEME:嘲讽]") == ("笑死", "嘲讽"))
    check("tag 全角", meme_catalog.extract_tag("好耶【MEME：卖萌】") == ("好耶", "卖萌"))
    check("tag 缺右括号", meme_catalog.extract_tag("离谱[MEME:嘲讽")[1] == "")
    check("tag 无标签", meme_catalog.extract_tag("普通一句") == ("普通一句", ""))

    # 2. 反模板闸：只挡原样复读，风格不管
    bot.recent_bot_replies.clear()
    _g = 123456789
    same = "吵死啦！本大肥鱼也是需要休息的，等会"
    check("第一次照原样发", bot.de_template(same, _g) == same)
    bot.recent_bot_replies[_g].append(same)
    shorter = bot.de_template(same, _g)
    check("复读时削短", len(shorter) < len(same) and bot._skeleton(shorter) in bot._skeleton(same), shorter)
    check("回喂提示是原话", same in bot.get_anti_repeat_hint(_g))
    check("提示里不写词表规则", "不许" not in bot.get_anti_repeat_hint(_g))
    check("不同的话不误伤", bot.de_template("今天天气不错", _g) == "今天天气不错")
    bot.recent_bot_replies.clear()

    # 2.6 提示词里「只对眼前这条消息做反应」不能丢，这是防臆想的核心
    check("人设有防臆想条款", "只对眼前这条消息做反应" in bot.SYSTEM_PROMPT)
    check("大肥鱼人设保留", "大肥鱼" in bot.SYSTEM_PROMPT)

    check("水聊跳过", not bot._is_favor_worthy("哈哈哈"))
    check("短句跳过", not bot._is_favor_worthy("在吗"))
    check("正常发言打分", bot._is_favor_worthy("你这大肥鱼今天挺活跃"))

    # 4. 面板清单路由
    check("问面板要注入", prompts.needs_games_kb("原神怎么绑定uid"))
    check("只提游戏名不注入", not prompts.needs_games_kb("原神真好玩"))

    # 5. 发送链路：标签要剥掉，图要发出去
    calls.clear()
    sent = await bot.send_segmented_group_message(FakeWS(), 123456789, "笑死，这也太抽象了 [MEME:嘲讽]")
    texts = [p.get("message") for a, p in calls if isinstance(p.get("message"), str)]
    imgs = sum(1 for a, p in calls if isinstance(p.get("message"), list))
    check("发送返回实际正文", sent == "笑死，这也太抽象了", repr(sent))
    check("正文不含标签", all("MEME" not in t for t in texts))
    check("配图已发送", imgs >= 1, f"imgs={imgs}")

    # 6. 戳一戳不消耗模型
    before = bot.token_tracker.data["total"]["requests"]
    quip = await bot.call_deepseek_poke("测试", is_admin=False, group_id=123456789)
    check("戳一戳走本地", bool(quip) and bot.token_tracker.data["total"]["requests"] == before)

    # 6.5 表情抽稀：该摘的时候必须摘干净，不能留下双空格或断句
    check("表情能摘掉", bot.thin_emoji("在啊 咋了 😅", keep_ratio=0) == "在啊 咋了")
    check("句中表情摘完不留空格", bot.thin_emoji("显卡没用 🙄 认命吧", keep_ratio=0) == "显卡没用 认命吧")
    check("纯表情不摘成空消息", bot.thin_emoji("😅", keep_ratio=0) == "😅")
    check("表情该留时留着", bot.thin_emoji("行 👉", keep_ratio=1) == "行 👉")
    check("放行也只留一个", bot.thin_emoji("笑死 😂 这人 🤣", keep_ratio=1) == "笑死 😂 这人")
    check("组合表情能整体识别", bot.thin_emoji("晕了 😵‍💫", keep_ratio=0) == "晕了")
    check("梗组合不被拆开", bot.thin_emoji("懂了 🤓👆 这波", keep_ratio=1) == "懂了 🤓👆 这波")

    # 7. 仪表盘状态能取
    status = bot.get_bot_dashboard_status()
    check("仪表盘状态", isinstance(status, dict) and "groups" in status)

    # 8. 群统计与玩法：年鉴/关系网/周榜要真能出图发出去
    GID = 999999999
    group_stats._stats.pop(str(GID), None)
    for i in range(60):
        group_stats.record_message(GID, 1000 + (i % 7), f"群友{i % 7}", "这是一句测试发言内容", hour=(2 if i % 9 == 0 else 21))
    group_stats.record_pair(GID, 1000, 1001)
    group_stats.record_pair(GID, 1000, 1001)
    top = group_stats.top_users(GID, 3)
    check("统计能排榜", bool(top) and top[0][2] > 0, str(top[:2]))

    calls.clear()
    await fun_features.send_yearbook(FakeWS(), GID)
    await fun_features.send_relation_map(FakeWS(), GID)
    await fun_features.send_weekly_board(FakeWS(), GID)
    imgs = sum(1 for a, p in calls if isinstance(p.get("message"), list))
    check("三张卡片都能出图", imgs >= 3, f"imgs={imgs}")

    # 9. 图灵投票：数字进得去，非数字不抢
    # 揭晓会给猜对的人加好感并落盘，测试期间把这步挡掉，别把假群友写进真实记忆库
    _real_save = bot.save_user_memories
    bot.save_user_memories = lambda *a, **k: None
    fun_features._turing[GID] = {"options": ["真话在这", "假话一", "假话二"], "answer": 1,
                                 "votes": {}, "start": 0}
    check("图灵票号识别", fun_features.record_turing_vote(GID, 1000, " 1 "))
    check("图灵非数字放行", not fun_features.record_turing_vote(GID, 1001, "我猜第二个"))
    calls.clear()
    await fun_features.reveal_turing(FakeWS(), GID)
    bot.save_user_memories = _real_save
    check("图灵能揭晓", any("揭晓" in str(p.get("message")) for _a, p in calls) and GID not in fun_features._turing)
    bot.user_memories.pop(str(GID), None)
    group_stats._stats.pop(str(GID), None)

    # 10. 指令路由：群里发 /年鉴 要真的走到玩法那层，不能被云崽前缀或管理员指令截走
    check("年鉴不被云崽吞", not bot.is_game_or_yunzai_command("/年鉴"))
    check("图灵不被云崽吞", not bot.is_game_or_yunzai_command("/图灵"))

    import handlers.group as hgroup
    gid = 999999998
    bot.ALLOWED_GROUPS.add(gid)
    group_stats._stats.pop(str(gid), None)
    for i in range(60):
        group_stats.record_message(gid, 1000, "测试群友", "测试发言内容够长了", hour=21)
    calls.clear()
    await hgroup.handle_group_message(FakeWS(), {
        "group_id": gid, "user_id": 1000,
        "sender": {"nickname": "测试群友"},
        "raw_message": "/年鉴",
        "message": [{"type": "text", "data": {"text": "/年鉴"}}],
    })
    imgs = sum(1 for a, p in calls if isinstance(p.get("message"), list))
    check("群里发 /年鉴 会出图", imgs >= 1, f"imgs={imgs}")
    bot.ALLOWED_GROUPS.discard(gid)
    group_stats._stats.pop(str(gid), None)

    # 11. 作息区间与 /off 总闸
    check("23 点算睡觉", bot.in_sleep_window(23))
    check("凌晨 3 点算睡觉", bot.in_sleep_window(3))
    check("早上 6:59 还算", bot.in_sleep_window(6))
    check("早上 7 点醒了", not bot.in_sleep_window(7))
    check("下午不该睡", not bot.in_sleep_window(15))

    gid = 999999997
    bot.group_configs[str(gid)] = dict(bot.DEFAULT_GROUP_CONFIG)
    # /off 会落盘配置，测试期间挡掉，别把假群号写进 group_configs.json
    _real_save_cfg = bot.save_group_configs
    bot.save_group_configs = lambda: None
    calls.clear()
    await bot.handle_admin_command(FakeWS(), "/off", is_private=False, group_id=gid)
    cfg = bot.get_group_config(gid)
    check("/off 关全部功能",
          cfg["muted"] and not cfg["enable_interject"] and not cfg["enable_poke"]
          and not cfg["enable_games"] and not cfg["enable_search"] and not cfg["enable_fun"])
    check("/off 不把作息关掉（enable_sleep 语义是启用作息）", cfg["enable_sleep"] is True)

    await bot.handle_admin_command(FakeWS(), "/on", is_private=False, group_id=gid)
    cfg = bot.get_group_config(gid)
    check("/on 全开", not cfg["muted"] and cfg["enable_interject"] and cfg["enable_fun"])
    bot.save_group_configs = _real_save_cfg
    bot.group_configs.pop(str(gid), None)

    # 12. /memary：合并转发成一条折叠记录，节点里要有 QQ 号、身份、头衔
    calls.clear()
    admin_mod = __import__("handlers.admin", fromlist=["_MEMORY_CACHE", "_MEMBER_CACHE"])
    admin_mod._MEMORY_CACHE.clear()
    admin_mod._MEMBER_CACHE.clear()
    gid = 999999996
    bot.user_memories[str(gid)] = {
        str(100000 + i): {"nickname": f"群友{i}", "favor": 50 + i % 40,
                          "notes": ["爱水群"], "updated_at": 0} for i in range(30)
    }
    await bot.handle_memory_summary_command(FakeWS(), group_id=gid)
    fwd = [p for a, p in calls if a == "send_group_forward_msg"]
    check("/memary 走合并转发", len(fwd) == 1, f"forward 次数={len(fwd)}")
    nodes = fwd[0]["messages"] if fwd else []
    body = "\n".join(n["data"]["content"] for n in nodes)
    check("节点数是标题+30人", len(nodes) == 31, f"节点={len(nodes)}")
    check("节点带 QQ 号", "QQ 100000" in body)
    check("节点带群身份", "群主" in body and "群员" in body)
    check("节点带头衔", "摸鱼冠军" in body)
    check("群名片与QQ昵称不同时都显示", "群名片0（QQ昵称 QQ昵称0）" in body)
    bot.user_memories.pop(str(gid), None)

    # 仓库地址唤醒：问项目就甩链接，闲聊和别的仓库不能误伤
    repo = lambda t: prompts.repo_ask_reply(t) or ""
    check("问源码直接给地址", "github.com/lifefuck/qq-deepseek-realistic-bot" in repo("这个bot源码在哪"))
    check("问部署给地址", "github.com/lifefuck" in repo("怎么部署"))
    check("闲聊不抢话", prompts.repo_ask_reply("今天吃啥") is None)
    check("别的仓库不误伤", prompts.repo_ask_reply("我们公司仓库地址迁移了，明天开会讨论") is None)
    check("知识库不再提已停用功能", "答案之书" not in prompts.GAMES_KB and "摸摸头" not in prompts.GAMES_KB)

    # 仓库链接必须原样发出去，不能再被插汉字
    u = "https://github.com/lifefuck/qq-deepseek-realistic-bot"
    ob = bot.obfuscate_urls_for_qq(u)
    check("链接插删字且能还原", ob.split("（")[0].replace("删", "") == u and "删" in ob)
    check("链接带去除提示", "去掉" in ob)
    check("其他域名同样处理", "删" in bot.obfuscate_urls_for_qq("https://example.com/a"))
    # 模型自己加的括号旁白不能发出去
    check("纯括号旁白被丢弃", safety_guard.strip_internal_monologue("（保持简洁自然，不用特意打招呼或回忆过往）") == "")
    check("正常括号内容保留", safety_guard.strip_internal_monologue("（笑）行吧") != "")

    print()
    if FAILED:
        print("失败项:", ", ".join(FAILED))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
