#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板鉴权/输入校验与落盘自愈的回归测试。

这些用例都是踩过的坑：CF 头后门、路径穿越、任意配置写入、记忆库读坏后被空库覆盖。
跑法：.venv/bin/python tests/test_hardening.py
"""
import asyncio
import datetime
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp.test_utils import TestServer, TestClient

import bot
import safety_guard
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


async def test_cq_face_raw_sanitizing():
    """新版带 raw= 的表情码不许整段漏进提示词，也不许被复读回群里。"""
    raw = ('[CQ:face,id=344,raw={"faceIndex":344,"faceText":"/大怨种","faceType":2,'
           '"packId":null,"stickerId":null,"sourceType":null}]')
    cleaned = bot.clean_cq_codes(raw)
    check("表情码整段被吃掉，只留表情名", cleaned == "[表情:大怨种]", cleaned)
    check("净化后没有残留 JSON", "faceIndex" not in cleaned and "{" not in cleaned, cleaned)

    old_style = bot.clean_cq_codes("[CQ:face,id=14]")
    check("老式无参数表情码仍能识别", old_style == "[表情]", old_style)

    unknown = bot.clean_cq_codes("[CQ:mface,emoji_id=abc,key=1] 好耶")
    check("未知类型的 CQ 码也不会漏进去", unknown == "好耶", unknown)

    # 纯表情消息不该被当成值得接话的内容
    score, why = bot.score_message_quality(cleaned, recent_msgs=6)
    check("纯表情消息不打高分", score < 0.40, f"score={score:.2f} why={why}")

    # 出站兜底：模型复读的 CQ 码要被剥掉，合法 @ 保留
    echoed = '看这个[CQ:face,id=344,raw={"faceText":"/大怨种"}]'
    check("出站剥表情码", bot.strip_outgoing_cq(echoed) == "看这个", bot.strip_outgoing_cq(echoed))
    check("出站保留合法 @", bot.strip_outgoing_cq("[CQ:at,qq=123456] 你好") == "[CQ:at,qq=123456] 你好")
    check("出站把带参 @ 归一化",
          bot.strip_outgoing_cq("[CQ:at,qq=123456,name=阿三] 你好") == "[CQ:at,qq=123456] 你好")


async def test_pass_signal_variants():
    """模型说「不接」的各种写法都得静默，别把 PASS 三个字母发进群。"""
    from safety_guard import is_pass_signal
    for text in ["[PASS]", "［PASS］", "PASS", "pass", " PASS。", "[pass]（这条不接）",
                 "［PASS］ 日常闲聊不插话"]:
        check(f"静默信号: {text!r}", is_pass_signal(text), text)
    for text in ["这个 pass 掉就行", "密码是 PASS123", "我 PASS 了这局", "PASS 这个词挺好用吧"]:
        check(f"正常发言不误判: {text!r}", not is_pass_signal(text), text)
    # 出站链路也得整条丢掉，不能发出去
    check("剥独白时吃掉裸 PASS", safety_guard.strip_internal_monologue("PASS") == "")
    check("剥独白时吃掉全角 PASS", safety_guard.strip_internal_monologue("［PASS］") == "")

    # 整条出站链路也得静默：洗文会把 [PASS] 搓成 PASS]，只在出站闸判就晚了
    sent = []

    async def _fake_send(ws, action, params, timeout=3.0):
        if action == "send_group_msg":
            sent.append(params.get("message"))
        return {"status": "ok", "retcode": 0, "data": {}}

    orig = bot.call_action
    bot.call_action = _fake_send
    try:
        ws = type("W", (), {"closed": False})()
        out = await messaging.send_segmented_group_message(ws, 900000001, "[PASS]（日常闲聊不插话）")
        check("带解释的 PASS 整条链路静默", out == "" and not sent, (out, sent))
    finally:
        bot.call_action = orig


async def test_cold_nudge_gate():
    """冷场冒泡不能对着空气演独白：没真人说话、没人接话、超额都不许开口。"""
    gid = 990000001
    now = time.time()
    dt = datetime.datetime.now()
    bot.group_timelines[gid] = [{"content": "a"}, {"content": "b"}]
    pool = bot.group_history_pool[gid]
    pool.clear()
    bot.group_last_nudge_ts.pop(gid, None)
    bot.group_nudge_day.pop(gid, None)
    bot.group_nudge_count.pop(gid, None)

    # 只有机器人自己的话 → 闭嘴
    pool.append({"sender_id": bot.BOT_QQ, "content": "yenai 更完居然没崩", "time": now - 600})
    ok, why, _ = bot.cold_nudge_gate(gid, now, dt)
    check("只有自己说过话时不冒泡", not ok, why)

    # 昨天的旧话题（过期素材）→ 闭嘴，别接「后天是谁生日」
    pool.append({"sender_id": 10001, "content": "后天是某某生日", "time": now - 30 * 3600})
    pool.append({"sender_id": 10002, "content": "抽到芙宁娜了", "time": now - 20 * 3600})
    ok, why, _ = bot.cold_nudge_gate(gid, now, dt)
    check("素材超过 12 小时不冒泡", not ok, why)

    # 两条近 12 小时的真人发言 → 允许，素材带进来
    pool.append({"sender_id": 10001, "content": "这版本更新了啥", "time": now - 4 * 3600})
    pool.append({"sender_id": 10002, "content": "引擎换了", "time": now - 3.5 * 3600})
    ok, why, mat = bot.cold_nudge_gate(gid, now, dt)
    check("有新鲜真人素材可以冒泡", ok, why)
    check("素材里混进过期内容会被切掉", "生日" not in mat, mat)

    # 上次冒泡之后没人接话 → 不再冒
    bot.group_last_nudge_ts[gid] = now - 60
    ok, why, _ = bot.cold_nudge_gate(gid, now, dt)
    check("上次冒泡没人接就不再来一次", not ok, why)

    # 有人接话后恢复，但一天最多两次
    pool.append({"sender_id": 10003, "content": "确实换了", "time": now - 30})
    bot.group_nudge_count[gid] = 2
    ok, why, _ = bot.cold_nudge_gate(gid, now, dt)
    check("每日配额用完后不再冒泡", not ok, why)


async def test_msg_dedupe_and_placeholders():
    """重复投递不能回答两遍；自己造的占位符不能当正文发出去。"""
    check("同一 msg_id 第二次判重", bot.is_duplicate_message(5550001) is False
          and bot.is_duplicate_message(5550001) is True)
    check("不同 msg_id 不算重", bot.is_duplicate_message(5550002) is False)
    check("没有 msg_id 时不判重", bot.is_duplicate_message(None) is False)
    check("非数字 msg_id 不崩", bot.is_duplicate_message("abc") is False)

    check("纯占位符被剥空", bot.strip_outgoing_cq("[表情:笑]") == "")
    check("图片占位符被剥空", bot.strip_outgoing_cq("[图片]") == "")
    check("占位符带正文时只留正文",
          bot.strip_outgoing_cq("[表情:大怨种] 今天热死了") == "今天热死了")
    check("合法 @ 仍保留",
          bot.strip_outgoing_cq("[CQ:at,qq=123456] [图片] 看这个") == "[CQ:at,qq=123456] 看这个")


async def test_concurrent_at_not_dropped():
    """两个群友同时 @，两条都要回：同群生成锁是排队，不是丢弃。"""
    gid, sends = 900000001, []
    bot.ALLOWED_GROUPS = set(bot.ALLOWED_GROUPS) | {gid}
    bot.load_group_configs()
    cfg = bot.get_group_config(gid)
    cfg["muted"] = False
    cfg["interject_prob"] = 0.0

    async def _ca(ws, action, params, timeout=2.0):
        if action == "send_group_msg":
            sends.append(params.get("message"))
        return {"status": "ok", "retcode": 0, "data": {}}

    seen = []

    async def _model(*a, **k):
        seen.append(1)
        await asyncio.sleep(0.2)
        return ["在的在的", "问啥呢", "看着呢"][min(len(seen) - 1, 2)]

    orig_ca, orig_model = bot.call_action, bot.call_deepseek
    bot.call_action, bot.call_deepseek = _ca, _model
    bot.archive_message = lambda *a, **k: None
    bot.check_bot_banned_realtime = lambda *a, **k: asyncio.sleep(0, result=False)
    bot.update_user_memory_async = lambda *a, **k: asyncio.sleep(0)
    bot.sleep_override_until = time.time() + 3600
    try:
        def _ev(text, mid, uid):
            return {"post_type": "message", "message_type": "group", "group_id": gid,
                    "user_id": uid, "raw_message": f"[CQ:at,qq={bot.BOT_QQ}] {text}",
                    "message": [{"type": "text", "data": {"text": text}}],
                    "sender": {"user_id": uid, "nickname": f"人{uid % 100}", "card": f"人{uid % 100}"},
                    "message_id": mid, "time": int(time.time())}

        ws = type("W", (), {"closed": False})()
        from handlers import group as _hg
        await asyncio.gather(
            _hg.handle_group_message(ws, _ev("在不在", 910001, 800000021)),
            _hg.handle_group_message(ws, _ev("问个事", 910002, 800000022)),
        )
        await asyncio.sleep(2.5)
        check("两人同时 @ 都有回答", len(sends) == 2, sends)
        check("两条回答 @ 了不同的人",
              any("800000021" in str(m) for m in sends) and any("800000022" in str(m) for m in sends),
              sends)
    finally:
        bot.call_action, bot.call_deepseek = orig_ca, orig_model


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
    await test_cq_face_raw_sanitizing()
    await test_pass_signal_variants()
    await test_cold_nudge_gate()
    await test_msg_dedupe_and_placeholders()
    await test_concurrent_at_not_dropped()
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
