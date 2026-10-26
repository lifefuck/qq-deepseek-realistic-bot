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

    # 2. 口头禅冷却与括号动作
    bot.recent_bot_replies.clear()
    _g = 123456789
    bot.recent_bot_replies[_g].extend(["来口大白饭", "大白饭真香", "今天写代码"])
    out = bot.de_template("这也太离谱了（嚼嚼大白饭）", _g)
    check("括号动作被清掉", "大白饭" not in out and "（" not in out, out)

    # 2.5 人设自称与整句复读
    bot.recent_bot_replies.clear()
    check("自称换成我", bot._strip_self_ref("本鲸宝要去喝水") == "我要去喝水")
    check("不出现我我", "我我" not in bot._strip_self_ref("这是我大肥鱼本鱼的手笔"))
    same = "吵死啦！本大肥鱼也是需要休息的，等会"
    first = bot.de_template(same, _g)
    check("第一次照原样发", first == same)
    bot.recent_bot_replies[_g].append(first)
    second = bot.de_template(same, _g)
    check("复读时去掉自称", "本大肥鱼" not in second, second)
    bot.recent_bot_replies[_g].append(second)
    third = bot.de_template(same, _g)
    check("第三次再撞就削半句", third != second and len(third) < len(second), third)
    bot.recent_bot_replies[_g].extend(["本鲸宝真累", "本大肥鱼要睡了"])
    check("回喂提示带禁自称", "别再自称" in bot.get_anti_repeat_hint(_g))
    bot.recent_bot_replies.clear()

    # 2.6 提示词里「只回应真实发生的事」不能丢，这是防臆想的核心约束
    check("人设有防臆想条款", "只回应真实发生的事" in bot.SYSTEM_PROMPT)
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

    print()
    if FAILED:
        print("失败项:", ", ".join(FAILED))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
