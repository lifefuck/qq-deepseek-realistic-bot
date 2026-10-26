# -*- coding: utf-8 -*-
"""群长期统计：发言量、活跃时段、互动对、本周榜。

年鉴和关系网要的是"攒了半年的料"，光靠内存里那 200 条时间线做不出来，
所以这里单独落一份累计计数。只累加，不存原文，群里聊什么这里看不见。
"""
import collections
import json
import logging
import os
import threading
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("qqbot.stats")

_DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "group_stats.json")
_FLUSH_INTERVAL = 300.0

_lock = threading.Lock()
_dirty = 0
_last_flush = 0.0

# {group_id(str): {"since": ts, "users": {uid: {...}}, "pairs": {"a|b": n}, "week": {...}}}
_stats: Dict[str, dict] = {}

WEEK_SECONDS = 7 * 86400


def _blank_group() -> dict:
    return {"since": time.time(), "users": {}, "pairs": {}, "week": {}}


def _blank_user(nick: str) -> dict:
    return {"nick": nick, "msgs": 0, "chars": 0, "night": 0,
            "hours": {}, "last_seen": 0.0, "first_seen": time.time()}


def load() -> None:
    global _stats
    try:
        with open(_DATA_FILE, "r", encoding="utf-8") as f:
            _stats = json.load(f) or {}
        logger.info("群统计已加载：%d 个群", len(_stats))
    except FileNotFoundError:
        _stats = {}
    except Exception as e:
        logger.warning("群统计加载失败，从空开始: %s", e)
        _stats = {}


def save(force: bool = False) -> None:
    global _dirty, _last_flush
    if not _dirty and not force:
        return
    with _lock:
        snapshot = json.dumps(_stats, ensure_ascii=False, separators=(",", ":"))
        _dirty = 0
    try:
        os.makedirs(os.path.dirname(_DATA_FILE), exist_ok=True)
        tmp = _DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(snapshot)
        os.replace(tmp, _DATA_FILE)
        _last_flush = time.time()
    except Exception as e:
        logger.warning("群统计落盘失败: %s", e)


def maybe_flush() -> None:
    """写得太频繁没必要，攒够 5 分钟再落一次；崩溃最多丢 5 分钟计数。"""
    if _dirty and time.time() - _last_flush > _FLUSH_INTERVAL:
        save()


def group(group_id: int) -> dict:
    key = str(group_id)
    g = _stats.get(key)
    if g is None:
        g = _blank_group()
        _stats[key] = g
    g.setdefault("users", {})
    g.setdefault("pairs", {})
    g.setdefault("week", {})
    return g


def record_message(group_id: int, user_id: int, nick: str, text: str, hour: Optional[int] = None) -> None:
    global _dirty
    if not group_id or not user_id:
        return
    g = group(group_id)
    uid = str(user_id)
    u = g["users"].get(uid) or _blank_user(nick)
    if nick:
        u["nick"] = nick
    plain = text or ""
    u["msgs"] += 1
    u["chars"] += len(plain)
    hh = str(hour if hour is not None else time.localtime().tm_hour)
    u["hours"][hh] = u["hours"].get(hh, 0) + 1
    if (hour if hour is not None else time.localtime().tm_hour) < 6:
        u["night"] += 1
    u["last_seen"] = time.time()
    g["users"][uid] = u

    w = g["week"].get(uid) or {"nick": nick, "msgs": 0, "chars": 0, "night": 0}
    w["nick"] = nick or w.get("nick", "")
    w["msgs"] += 1
    w["chars"] += len(plain)
    if (hour if hour is not None else time.localtime().tm_hour) < 6:
        w["night"] += 1
    g["week"][uid] = w
    _dirty += 1


def record_pair(group_id: int, a: int, b: int) -> None:
    """记一对互动（@ 或引用），关系网按这个画线。"""
    global _dirty
    if not group_id or not a or not b or a == b:
        return
    key = "|".join(str(x) for x in sorted((int(a), int(b))))
    g = group(group_id)
    g["pairs"][key] = g["pairs"].get(key, 0) + 1
    _dirty += 1


def top_users(group_id: int, n: int = 5, by: str = "msgs", weekly: bool = False) -> List[Tuple[int, str, int]]:
    g = group(group_id)
    src = g["week"] if weekly else g["users"]
    rows = []
    for uid, u in src.items():
        val = int(u.get(by, 0))
        if val:
            rows.append((int(uid), u.get("nick") or uid, val))
    rows.sort(key=lambda x: x[2], reverse=True)
    return rows[:n]


def top_pairs(group_id: int, n: int = 6) -> List[Tuple[int, int, int]]:
    g = group(group_id)
    rows = []
    for key, cnt in g["pairs"].items():
        try:
            a, b = (int(x) for x in key.split("|"))
        except ValueError:
            continue
        rows.append((a, b, int(cnt)))
    rows.sort(key=lambda x: x[2], reverse=True)
    return rows[:n]


def nick_of(group_id: int, user_id: int) -> str:
    g = group(group_id)
    u = g["users"].get(str(user_id)) or {}
    return u.get("nick") or str(user_id)


def group_totals(group_id: int) -> dict:
    g = group(group_id)
    users = g["users"]
    total_msgs = sum(u.get("msgs", 0) for u in users.values())
    total_chars = sum(u.get("chars", 0) for u in users.values())
    night = sum(u.get("night", 0) for u in users.values())
    since = g.get("since") or time.time()
    return {
        "members": len(users),
        "msgs": total_msgs,
        "chars": total_chars,
        "night": night,
        "days": max(1, int((time.time() - since) / 86400)),
    }


def rollover_week(group_id: int) -> List[Tuple[int, str, int]]:
    """把本周榜清出来交给调用方发榜，然后清零重新起算。"""
    global _dirty
    g = group(group_id)
    rows = top_users(group_id, n=10, weekly=True)
    g["week"] = {}
    _dirty += 1
    return rows


def idle_since(group_id: int, seconds: float) -> List[int]:
    """多久没说过话的人，用来做每周好感度自然回落。"""
    g = group(group_id)
    now = time.time()
    out = []
    for uid, u in g["users"].items():
        if now - float(u.get("last_seen") or 0) > seconds:
            try:
                out.append(int(uid))
            except ValueError:
                pass
    return out
