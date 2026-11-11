"""从 NapCat 导出群历史消息，落成 JSONL。

要点（实测过的坑）：
- `message_seq` 必须给 0 才是「从最新往回取」，给具体值只会返回该条之后的消息；
- `count` 可以一次给很大，NapCat 会回缓存里的全部（2000 条约 4 天），比逐页翻更省事；
- 给 20000 会直接把 NapCat 拖到超时，别贪。
"""
import asyncio
import json
import os
import sys
import time
import uuid

import aiohttp

WS_URL = os.environ.get("NAPCAT_WS_URL", "")
OUT_DIR = os.environ.get(
    "HISTORY_OUT",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "history"),
)


async def call(ws, action, params, timeout=90.0):
    eid = str(uuid.uuid4())
    await ws.send_str(json.dumps({"action": action, "params": params, "echo": eid}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = await asyncio.wait_for(ws.receive(), timeout=max(1.0, deadline - time.time()))
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue
        data = json.loads(msg.data)
        if data.get("echo") == eid:
            return data
    return None


async def fetch_group(ws, gid, limit):
    # 从大到小试，NapCat 处理不过来会直接不响应，所以每档单独计时
    for cnt in (limit, 2000, 800, 200):
        if cnt > limit:
            continue
        res = await call(ws, "get_group_msg_history",
                         {"group_id": gid, "message_seq": 0, "count": cnt}, timeout=60.0)
        msgs = ((res or {}).get("data") or {}).get("messages") or []
        if msgs:
            return msgs
        print(f"  [{gid}] count={cnt} 无响应/空", file=sys.stderr)
    return []


async def main():
    if not WS_URL:
        sys.exit("请先设置 NAPCAT_WS_URL，例如 ws://<主机>:3001")
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    os.makedirs(OUT_DIR, exist_ok=True)
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(WS_URL, timeout=30) as ws:
            gres = await call(ws, "get_group_list", {})
            groups = (gres or {}).get("data") or []
            total = 0
            for g in groups:
                gid, name = g.get("group_id"), g.get("group_name")
                rows = await fetch_group(ws, gid, limit)
                # 去重 + 按时间正序
                uniq = {m.get("message_id"): m for m in rows}
                rows = sorted(uniq.values(), key=lambda m: m.get("time") or 0)
                path = os.path.join(OUT_DIR, f"group_{gid}.jsonl")
                with open(path, "w", encoding="utf-8") as f:
                    for m in rows:
                        f.write(json.dumps({
                            "msg_id": m.get("message_id"),
                            "seq": m.get("real_seq"),
                            "time": m.get("time"),
                            "sender_id": (m.get("sender") or {}).get("user_id") or m.get("user_id"),
                            "sender": (m.get("sender") or {}).get("card") or (m.get("sender") or {}).get("nickname"),
                            "raw": m.get("raw_message"),
                        }, ensure_ascii=False) + "\n")
                total += len(rows)
                if rows:
                    first, last = rows[0]["time"], rows[-1]["time"]
                    print(f"{name}({gid}): {len(rows)} 条 / {path}")
                    print(f"    {time.strftime('%m-%d %H:%M', time.localtime(first))} → "
                          f"{time.strftime('%m-%d %H:%M', time.localtime(last))}"
                          f"（{(last - first) / 86400:.1f} 天）")
            print(f"\n合计 {total} 条")


if __name__ == "__main__":
    asyncio.run(main())
