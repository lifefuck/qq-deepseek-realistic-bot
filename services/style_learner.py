# -*- coding: utf-8 -*-
"""群表达学习：从本群真实的聊天里挖「有人说 → 别人怎么接」，喂给提示词当样例。

为什么不用形容词教风格：写十句「要口语化、要简短」也没用，模型只会理解成
一种笼统的腔调；给两三组真实的「上一句→下一句」，它模仿的是具体的手感。
（MaiBot 的 expression_learner、Pallas-Bot 的群表达块，走的是同一条路。）

两种来源：
  1. 在线：每来一条群消息，跟它的上一条凑一对，合格就入池（observe）；
  2. 存量：data/history/group_*.jsonl 里攒的原始语料，一次性挖（seed_from_archive）。

池子落在 data/style_fewshot.json，由 services/llm.py 的 style_samples_hint 随机取用。
"""

import argparse
import json
import logging
import os
import re
import time

logger = logging.getLogger("qqbot.style_learner")

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
_POOL_PATH = os.path.join(_DATA_DIR, "style_fewshot.json")
_HISTORY_DIR = os.path.join(_DATA_DIR, "history")
_LIVE_DIR = os.path.join(_DATA_DIR, "live")

MAX_PER_GROUP = 300          # 每群上限，超了丢最老的
_FLUSH_INTERVAL = 300.0      # 攒够 5 分钟写一次盘，避免每条消息都写文件
_MIN_PREV, _MAX_PREV = 4, 60
_MIN_REPLY, _MAX_REPLY = 2, 24

# 云崽指令、纯符号、复读表情这些不算「接话」，学进去只会教它发指令
_BAD_REPLY = re.compile(r"^[#*/%~]|^\[CQ:|^(草|草草|233+|hhh+|\.\.\.|。。。)$|^\d+$")
_BAD_PREV = re.compile(r"^\[CQ:|^[#*/%~]")
# 群管机器人、欢迎语、引用块、内部占位：这些不是群友在聊天，学进来只会教它发通知
_NOISE = re.compile(r"\[引用|\[合并转发|\[CQ:|欢迎新人|管理员可以|撤回了一条|邀请.*加入了群聊|"
                    r"\{\{[A-Z_]+_[a-z0-9]+\}\}")

_pool = []            # [{gid, a, b, ts}]
_dirty = False
_last_flush = 0.0
_loaded = False


def _load() -> None:
    global _pool, _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(_POOL_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            _pool = [x for x in data if isinstance(x, dict) and x.get("a") and x.get("b")]
    except FileNotFoundError:
        _pool = []
    except Exception as e:
        logger.warning("读取接话样例池失败: %s", e)
        _pool = []


def _flush(force: bool = False) -> None:
    global _dirty, _last_flush
    if not _dirty:
        return
    now = time.time()
    if not force and now - _last_flush < _FLUSH_INTERVAL:
        return
    tmp = _POOL_PATH + ".tmp"
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_pool, f, ensure_ascii=False, indent=0)
        os.replace(tmp, _POOL_PATH)
        _dirty = False
        _last_flush = now
    except Exception as e:
        logger.warning("写接话样例池失败: %s", e)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


# 群里聊得快，前后两条常常毫无关系；只靠「相邻」会学进来一堆不搭界的对子。
# 加一道话题重合度门槛：两句至少共享两个实词字，或者上一句本身是个问句。
_STOP_CHARS = set("的了是我你他她它们这那就都也不在有和吗呢吧啊哦嗯个一上下来去说就")
_QUOTE_PREFIX = re.compile(r'^\[(引用|合并转发)[^\]]*\]\s*')
_PLACEHOLDER = re.compile(r"\{\{[A-Z_]+_[a-z0-9]+\}\}")


def _strip_noise(text: str) -> str:
    """引用前缀、合并转发标记、内部占位符：留着只会污染样例，剥掉再看是否可用。"""
    s = _QUOTE_PREFIX.sub("", text)
    s = _PLACEHOLDER.sub("某人", s)
    return s.strip()


def _related(prev: str, reply: str) -> bool:
    a = {c for c in prev if c not in _STOP_CHARS and "\u4e00" <= c <= "\u9fff"}
    b = {c for c in reply if c not in _STOP_CHARS and "\u4e00" <= c <= "\u9fff"}
    if len(a & b) >= 2:
        return True
    # 问句的答案本来就不重合（「吃啥」→「火锅」），单独放行
    if re.search(r"[?？]|吗$|呢$|啥|怎么|什么", prev):
        return True
    # 接梗、抖机灵这类「短反应」跟上一句没有任何字面重合，但恰恰是最该学的样本
    # （「这芯片直接认了 再加上这高刷」→「米式幽默」）。八个字以内的短句放行，
    # 再长的如果还扯不上同一件事，就真的是两张不搭界的话。
    return len(reply) <= 8


def _usable(prev: str, reply: str) -> bool:
    """这一对值不值得学：上一句够具体，下一句够短，都不是指令或表情码，而且是同一件事。"""
    # 引用块、机器人通知要在剥前缀之前拦：剥完就看不出来了
    if _NOISE.search(prev) or _NOISE.search(reply):
        return False
    prev, reply = _strip_noise(prev), _strip_noise(reply)
    if not (_MIN_PREV <= len(prev) <= _MAX_PREV):
        return False
    if not (_MIN_REPLY <= len(reply) <= _MAX_REPLY):
        return False
    if _BAD_PREV.search(prev) or _BAD_REPLY.search(reply):
        return False
    if _NOISE.search(prev) or _NOISE.search(reply):
        return False
    if prev == reply:
        return False
    return _related(prev, reply)


def _add(gid, prev: str, reply: str) -> bool:
    global _dirty
    _load()
    key = (str(gid), prev[:20], reply)
    for item in _pool:
        if (item.get("gid"), str(item.get("a"))[:20], item.get("b")) == key:
            return False
    _pool.append({"gid": str(gid), "a": prev, "b": reply, "ts": int(time.time())})
    # 群里聊天量大，同群超过上限就按时间丢最老的
    same = [i for i, x in enumerate(_pool) if x.get("gid") == str(gid)]
    if len(same) > MAX_PER_GROUP:
        drop = set(same[: len(same) - MAX_PER_GROUP])
        _pool[:] = [x for i, x in enumerate(_pool) if i not in drop]
    _dirty = True
    return True


def observe(group_id, pool) -> int:
    """在线学习：拿历史池里最后两条凑一对。返回是否新增（0/1）。"""
    try:
        if not group_id or not pool or len(pool) < 2:
            return 0
        prev_item, cur_item = pool[-2], pool[-1]
        # 自己发的不学：学自己的旧话只会让它越来越像自己
        if "你自己" in str(prev_item.get("sender") or "") or "你自己" in str(cur_item.get("sender") or ""):
            return 0
        if str(prev_item.get("sender_id")) == str(cur_item.get("sender_id")):
            return 0
        added = 1 if _add(group_id, _norm(prev_item.get("content")), _norm(cur_item.get("content"))) else 0
        if added:
            _flush()
        return added
    except Exception as e:
        logger.debug("在线学习接话样例失败: %s", e)
        return 0


def _iter_archive_rows(group_id):
    """攒下来的原始语料有两处：新版在 data/live/YYYY-MM.jsonl（t/gid/uid/raw），
    老版在 data/history/group_<gid>.jsonl（time/sender_id/raw）。两处都读。"""
    rows = []
    for name in sorted(os.listdir(_LIVE_DIR)) if os.path.isdir(_LIVE_DIR) else []:
        if not name.endswith(".jsonl"):
            continue
        try:
            with open(os.path.join(_LIVE_DIR, name), encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if str(row.get("gid")) == str(group_id):
                        rows.append({"uid": row.get("uid"), "text": row.get("raw"), "t": row.get("t")})
        except Exception:
            continue
    legacy = os.path.join(_HISTORY_DIR, f"group_{group_id}.jsonl")
    if os.path.exists(legacy):
        try:
            with open(legacy, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    rows.append({"uid": row.get("sender_id"), "text": row.get("content") or row.get("raw"),
                                 "t": row.get("time") or 0})
        except Exception:
            pass
    rows.sort(key=lambda r: r.get("t") or 0)
    return rows


def seed_from_archive(group_id, limit: int = 20000) -> int:
    """把攒下来的原始语料挖一遍，返回新增条数。"""
    rows = [r for r in _iter_archive_rows(group_id) if _norm(r.get("text"))][-limit:]
    added = 0
    prev = None
    for row in rows:
        cur = _norm(row.get("text"))
        if prev is not None:
            prev_text, prev_uid = prev
            if str(row.get("uid")) != str(prev_uid) and _usable(prev_text, cur):
                added += 1 if _add(group_id, prev_text, cur) else 0
        prev = (cur, row.get("uid"))
    _flush(force=True)
    logger.info("[群 %s] 从历史语料挖到 %d 组接话样例", group_id, added)
    return added


def main():
    ap = argparse.ArgumentParser(description="从历史语料挖群表达样例")
    ap.add_argument("groups", nargs="*", help="群号；不填就扫 data/history 下所有群")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    groups = args.groups
    if not groups:
        groups = sorted({str(json.loads(l).get("gid")) for name in os.listdir(_LIVE_DIR)
                         if name.endswith(".jsonl")
                         for l in open(os.path.join(_LIVE_DIR, name), encoding="utf-8", errors="ignore")
                         if l.strip() and "gid" in l})
    total = 0
    for gid in groups:
        total += seed_from_archive(gid)
    print(f"新增 {total} 组，池子共 {len(_pool)} 组")


if __name__ == "__main__":
    main()
