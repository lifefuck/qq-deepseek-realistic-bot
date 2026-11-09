#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拟真机制的回归测试：节奏、反模板、错字、心境、接话时机。

这些模块的共性是「有随机、有时序」，所以断言只看边界和不变量，
不写死具体数值——否则每次调参都要改测试。

跑法：.venv/bin/python tests/test_humanlike.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import affect, anti_template, interject_planner, pacing, typo

PASSED = 0
FAILED = []


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"PASS {name}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f"FAIL {name} {detail}")


def test_pacing():
    samples = [pacing.segment_delay("今天这波确实有点离谱", 14) for _ in range(40)]
    check("节奏下界不低于 0.55s", min(samples) >= 0.55, f"min={min(samples):.2f}")
    check("节奏上界不超过 8s", max(samples) <= 8.0, f"max={max(samples):.2f}")
    check("节奏有抖动不是固定值", len({round(s, 2) for s in samples}) > 20)
    # 长句打字更久：比较均值，单次比较会被随机抖动打败
    short = sum(pacing.segment_delay("好", 14) for _ in range(60)) / 60
    long = sum(pacing.segment_delay("这句话写得特别长" * 6, 14) for _ in range(60)) / 60
    check("长句间隔大于短句", long > short + 0.3, f"{long:.2f} vs {short:.2f}")
    night = sum(pacing.reading_delay(3) for _ in range(60)) / 60
    day = sum(pacing.reading_delay(14) for _ in range(60)) / 60
    check("深夜阅读更慢", night > day, f"{night:.2f} vs {day:.2f}")


def test_anti_template():
    recent = ["不是，这也太离谱了", "好家伙，又来"]
    check("撞起手能被识别", anti_template.match("不是，你认真的吗", recent) == "opening")
    check("不撞就不报", anti_template.match("今天天气不错", recent) == "none")

    tails = ["真服了呀", "我也服了呀"]
    check("撞收尾能被识别", anti_template.match("这下服了呀", tails) == "ending")

    out = anti_template.reshape("不是，这也太离谱了", "opening")
    check("语气起手被削掉", not out.startswith("不是"), out)

    # 削形不能把话削没
    for reason in ("opening", "ending"):
        for text in ("？", "好", "不是", "短句"):
            got = anti_template.reshape(text, reason)
            check(f"削形不返回空({reason}/{text})", bool(got.strip()))

    report = anti_template.shape_report("不是，这也太离谱了吧？")
    check("形状快照识别问句", report["has_question"] is True)


def test_typo():
    # 链接、@、数字一律不碰
    protected = [
        "看这个 https://example.com/a/b 真的很长",
        "戳一下 @某人 这个真的很好用",
        "[CQ:at,qq=123456] 你看看这个",
        "版本 1.2.3 更新了真的很久",
        "短句",
    ]
    for text in protected:
        hits = [typo.maybe_typo(text, prob=1.0) for _ in range(200)]
        check(f"受保护文本不改({text[:12]})", all(h is None for h in hits))

    # 长句允许改，但只改一处、长度变化很小
    base = "我今天觉得这个时候应该已经到家了吧"
    results = [typo.maybe_typo(base, prob=1.0) for _ in range(200)]
    results = [r for r in results if r]
    check("长句能被改", len(results) > 100, f"{len(results)}/200")
    check("改动幅度很小", all(abs(len(r.text) - len(base)) <= 1 for r in results))
    check("纠正句包含正确词", all(r.right in typo.correction_line(r) for r in results))


def test_affect():
    affect.reset()
    now = time.time()

    # 别扭会随时间回落
    st = affect._get(1)
    st["pout"] = 1.0
    st["updated"] = now - 3 * 3600
    affect.settle(st, now)
    check("别扭三小时后明显回落", st["pout"] < 0.2, f"{st['pout']:.2f}")

    # 被哄能破功
    affect.reset()
    st = affect._get(2)
    st["pout"] = 0.8
    affect.on_group_message(2, "对不起嘛，别生气了", now=now)
    check("被哄后别扭大降", st["pout"] < 0.2, f"{st['pout']:.2f}")

    # 问正事时情绪让路
    affect.reset()
    st = affect._get(3)
    st["pout"] = 0.9
    affect.on_group_message(3, "帮我看看这个报错怎么配置", now=now)
    check("正事压过别扭", st["pout"] < 0.4, f"{st['pout']:.2f}")

    # 负面好感会积累别扭
    affect.reset()
    affect.on_favor_delta(4, -5, "你个笨蛋")
    check("被怼会别扭", affect._get(4)["pout"] > 0.2)

    # 牵挂账本：4 小时内不问，问两次封顶
    affect.reset()
    st = affect._get(5)
    affect.on_group_message(5, "最近加班累死了", now=now)
    check("记下牵挂", len(st["care"]) == 1)
    check("没到点不问", affect.care_hint(5, now=now) == "")
    first = affect.care_hint(5, now=now + 5 * 3600)
    check("到点会问", "加班" in first or "赶事" in first, first)
    check("八小时内不追问", affect.care_hint(5, now=now + 5 * 3600 + 3600) == "")
    second = affect.care_hint(5, now=now + 14 * 3600)
    check("隔八小时可再问一次", bool(second))
    check("两次封顶", affect.care_hint(5, now=now + 30 * 3600) == "")

    # 体感 memo 不能出现数字或状态标签
    affect.reset()
    st = affect._get(6)
    st["pout"] = 0.9
    memo = affect.prompt_memo(6, now=now)
    check("有别扭时给体感", bool(memo))
    check("memo 不含数字", not any(ch.isdigit() for ch in memo), memo)
    check("memo 不报状态名", "valence" not in memo and "数值" not in memo)

    # 表达参数永远在合理区间
    p = affect.profile(6, now=now)
    check("条数上限 1~3", 1 <= p["max_segments"] <= 3)
    check("表情上限 0~3", 0 <= p["max_faces"] <= 3)
    check("节奏倍率 0.5~2", 0.5 <= p["delay_mult"] <= 2.0)
    check("表情密度 0~1", 0 <= p["emoji_keep"] <= 1)


def test_persist():
    """落盘回归：曾经因为 save() 里 _dirty 是局部变量，状态永远写不出去。"""
    import json
    import tempfile

    tmp = os.path.join(tempfile.mkdtemp(), "affect.json")

    def writer(path, data, pretty=False):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    affect.reset()
    affect.attach(writer, tmp)
    affect.on_group_message(77, "最近加班到半夜，烦死了")
    check("首条消息立即落盘", os.path.exists(tmp))
    on_disk = json.load(open(tmp, encoding="utf-8"))
    check("落盘内容含该群", "77" in on_disk, str(list(on_disk)))
    check("落盘内容含牵挂", bool(on_disk["77"]["care"]))

    # 模拟重启：清内存后从文件恢复
    affect._state.clear()
    affect.load()
    check("重启后恢复状态", "77" in affect._state and affect._state["77"]["care"])

    affect.reset()
    affect.attach(None, "")


def test_prompt_injection():
    """心境必须真的进 prompt——线上一旦为空，只是回复变平，不会报错。"""
    from services import llm

    affect.reset()
    now = time.time()
    st = affect._get(88)
    st["pout"] = 0.9
    st["valence"] = -0.5
    ctx = llm.affect_context(88, False)
    check("心情差时注入体感", "堵" in ctx or "低" in ctx, ctx[:60])
    check("注入内容不含数字", not any(ch.isdigit() for ch in ctx), ctx[:60])

    affect.reset()
    ctx_private = llm.affect_context(None, True)
    check("私聊也能取到状态", isinstance(ctx_private, str))
    affect.reset()


def test_echo_and_particle():
    """群 900000001 的实测样本：每条都是「复述+问号 → 我…啊」。"""
    check("回声开场被砍",
          anti_template.strip_echo_opener("cpdd？你认真的啊，突然来这么一句", "cpdd") ==
          "你认真的啊，突然来这么一句")
    check("不是回声不动", anti_template.strip_echo_opener(
        "神子？我哪来的神子啊", "还得改") == "神子？我哪来的神子啊")
    check("砍完太短就不砍", anti_template.strip_echo_opener("cpdd？", "cpdd") == "cpdd？")
    check("没问号不动", anti_template.strip_echo_opener(
        "cpdd 是啥玩意", "cpdd") == "cpdd 是啥玩意")

    same = ["我脑子还没转过来啊", "我这话说得挺正常的啊"]
    check("连续同语气词摘掉", anti_template.trim_particle("那我多没面子啊", same) == "那我多没面子")
    check("语气词不重复就留", anti_template.trim_particle("那我多没面子啊", ["行吧", "走了"]) == "那我多没面子啊")


def test_newline_split():
    """线上实测：模型回了两行，结果整条带着回车发出去了。"""
    import bot  # noqa: F401  先加载宿主，messaging 里的 bot 引用才有值
    from services import messaging

    reply = "哦？这是在问我刚才说话的样子顺不顺眼吗\n还行吧，比之前那堆翻译腔强多了"
    segs = messaging.split_reply_segments(reply)
    check("两行拆成两条", len(segs) == 2, str(segs))
    check("拆后没有裸换行", all("\n" not in s for s in segs), str(segs))


def test_short_bubbles():
    """十几个字一条、还拿逗号串着，是群里最像 AI 的形状。"""
    from services import messaging

    segs = messaging.shape_short(["刚吃完饭，然后下楼买了杯咖啡，顺手把快递拿了，今天人还挺多"])
    check("长句切成短气泡", len(segs) >= 3, str(segs))
    check("每条不超过 11 字", all(len(x) <= 11 for x in segs), str(segs))
    check("短句原样放过", messaging.shape_short(["就这"]) == ["就这"])
    check("没逗号不硬拆", messaging.shape_short(["你是不是又偷偷把服务器重启了"]) == ["你是不是又偷偷把服务器重启了"])
    check("不吐尾逗号", all(not x.endswith("，") for x in segs), str(segs))


def test_offtopic_gate():
    """旁观插话跟刚才聊的零交集，就是在自说自话。"""
    from services import anti_template

    check("不沾边判无关", anti_template.is_offtopic("这波操作真的离谱到没边了", "今晚吃什么 火锅还是烤肉"))
    check("沾边不误伤", not anti_template.is_offtopic("火锅太辣了", "今晚吃什么 火锅还是烤肉"))
    check("短句不拦", not anti_template.is_offtopic("笑死", "今晚吃什么 火锅还是烤肉"))


def test_quote_reply():
    """引用回复要能认出来「引用的是谁」，不能只靠 event["reply"]。"""
    import asyncio
    import bot

    class WS:
        closed = False

    async def fake_call(ws, action, params, timeout=None):
        assert action == "get_msg"
        return {"status": "ok", "data": {
            "sender": {"user_id": bot.BOT_QQ, "nickname": "小鲸鱼"},
            "raw_message": "我什么没见过，这话一出来就知道你要开始吹了",
        }}

    orig = bot.call_action
    bot.call_action = fake_call
    try:
        meta = asyncio.run(bot.extract_reply_meta(WS(), {}, "[CQ:reply,id=123] 你刚才说啥"))
    finally:
        bot.call_action = orig

    check("引用能回查出原话", "我什么没见过" in ((meta or {}).get("text") or ""))
    check("引用自己认得出来", str((meta or {}).get("sender_id")) == str(bot.BOT_QQ))
    check("引用自己写成「引用你」", "引用你" in ((meta or {}).get("text") or ""))


def test_recall_interrupt():
    """撤回要真的掐掉还在排队的回复，而不是只记个日志。"""
    import asyncio
    import bot

    async def scenario():
        async def slow():
            await asyncio.sleep(5)

        task = asyncio.create_task(slow())
        mid = 987654321
        bot.track_reply_task(mid, task)
        bot.group_timelines[999].append({
            "sender": "甲(QQ 1)", "sender_id": 1, "content": "撤回我", "time": 0, "msg_id": mid,
        })
        await bot.handle_notice_event(None, {
            "post_type": "notice", "notice_type": "group_recall",
            "group_id": 999, "user_id": 1, "message_id": mid,
        })
        await asyncio.sleep(0)
        return (task.cancelled(), bot.was_recalled(mid),
                any(m.get("msg_id") == mid for m in bot.group_timelines[999]))

    cancelled, marked, still_there = asyncio.run(scenario())
    check("撤回会取消回复任务", cancelled)
    check("撤回会被记下", marked)
    check("撤回的消息从时间线移除", not still_there)


def test_meme_recall():
    """梗库 1900 条，靠的是检索而不是全塞进 prompt。"""
    from meme_manager import meme_manager as mm

    mm.load_pool(force=True)
    check("核心梗已加载", len(mm.core_memes) >= 8, str(len(mm.core_memes)))
    check("精确命中打瓦", [m["name"] for m in mm.match_user_message("打瓦吗兄弟")][:1] == ["打瓦"])
    check("别名命中v我50", "疯狂星期四" in [m["name"] for m in mm.match_user_message("v我50")])
    check("打错字也能召回", "卡拉彼丘" in [m["name"] for m in mm.match_user_message("今晚卡啦彼丘开黑")])
    check("两字角色名照常命中", "白墨" in [m["name"] for m in mm.match_user_message("白墨这奶量")])
    check("不沾边不乱认", mm.match_user_message("今晚吃火锅") == [])
    snippet = mm.get_prompt_snippet("今晚卡啦彼丘开黑")
    check("核心梗常驻在提示词里", "卡拉彼丘→" in snippet)
    check("梗提示不超 600 字", len(snippet) <= 600, str(len(snippet)))


def test_interject():
    now = 10000.0
    check("刚说完不抢话", interject_planner.too_soon(now, now - 10))
    check("隔久了可以接", not interject_planner.too_soon(now, now - 120))

    s, why = interject_planner.adjust(0.8, now=now, last_bot_reply=now - 10)
    check("刚说完降权", s < 0.8 and "刚说过" in why, f"{s:.2f} {why}")
    s, why = interject_planner.adjust(0.5, now=now, last_bot_reply=now - 300)
    check("热聊窗口提权", s > 0.5 and "热聊" in why, f"{s:.2f} {why}")
    s, why = interject_planner.adjust(0.6, now=now, last_bot_reply=now - 300,
                                     msgs_since_bot=10, last_reply_answered=True)
    check("刷屏提权且有人接", "刷屏" in why and "上条没人接" not in why, why)
    s, why = interject_planner.adjust(0.6, now=now, last_bot_reply=now - 300,
                                     msgs_since_bot=10, last_reply_answered=False)
    check("上条没人接要降权", "上条没人接" in why, why)
    s, why = interject_planner.adjust(1.0, now=now, last_bot_reply=now - 300,
                                     last_reply_answered=False)
    check("分数不越界", 0 <= s <= 1, f"{s:.2f}")


def test_long_clause_split():
    """线上实测：模型一口气写 20 多字不带逗号，「2~10 字」的规则等于没有。"""
    from services import messaging

    segs = messaging.shape_short(["你们是集体被devinfo分区吸进去了还是咋的"])
    check("无逗号长句也切开", len(segs) >= 2, str(segs))
    check("切完每条不超过 18 字", all(len(x) <= 18 for x in segs), str(segs))
    check("不切出俩字残句", all(len(x) >= 4 for x in segs), str(segs))

    segs2 = messaging.shape_short(["[CQ:at,qq=1234567890] 这个仓库 https://github.com/a/b 你自己看吧"])
    joined = "".join(segs2)
    check("CQ 码没被从中间切断", joined.count("[CQ:at,qq=1234567890]") == 1, str(segs2))
    check("链接整段保留", "https://github.com/a/b" in joined, str(segs2))


def test_de_cliche():
    """「这波/属于是」连着几条出现，是 AI 味最明显的指纹。"""
    from services import messaging

    check("重复出现的模板词被摘掉",
          messaging.de_cliche("拼多多这波是真把我看傻了", ["上一条也这波"]) == "拼多多是真把我看傻了",
          messaging.de_cliche("拼多多这波是真把我看傻了", ["上一条也这波"]))
    check("没重复就保留原句",
          messaging.de_cliche("拼多多这波是真把我看傻了", []) == "拼多多这波是真把我看傻了")
    check("删词不把句子删没", messaging.de_cliche("这波", ["这波"]) == "这波")


def test_context_hygiene():
    """自己的长篇历史会被模型当范文照抄；裸 QQ 号会被当成名字念。"""
    import bot as host
    from handlers import group as gh

    bot_qq = host.BOT_QQ
    timeline = [
        {"sender": f"小鲸鱼(你自己)(QQ {bot_qq})", "sender_id": bot_qq, "content": "更早那句自己说的"},
        {"sender": "甲(QQ 1)", "sender_id": 1, "content": "人呢"},
        {"sender": f"小鲸鱼(你自己)(QQ {bot_qq})", "sender_id": bot_qq, "content": "刚说的那句"},
        {"sender": "乙(QQ 2)", "sender_id": 2, "content": "在"},
    ]
    kept = gh._compose_dialogue(timeline, 8)
    own = [m for m in kept if m["sender_id"] == bot_qq]
    check("上下文里自己只留最近一条", len(own) == 1 and own[0]["content"] == "刚说的那句", str(kept))
    check("别人的话一条不少", len([m for m in kept if m["sender_id"] != bot_qq]) == 2)

    host.group_member_nicks[999] = {123456: "老王"}
    check("裸 QQ 号补上昵称", gh._annotate_qq("骂123456", 999) == "骂老王(QQ 123456)",
          gh._annotate_qq("骂123456", 999))
    check("不认识的号原样保留", gh._annotate_qq("骂123456", 998) == "骂123456")


def test_action_intents():
    """「命令」得真执行，不能又回一句「攻击谁啊」。"""
    from handlers import actions

    check("识别无差别攻击", bool(actions.RE_ATTACK_ALL.search("给我无差别攻击所有人")))
    check("识别群嘲", bool(actions.RE_ATTACK_ALL.search("群嘲一下他们")))
    check("不误伤正常讨论", not actions.RE_ATTACK_ALL.search("这游戏攻击力多少"))
    check("越线台词被拦下", actions._clean_line("你全家都傻") == "")
    check("正常损话留着", actions._clean_line("就你这发言水平") == "就你这发言水平")


if __name__ == "__main__":
    test_pacing()
    test_anti_template()
    test_typo()
    test_affect()
    test_persist()
    test_prompt_injection()
    test_echo_and_particle()
    test_newline_split()
    test_short_bubbles()
    test_long_clause_split()
    test_de_cliche()
    test_context_hygiene()
    test_action_intents()
    test_offtopic_gate()
    test_meme_recall()
    test_quote_reply()
    test_recall_interrupt()
    test_interject()
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        for item in FAILED:
            print("  -", item)
        sys.exit(1)
