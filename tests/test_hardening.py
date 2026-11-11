#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板鉴权/输入校验与落盘自愈的回归测试。

这些用例都是踩过的坑：CF 头后门、路径穿越、任意配置写入、记忆库读坏后被空库覆盖。
跑法：.venv/bin/python tests/test_hardening.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp.test_utils import TestServer, TestClient

import bot
import web_dashboard
from services import messaging

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


async def test_dashboard():
    os.environ["DASHBOARD_AUTH"] = "tester:T3stPass-2026"
    # 测试期间不要把会话写进项目里的真实文件
    web_dashboard.SESSION_FILE = os.path.join(tempfile.mkdtemp(), "sessions.json")
    web_dashboard.ACTIVE_SESSIONS.clear()
    web_dashboard.LOGIN_FAILURES.clear()
    web_dashboard.LOCKED_IPS.clear()

    app = await web_dashboard.create_web_app(lambda: {"ok": True}, lambda gid, k, v: True)
    async with TestClient(TestServer(app)) as client:
        r = await client.get("/api/status")
        check("未登录访问状态接口返回 401", r.status == 401, f"got {r.status}")

        r = await client.get("/api/status", headers={"Cf-Access-Authenticated-User-Email": "a@b.c"})
        check("伪造 CF 认证头不再放行", r.status == 401, f"got {r.status}")

        # 直接从登录处理函数里取字段名，改名字也不会让测试失效
        import inspect, re
        login_key = re.search(r'body\.get\("([^"]+)"', inspect.getsource(web_dashboard)).group(1)

        def login_body(pwd):
            return {login_key: pwd}

        r = await client.post("/api/login", json=login_body("wrong"))
        check("错误密码返回 403", r.status == 403, f"got {r.status}")

        r = await client.post("/api/login", json=login_body("T3stPass-2026"))
        check("正确密码登录成功", r.status == 200, f"got {r.status}")

        r = await client.get("/api/status")
        check("登录后可读取状态", r.status == 200, f"got {r.status}")

        r = await client.get("/assets/%2e%2e/%2e%2e/%2e%2e/etc/passwd")
        check("静态资源路径穿越被拦住", r.status in (401, 404), f"got {r.status}")

        r = await client.post("/api/group/config", json={"group_id": 1, "key": "hack_key", "value": 1})
        check("未知配置项被拒绝", r.status == 400, f"got {r.status}")

        r = await client.post("/api/group/config", json={"group_id": 1, "key": "interject_prob", "value": 3})
        check("面板不再暴露的开关被拒绝", r.status == 400, f"got {r.status}")

        r = await client.post("/api/group/config", json={"group_id": 1, "key": "muted", "value": True})
        check("合法配置可以写入", r.status == 200, f"got {r.status}")

        r = await client.get("/")
        body = await r.text()
        check("登录页仍可访问", r.status == 200 and "<html" in body.lower())


async def test_json_recovery():
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "mem.json")
    try:
        bot.atomic_write_json(path, {"a": 1})
        check("原子写出的 JSON 可读", bot.load_json_with_recovery(path) == {"a": 1})

        # 主文件写坏，应该从 .bak-* 备份恢复，而不是返回空字典
        shutil.copy2(path, path + ".bak-20260101-000000")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"a": ')
        recovered = bot.load_json_with_recovery(path)
        check("主文件损坏时从备份恢复", recovered == {"a": 1}, repr(recovered))

        broken = os.path.join(tmpdir, "broken.json")
        with open(broken, "w", encoding="utf-8") as f:
            f.write("not json")
        check("彻底读不出来时返回 None（禁止用空库覆盖）", bot.load_json_with_recovery(broken) is None)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def test_interject_scoring():
    """质量打分取代随机抽样：普通闲聊不接，提问/点名/玩梗才接。"""
    gate = 0.80 - 0.8 * 0.25  # 默认意愿 25 对应的门槛
    cases = [
        ("今天好热啊", False, "普通闲聊"),
        ("哈哈哈", False, "纯水"),
        ("上号", False, "开黑喊人（旧黑名单该拦的，现在靠分低自然略过）"),
        ("这个配置怎么改？", True, "技术提问"),
        ("小鲸鱼你在干嘛", True, "点名机器人"),
        ("笑死 这也太抽象了", True, "玩梗吐槽"),
    ]
    for text, expect, label in cases:
        score, why = bot.score_message_quality(text)
        got = score >= gate
        check(f"质量打分 {label}: {text}", got == expect, f"score={score:.2f} why={why}")


async def test_addressing():
    """串人：时间线得写清楚谁对谁说，追问窗口也不能见谁答谁。"""
    from handlers import group as gh
    line = gh._timeline_line({"sender": "小明(QQ 1)", "addr": "→@小红", "content": "这题怎么做"})
    check("时间线带 @ 对象", line == "小明(QQ 1) →@小红: 这题怎么做", line)
    check("无地址时保持原样", gh._timeline_line({"sender": "小明(QQ 1)", "content": "嗯"}) == "小明(QQ 1): 嗯")

    check("叫你的算接着聊", gh._looks_like_continuation("你在干嘛"))
    check("问句算接着聊", gh._looks_like_continuation("这个咋整"))
    check("短半句算接着聊", gh._looks_like_continuation("然后呢"))
    check("长篇对别人说的不算", not gh._looks_like_continuation("他那个显卡昨天又花屏了，气得直接退货，现在还在等快递上门取件"))


async def test_wording():
    """空格当逗号、AI 腔连接词、长句不分段——都是群里一眼看出来的毛病。"""
    np_ = messaging.normalize_punctuation
    check("语气词后空格变逗号", np_("知道啦 这不就来了") == "知道啦，这不就来了", np_("知道啦 这不就来了"))
    check("汉字间空格变逗号", np_("显卡没用 认命吧") == "显卡没用，认命吧", np_("显卡没用 认命吧"))
    check("中英之间的空格不动", np_("部署写在 README 里") == "部署写在 README 里", np_("部署写在 README 里"))
    check("不叠逗号", np_("行吧，，，就这样") == "行吧，就这样", np_("行吧，，，就这样"))

    check("AI 腔开篇被削", bot.scrub_ai_flavor("首先，这事不怪你") == "这事不怪你")
    check("客服收尾被削", bot.scrub_ai_flavor("这样改就行，希望对你有帮助") == "这样改就行", bot.scrub_ai_flavor("这样改就行，希望对你有帮助"))
    check("句中另外不误伤", "另外" in bot.scrub_ai_flavor("另外一个人也这么说"), bot.scrub_ai_flavor("另外一个人也这么说"))

    s = messaging.split_reply_segments("这条路我走过很多次了是真的挺远的你自己看着办吧别硬撑着啊，非要硬来我也拦不住你")
    check("长单句在逗号处断开", len(s) == 2 and "，" not in s[0], repr(s))


async def main():
    await test_dashboard()
    await test_json_recovery()
    await test_interject_scoring()
    await test_wording()
    await test_addressing()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for item in FAILED:
            print(" -", item)
        return 1
    print(f"全部通过（{PASSED} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
