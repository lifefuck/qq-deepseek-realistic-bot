#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""面板鉴权/输入校验与落盘自愈的回归测试。

这些用例都是踩过的坑：CF 头后门、路径穿越、任意配置写入、记忆库读坏后被空库覆盖。
跑法：.venv/bin/python tests/test_hardening.py
"""
import asyncio
import re
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
import meme_catalog
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
    orig_archive, orig_ban, orig_mem = (bot.archive_message, bot.check_bot_banned_realtime,
                                        bot.update_user_memory_async)
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
        bot.archive_message, bot.check_bot_banned_realtime = orig_archive, orig_ban
        bot.update_user_memory_async = orig_mem


async def test_audit_regressions():
    """审计里查实的几处：全角标签、@ 前缀误判、全员禁言不过期、坏状态文件、PASS 记账。"""
    import json as _json
    check("全角 PASS 被识别", safety_guard.is_pass_signal("［ＰＡＳＳ］") is True)
    check("全角小写 PASS 被识别", safety_guard.is_pass_signal("［pass］") is True)
    check("全角 MEME 标签被剥",
          meme_catalog.extract_tag("［MEME：嘲讽］就这水平") == ("就这水平", "嘲讽"))
    check("全角 MEME 无正文时不发",
          meme_catalog.extract_tag("［MEME：嘲讽］")[0] == "")

    # @ 判定不能靠 QQ 号前缀包含：@10000000009 不等于 @1000000000
    pat = rf"\[CQ:at,[^\]]*qq={bot.BOT_QQ}(?!\d)[^\]]*\]"
    check("@ 自己的号命中", bool(re.search(pat, f"[CQ:at,qq={bot.BOT_QQ}] 在吗")))
    check("前缀相似的长号不误判", not re.search(pat, f"[CQ:at,qq={bot.BOT_QQ}9] 在吗"))

    # 全员禁言带过期：离线期间被解禁也不会永久哑巴
    gid = 424242
    bot.group_all_banned[gid] = time.time() - 1
    check("全员禁言过期后自动失效",
          await bot.check_bot_banned_realtime(None, gid) is False)
    bot.group_all_banned[gid] = time.time() + 600
    check("未过期的全员禁言仍然拦截",
          await bot.check_bot_banned_realtime(None, gid) is True)
    bot.group_all_banned.pop(gid, None)

    # 运行时状态文件被手改坏，不能把启动带崩
    import tempfile
    orig_file = bot.RUNTIME_STATE_FILE
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     dir=os.path.dirname(orig_file) or ".") as f:
        _json.dump({"silence_until": {"群号写错了": 9e12},
                    "follow_up": ["本该是字典"],
                    "ban_expire": {"111": "不是时间戳"},
                    "all_banned": {"222": "x"}}, f)
        broken = f.name
    bot.RUNTIME_STATE_FILE = broken
    try:
        bot.load_runtime_state()
        check("坏状态文件不抛异常", True)
    except Exception as e:
        check("坏状态文件不抛异常", False, f"{type(e).__name__}: {e}")
    finally:
        bot.RUNTIME_STATE_FILE = orig_file
        os.unlink(broken)

    # 被出站闸吞掉的回复不能算"说过了"
    gid2 = 900000001
    bot.ALLOWED_GROUPS = set(bot.ALLOWED_GROUPS) | {gid2}
    bot.load_group_configs()
    cfg = bot.get_group_config(gid2)
    cfg["muted"] = False
    cfg["interject_prob"] = 0.0
    bot.reply_times_clear(gid2) if hasattr(bot, "reply_times_clear") else None
    bot.group_bot_reply_times[gid2].clear()
    sent = []

    async def _ca(ws, action, params, timeout=2.0):
        if action == "send_group_msg":
            sent.append(params.get("message"))
        return {"status": "ok", "retcode": 0, "data": {}}

    async def _pass_model(*a, **k):
        return "[PASS] 这个我不太确定"

    o_ca, o_m = bot.call_action, bot.call_deepseek
    # 这个桩以前只打不还：后面所有用例 seeing 的 bot.check_bot_banned_realtime
    # 都变成「永远没被禁言」，禁言相关断言全成了空跑（本条就是这么暴露出来的）
    o_ban = bot.check_bot_banned_realtime
    _orig_archive = bot.archive_message
    bot.call_action, bot.call_deepseek = _ca, _pass_model
    bot.archive_message = lambda *a, **k: None
    bot.check_bot_banned_realtime = lambda *a, **k: asyncio.sleep(0, result=False)
    bot.update_user_memory_async = lambda *a, **k: asyncio.sleep(0)
    bot.sleep_override_until = time.time() + 3600
    try:
        ws = type("W", (), {"closed": False})()
        from handlers import group as _hg
        await _hg.handle_group_message(ws, {
            "post_type": "message", "message_type": "group", "group_id": gid2,
            "user_id": 800000031, "raw_message": f"[CQ:at,qq={bot.BOT_QQ}] 这咋弄",
            "message": [{"type": "text", "data": {"text": "这咋弄"}}],
            "sender": {"user_id": 800000031, "nickname": "阿测", "card": "阿测"},
            "message_id": 920001, "time": int(time.time())})
        await asyncio.sleep(1.5)
        check("PASS 时一条都没发", not sent, sent)
        check("PASS 不占主动开口配额", len(bot.group_bot_reply_times[gid2]) == 0,
              list(bot.group_bot_reply_times[gid2]))
    finally:
        bot.call_action, bot.call_deepseek = o_ca, o_m
        bot.check_bot_banned_realtime = o_ban
        bot.archive_message = _orig_archive


async def test_batch2_regressions():
    """审计第二批：SSRF 对端复核、记忆数字区分、WAL。"""
    import media_resolver
    from services.fish_memory import FishMemoryEngine

    class _T:
        def __init__(self, ip):
            self.ip = ip

        def get_extra_info(self, key):
            return (self.ip, 443) if key == "peername" else None

    class _C:
        def __init__(self, ip):
            self.transport = _T(ip)

    class _R:
        def __init__(self, ip):
            self.connection = _C(ip)

    check("实际连到内网地址被拒", media_resolver.connected_peer_is_public(_R("127.0.0.1")) is False)
    check("公网地址放行", media_resolver.connected_peer_is_public(_R("8.8.8.8")) is True)
    check("拿不到对端时不误伤",
          media_resolver.connected_peer_is_public(type("R", (), {"connection": None})()) is True)

    import tempfile, sqlite3
    with tempfile.TemporaryDirectory() as td:
        eng = FishMemoryEngine(db_path=os.path.join(td, "t.db"))
        a = eng.add_memory("g", "p", "我明天要去北京出差3天")
        b = eng.add_memory("g", "p", "我明天要去北京出差9天")
        check("数字不同的事实不合并", a.get("id") != b.get("id"), (a, b))
        c = eng.add_memory("g", "p", "我明天要去北京出差3天！")
        check("同一事实仍会合并", c.get("merged") is True, c)
        mode = sqlite3.connect(os.path.join(td, "t.db")).execute("pragma journal_mode").fetchone()[0]
        check("记忆库启用 WAL", str(mode).lower() == "wal", mode)

    check("分词保留数字", "3" in FishMemoryEngine.tokens("出差3天"))


async def test_batch3_regressions():
    """审计第三批：链接不被切断、面板只能改白名单群、冷场冒泡回滚。"""
    import services.messaging as messaging

    url = "https://www.bilibili.com/video/BV1xx411c7mD?p=2&t=30"
    text = "这视频真的离谱笑死我了必须发给你看看啊兄弟！" + url + "！看完记得回我一句啊"
    parts = messaging.split_reply_segments(text)
    check("链接没被 ? 切断", any(url in p for p in parts), parts)
    check("分段占位符不泄漏", not any("\ue000" in p or "\ue001" in p for p in parts), parts)
    check("真正的感叹号仍然分段", len(parts) >= 2, parts)

    # 面板/接口不能凭空给陌生群写配置（否则能把它塞进云崽白名单）
    check("未知群号被拒", bot.update_group_config(999999999, "muted", True) is False)
    gid_ok = sorted(bot.ALLOWED_GROUPS)[0]
    check("白名单群可写", bot.update_group_config(gid_ok, "muted", False) is True)
    check("私聊(None)仍可写", bot.update_group_config(None, "muted", False) is True)

    # 冷场冒泡失败回滚
    gid = 424243
    bot.group_nudge_count[gid] = 2
    bot.group_last_nudge_ts[gid] = 111.0
    bot.group_last_active_reply[gid] = 222.0
    bot.group_last_msg_time[gid] = 333.0
    bot.cold_nudge_rollback(gid, {"nudge_ts": 111.0, "active": 222.0, "msg": 333.0})
    check("失败后名额退回", bot.group_nudge_count[gid] == 1, bot.group_nudge_count[gid])
    check("失败后活跃时间回滚", bot.group_last_active_reply[gid] == 222.0)
    for d in (bot.group_nudge_count, bot.group_last_nudge_ts,
              bot.group_last_active_reply, bot.group_last_msg_time):
        d.pop(gid, None)


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


async def test_batch4_regressions():
    """审计第四批：配置写成非对象不崩、冷场判据用单调计数、误判指令、越权动作。"""
    import json as _json
    import tempfile
    import games
    from handlers import actions
    import safety_guard as sg

    # 免红温名单被手改成数组 / 写成字符串，都不该抛异常
    import os as _os
    _tmp = tempfile.mkdtemp()
    _anger = _os.path.join(_tmp, "anger_overrides.json")
    _orig_file = bot.ANGER_OVERRIDE_FILE
    _orig_cache = bot._anger_cache
    try:
        bot.ANGER_OVERRIDE_FILE = _anger
        for payload, want, label in (
            ('["oops"]', False, "数组结构"),
            ('{"123456789": true}', False, "布尔条目"),
            ('{"123456789": {"unlimited": "no"}}', False, "字符串没算开启"),
            ('{"123456789": {"unlimited": true}}', True, "真开启生效"),
        ):
            with open(_anger, "w", encoding="utf-8") as f:
                f.write(payload)
            bot._anger_cache = {"mtime": 0.0, "data": {}}
            check(f"免红温配置{label}", bot.is_anger_unlimited(123456789) is want)
    finally:
        bot.ANGER_OVERRIDE_FILE = _orig_file
        bot._anger_cache = _orig_cache

    # 群配置条目被写成字符串：不能崩在 import
    gid = sorted(bot.ALLOWED_GROUPS)[0]
    saved = dict(bot.group_configs.get(str(gid), {}))
    try:
        bot.group_configs[str(gid)] = "on"
        bot.load_group_configs()
        check("坏配置条目降级为默认值", isinstance(bot.get_group_config(gid), dict))
    finally:
        bot.group_configs[str(gid)] = saved
        bot.load_group_configs()

    # 冷场判据：deque 会被 popleft，长度不可靠，必须用单调计数
    bot.group_bot_reply_seq[gid] = 7
    bot.note_reply_sent(gid, "真的发出去了")
    check("冷场判据用单调计数", bot.group_bot_reply_seq[gid] == 8)

    # 云崽指令：情绪化中文句不该被当成指令吞掉
    for text in ("？", "！这也太离谱了", "～好困", "我想问下培养", "深渊今天开了", "帮我看看这个面板"):
        check(f"不当指令: {text}", bot.is_game_or_yunzai_command(text) is False)
    for text in ("#帮助", "?help", "雷神面板", "深渊"):
        check(f"仍是指令: {text}", bot.is_game_or_yunzai_command(text) is True)

    # 编号规则行：口语里的「要求/规则」不该整行删空
    check("要求句不被删空", sg.strip_internal_monologue("1. 这个要求我做不到") != "")
    check("真规则复述仍剥掉", sg.strip_internal_monologue("1. 保持人设，别端着") == "")

    # 数字炸弹：超长数字不崩、过期能重开
    games.handle_quick_game_commands("roll " + "9" * 5000)
    check("超长数字不崩", True)
    import time as _t
    games.active_games[987654321] = {"game": "bomb", "target": 33, "low": 1, "high": 100,
                                     "start_time": 0, "last_active": _t.time() - 10000, "guesses": 1}
    handled, _msg = games.process_number_bomb(987654321, "数字炸弹", 111, "阿测")
    check("过期炸弹能重开", handled and games.active_games[987654321]["last_active"] > _t.time() - 60)
    games.active_games.pop(987654321, None)

    # 记忆原子：level 写成 null 不许崩
    import memory_atoms
    atom = memory_atoms.normalize({"text": "喜欢猫", "level": None}, _t.time())
    check("level=null 兜住", atom["level"] == 3)

    # 动作层：讨论句不执行、步数不当 QQ 号
    GID = gid
    bot.group_member_nicks[GID] = {222222222: "小明"}
    calls = []

    async def _fake(ws, action, params, timeout=2.0):
        calls.append((action, params))
        return {"status": "ok", "data": {"role": "member"}}
    _orig_action = bot.call_action
    bot.call_action = _fake
    try:
        r = await actions.maybe_run(None, GID, 333333333, "测", True, "小明该不该禁言", True)
        check("讨论句不执行禁言", not r and not [c for c in calls if c[0].startswith("set_group")])
        calls.clear()
        r = await actions.maybe_run(None, GID, 333333333, "测", True, "我踢了80000步厉害吧", True)
        check("步数不被当 QQ 号", not [c for c in calls if c[0].startswith("set_group")])
        calls.clear()
        await actions._send(None, GID, 222222222, "PASS")
        check("动作层也过出站闸", not calls)
        calls.clear()
        await actions._send(None, GID, 222222222, "你行不行啊")
        check("动作层正常文本能发出", any(c[0] == "send_group_msg" for c in calls))
    finally:
        bot.call_action = _orig_action


async def test_batch5_regressions():
    """审计第五批：数据落盘与持久化（话题库被覆盖、跨天统计丢失、禁言误判、被踢还在服务）。"""
    import json as _json
    import tempfile
    import memory_atoms
    import prompts
    import sync_hot_words
    import cross_memory
    import web_dashboard

    # cross_memory：顶层不是 list 时必须拒绝加载，不能按 0 条算完再覆盖
    _tmp = tempfile.mkdtemp()
    _path = __import__("os").path.join(_tmp, "topics.json")
    with open(_path, "w", encoding="utf-8") as f:
        _json.dump({"topics": [{"content": "旧话题", "score": 3}]}, f, ensure_ascii=False)
    try:
        mgr = cross_memory.CrossGroupTopicManager(file_path=_path)
        check("非 list 话题文件按空库启动", len(mgr.topics) == 0)
    except ValueError:
        check("非 list 话题文件按空库启动", True)
    with open(_path, encoding="utf-8") as f:
        check("原话题文件未被覆盖", "旧话题" in f.read())

    # get_status_summary 跨天要归档昨天的数据
    tracker = bot.token_tracker
    _saved = _json.loads(_json.dumps(tracker.data))
    try:
        tracker.data["today"] = {"date": "2000-01-01", "prompt_tokens": 111, "completion_tokens": 222,
                                 "total_tokens": 333, "requests": 7, "cost_cny": 0.5}
        tracker.data["history"] = {}
        tracker.get_status_summary()
        check("跨天归档了昨天的统计", tracker.data["history"].get("2000-01-01", {}).get("requests") == 7)
        check("今日计数已清零", tracker.data["today"]["requests"] == 0)
    finally:
        tracker.data.clear()
        tracker.data.update(_saved)

    # 好感衰减：一条脏档案不能拖垮整轮
    _mem_saved = _json.loads(_json.dumps(bot.user_memories))
    try:
        bot.user_memories["_test_scope_"] = {
            "222222222": {"favor": "很高", "updated_at": 0},
            "333333333": {"favor": 90, "updated_at": 0},
        }
        n = bot.weekly_favor_decay()
        check("脏档案不中断衰减", n >= 1)
        check("正常档案已衰减", bot.user_memories["_test_scope_"]["333333333"]["favor"] == 89)
    finally:
        bot.user_memories.clear()
        bot.user_memories.update(_mem_saved)

    # 禁言复核：响应里没有 shut_up_timestamp 时不能当成已解禁
    GID = sorted(bot.ALLOWED_GROUPS)[0]
    _exp_saved = dict(bot.group_bot_ban_expire)
    _ab_saved = dict(bot.group_all_banned)
    bot.group_all_banned.pop(GID, None)
    _orig_action = bot.call_action
    try:
        bot.group_bot_ban_expire[GID] = time.time() + 30   # 离解禁很近，会触发主动核实
        bot.last_active_ban_check.pop(GID, None)

        async def _no_field(ws, action, params, timeout=1.5):
            return {"status": "ok", "data": {"user_id": bot.BOT_QQ}}
        bot.call_action = _no_field
        banned = await bot.check_bot_banned_realtime(object(), GID)
        check("缺字段仍按禁言处理", banned is True)
        check("禁言到期时间被顺延", bot.group_bot_ban_expire[GID] > time.time() + 60)
    finally:
        bot.call_action = _orig_action
        bot.group_bot_ban_expire.clear()
        bot.group_bot_ban_expire.update(_exp_saved)
        bot.group_all_banned.clear()
        bot.group_all_banned.update(_ab_saved)

    # 被踢出群：运行期白名单摘掉；被拉回：恢复
    _ag_saved = set(bot.ALLOWED_GROUPS)

    async def _drive(notice_type, uid):
        ev = {"notice_type": notice_type, "group_id": GID, "user_id": uid,
              "message_type": "group", "raw_message": "", "message": [], "sender": {}}
        await bot.handle_notice_event(None, ev)
    try:
        await _drive("group_decrease", bot.BOT_QQ)
        check("被踢后不再服务该群", GID not in bot.ALLOWED_GROUPS)
        await _drive("group_increase", bot.BOT_QQ)
        check("被拉回后恢复服务", GID in bot.ALLOWED_GROUPS)
    finally:
        bot.ALLOWED_GROUPS.clear()
        bot.ALLOWED_GROUPS.update(_ag_saved)

    # 云崽白名单：带引号的条目不能被静默丢弃
    _tmp2 = tempfile.mkdtemp()
    _yml = __import__("os").path.join(_tmp2, "other.yaml")
    _old_yml = bot.YUNZAI_OTHER_YAML
    try:
        bot.YUNZAI_OTHER_YAML = _yml
        with open(_yml, "w", encoding="utf-8") as f:
            f.write("whiteGroup:\n  - 111111111\n  - \"222222222\"  # 二群\n")
        bot.sync_yunzai_whitelist(333333333, True)
        with open(_yml, encoding="utf-8") as f:
            body = f.read()
        check("数字条目保留", "111111111" in body)
        check("带引号的条目保留", "222222222" in body)
        check("新群已写入", "333333333" in body)
    finally:
        bot.YUNZAI_OTHER_YAML = _old_yml

    # 卡片解析：payload 里带数组也不能截断在第一个 ]
    card = '[CQ:json,data={"app":"com.tencent.structmsg","meta":{"news":{"tagList":[{"t":"x"}]}}}]'
    parsed = bot.parse_card_content(card)
    check("卡片不在数组处截断", "tagList" not in parsed and "[分享" in parsed or "分享" in parsed)

    # 对话记录：续行的 URL 不能被当昵称前缀砍掉
    dialogue = "阿测(123): 看这个\nhttps://example.com/a?b=1"
    out = bot.extract_dialogue_content(dialogue)
    check("续行 URL 协议头未被砍", "https://example.com" in out)

    # 记忆原子：0 级条目不会被分数淘汰
    atoms = [memory_atoms._new_atom(f"普通{i}", 0) for i in range(memory_atoms.MAX_NOTES)]
    _lvl0 = memory_atoms._new_atom("不许提那件事", 0)
    _lvl0["level"] = 0
    atoms.append(_lvl0)
    trimmed = memory_atoms._trim(atoms, time.time() + 10 ** 6)
    check("0 级条目保留", any(a.get("text") == "不许提那件事" for a in trimmed))
    check("长度不超上限", len(trimmed) <= memory_atoms.MAX_NOTES)

    # 云崽知识库：随口一句「怎么」不该灌两千多字
    check("闲聊不触发知识库", prompts.needs_games_kb("今天怎么这么热") is False)
    check("问功能才触发", prompts.needs_games_kb("原神面板怎么用") is True)

    # 热词同步：库存少也要保护，缩水判定用比例而不是写死的 200
    check("库存 50 掉到 5 条要中止", sync_hot_words._should_abort(50, 5) is True)
    check("库存 50 掉到 40 条不中止", sync_hot_words._should_abort(50, 40) is False)
    check("小库存正常同步不中止", sync_hot_words._should_abort(10, 8) is False)

    # 面板 Basic 认证结果有缓存（同一凭据不重复算 PBKDF2）
    web_dashboard._BASIC_VERIFY_CACHE.clear()
    salt, h = web_dashboard.hash_password("pw123456")
    check("首次验证通过", web_dashboard._basic_verify_cached("admin", "pw123456", salt, h) is True)
    check("结果进了缓存", len(web_dashboard._BASIC_VERIFY_CACHE) == 1)
    check("错误密码不通过", web_dashboard._basic_verify_cached("admin", "wrong", salt, h) is False)


async def main():
    await test_dashboard()
    await test_json_recovery()
    await test_interject_scoring()
    await test_cq_face_raw_sanitizing()
    await test_pass_signal_variants()
    await test_cold_nudge_gate()
    await test_msg_dedupe_and_placeholders()
    await test_concurrent_at_not_dropped()
    await test_audit_regressions()
    await test_batch2_regressions()
    await test_batch3_regressions()
    await test_batch4_regressions()
    await test_batch5_regressions()
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
