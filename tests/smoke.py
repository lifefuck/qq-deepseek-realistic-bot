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
    sent = await bot.send_segmented_group_message(FakeWS(), 1071994647, "笑死，这也太抽象了 [MEME:嘲讽]")
    texts = [p.get("message") for a, p in calls if isinstance(p.get("message"), str)]
    imgs = sum(1 for a, p in calls if isinstance(p.get("message"), list))
    check("发送返回实际正文", sent == "笑死，这也太抽象了", repr(sent))
    check("正文不含标签", all("MEME" not in t for t in texts))
    check("配图已发送", imgs >= 1, f"imgs={imgs}")

    # 6. 戳一戳不消耗模型
    before = bot.token_tracker.data["total"]["requests"]
    quip = await bot.call_deepseek_poke("测试", is_admin=False, group_id=1071994647)
    check("戳一戳走本地", bool(quip) and bot.token_tracker.data["total"]["requests"] == before)

    # 7. 仪表盘状态能取
    status = bot.get_bot_dashboard_status()
    check("仪表盘状态", isinstance(status, dict) and "groups" in status)

    print()
    if FAILED:
        print("失败项:", ", ".join(FAILED))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
