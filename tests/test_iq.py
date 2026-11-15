#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「智商」改造的回归测试：思考档位、人设写法、两步读懂、群表达学习。

这些改动全都是「少写一条就悄悄退化」的类型——提示词被改回禁令大全、
档位被改回 low、随机短回被加回来，线上表现只是「它又变笨了」，不会报错。
所以这里既测行为，也盯源码里的关键约定。

跑法：.venv/bin/python tests/test_iq.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import bot as botmod
import prompts
from services import social_read, style_learner

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


def read_src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_effort():
    # 闲聊也必须带思考：low 档下的 flash 只会复述模板句（实测接梗回「折腾啥」）
    check("闲聊默认不是 low", botmod.think_effort() != "low", botmod.think_effort())
    check("被 @ 走 high", botmod.think_effort(is_at=True) == "high")
    check("问事走 high", botmod.think_effort(text="这玩意怎么弄") == "high")


def test_persona():
    p = prompts.SYSTEM_PROMPT
    check("人设压到 1600 字以内", len(p) <= 1600, str(len(p)))
    # 正向样例：被夸/被损/冷场，这三种原来一条都没有，模型只能靠禁令自己猜
    for kw in ("夸你", "熟人损你", "话题冷下来"):
        check(f"人设有「{kw}」的正向样例", kw in p)
    check("有「开口前先看一眼」的读人环节", "开口前先看一眼" in p)
    check("保留防臆想条款", "只对眼前这条消息做反应" in p)
    check("保留 MEME 标记约定", "[MEME:情绪]" in p)
    # 这两条是从用户自己的回法抄来的，别被后来的改稿删掉
    check("被骂时不许反问「哪」", "别反问「哪」" in p)
    check("复述对方原话不算接话", "复述" in p and "往前推一步" in p)
    check("被损的样例是直接顶回去", "我傻个屁" in p)
    # 禁令密度：超过 15 个「别」说明又在往回堆规则，模型注意力会跑偏
    check("禁令不回到三十条", p.count("别") <= 15, str(p.count("别")))


def test_no_forced_short():
    src = read_src("services/llm.py")
    check("没有随机强制短回", "这条短回" not in src)
    check("好感度不再给中间档配语气", "就事论事、平平淡淡" not in src)
    check("中间档 guideline 留空", 'favor_guideline = ""' in src)


def test_timeline_scope():
    src = read_src("handlers/group.py")
    check("旁观上下文收到 10 条", "limit = 12 if is_follow_up else 10" in src)
    check("时间戳阈值是 10 分钟", "delta >= 600" in src)


def test_meme_tag():
    # 模型会写成 [得意] 这种没前缀的，认不出来就会把标签原样发进群
    from meme_catalog import extract_tag
    check("认 [MEME:嘲讽]", extract_tag("这话说的 [MEME:嘲讽]") == ("这话说的", "嘲讽"))
    check("认裸情绪标签", extract_tag("这话说的 [得意]") == ("这话说的", "得意"))
    check("认全角裸标签", extract_tag("行吧【委屈】") == ("行吧", "委屈"))
    check("普通括号不当标签", extract_tag("就这[笑]") == ("就这[笑]", ""))


def test_social_read():
    raw = '```json\n{"who": "小美", "about": "夸你回得好", "to": "你", "act": "接", "how": "收下并小得意"}\n```'
    parsed = social_read._extract_json(raw)
    check("能挖出裹在代码块里的 JSON", isinstance(parsed, dict) and parsed.get("who") == "小美")
    check("裸文本不报错", social_read._extract_json("接，收下") is None)
    check("空输入不报错", social_read._extract_json("") is None)

    yes = {"who": "甲", "about": "在聊显卡", "to": "乙", "act": "接", "how": "补一句价格"}
    no = dict(yes, act="不接")
    check("判定不接", social_read.wants_pass(no))
    check("判定接", not social_read.wants_pass(yes))
    check("缺字段不当成不接", not social_read.wants_pass({"who": "甲"}))
    check("空结果不当成不接", not social_read.wants_pass(None))

    hint = social_read.hint_text(yes)
    check("提示里带上是谁在说什么", "甲" in hint and "显卡" in hint)
    check("提示里给接法", "价格" in hint)
    check("空结果不给提示", social_read.hint_text(None) == "")


def test_style_learner():
    # 合格的接话：同一件事、下一句够短
    check("学到合格的接话", style_learner._usable("这芯片直接认了 再加上这高刷", "米式幽默"))
    check("问句的答案也算接话", style_learner._usable("今晚吃啥啊", "火锅"))
    # 不合格的：指令、表情码、机器人通知、两句不搭界
    check("跳过云崽指令", not style_learner._usable("来点色图", "#签到"))
    check("跳过引用块", not style_learner._usable("这波什么情况", "[引用某人: \"啊\"] 是啊"))
    check("跳过欢迎语", not style_learner._usable("有人吗", "欢迎新人~"))
    # 接梗不重合也要学得到：「米式幽默」跟上一句没有一个共同字
    check("学到短反应式的接梗", style_learner._usable("这芯片直接认了 再加上这高刷", "米式幽默"))
    check("跳过不搭界的长对子", not style_learner._usable("这芯片直接认了 再加上这高刷", "最近有点失眠真的烦死了"))
    check("跳过纯数字", not style_learner._usable("今晚吃啥啊", "111"))
    check("跳过太长的下一句",
          not style_learner._usable("这波确实离谱", "我觉得这个问题需要从三个完全不同的角度来分析一下再决定"))


def main():
    for fn in (test_effort, test_persona, test_no_forced_short, test_timeline_scope,
               test_meme_tag, test_social_read, test_style_learner):
        fn()
    print()
    if FAILED:
        print(f"{PASSED} passed, {len(FAILED)} failed")
        for f in FAILED:
            print("  -", f)
        sys.exit(1)
    print(f"全部通过（{PASSED} 项）")


if __name__ == "__main__":
    main()
