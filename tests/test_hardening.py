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

        r = await client.post("/api/group/config", json={"group_id": 1, "key": "mode", "value": "evil"})
        check("非法 mode 被拒绝", r.status == 400, f"got {r.status}")

        r = await client.post("/api/group/config", json={"group_id": 1, "key": "interject_prob", "value": 3})
        check("越界插话概率被拒绝", r.status == 400, f"got {r.status}")

        r = await client.post("/api/group/config", json={"group_id": 1, "key": "enable_poke", "value": False})
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


async def main():
    await test_dashboard()
    await test_json_recovery()
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
