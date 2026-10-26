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
        return {"status": "ok", "retcode": 0}

    bot.call_action = fake_action

    # 1. 标签解析：正常、全角、缺右括号、无标签
    check("tag 正常", meme_catalog.extract_tag("笑死 [MEME:嘲讽]") == ("笑死", "嘲讽"))
    check("tag 全角", meme_catalog.extract_tag("好耶【MEME：卖萌】") == ("好耶", "卖萌"))
    check("tag 缺右括号", meme_catalog.extract_tag("离谱[MEME:嘲讽")[1] == "")
    check("tag 无标签", meme_catalog.extract_tag("普通一句") == ("普通一句", ""))

    # 2. 口头禅冷却与括号动作
    bot.recent_bot_replies.clear()
    bot.recent_bot_replies.extend(["来口大白饭", "大白饭真香", "今天写代码"])
    out = bot.de_template("这也太离谱了（嚼嚼大白饭）")
    check("括号动作被清掉", "大白饭" not in out and "（" not in out, out)

    # 3. 好感度噪音过滤
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

    print()
    if FAILED:
        print("失败项:", ", ".join(FAILED))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
