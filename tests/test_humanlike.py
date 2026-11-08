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
    check("节奏下界不低于 0.8s", min(samples) >= 0.8, f"min={min(samples):.2f}")
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
    ctx = llm.affect_context(88, False, False)
    check("心情差时注入体感", "堵" in ctx or "低" in ctx, ctx[:60])
    check("注入内容不含数字", not any(ch.isdigit() for ch in ctx), ctx[:60])

    affect.reset()
    st = affect._get(89)
    st["pout"] = 0.9
    ctx_root = llm.affect_context(89, False, True)
    check("root 干活时不加换说法", "换个说法" not in ctx_root, ctx_root[:60])

    ctx_private = llm.affect_context(None, True, False)
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


if __name__ == "__main__":
    test_pacing()
    test_anti_template()
    test_typo()
    test_affect()
    test_persist()
    test_prompt_injection()
    test_echo_and_particle()
    test_newline_split()
    test_interject()
    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        for item in FAILED:
            print("  -", item)
        sys.exit(1)
