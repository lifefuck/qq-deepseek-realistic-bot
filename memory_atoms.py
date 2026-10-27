#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""群友印象原子层。

以前的 notes 是一串字符串，超 6 条就把所有条目拼成一句再截断到 100 字，
结果语义重复越堆越多、信息还被腰斩。这里换成「每条印象一个原子」：

- 去重看字符集重合度，不看字符串是否一模一样；
- 每条带等级，等级决定衰减速度和淘汰门槛；
- 注入时按 等级 x 剩余强度 x 新鲜度 排序，装进字数预算就停，不硬截断。

等级由规则判定（不再多问模型一句话，省 token）：
0 不可忘 / 1 随口 / 2 细节 / 3 平常 / 4 在意 / 5 关键
"""

import hashlib
import re
import time

DUP_RATIO = 0.75          # 到这个重合度就是同一条，只强化不新增
CONFLICT_RATIO = 0.55     # 到这个程度且新旧一边带否定词，算改了主意
REFINE_RATIO = 0.5        # 短的那句有一半长度被长句包着，就当是同一件事的补充说明
EVICT_RATIO = 0.25        # 剩余强度低于它就淘汰（0 级不受限）
MAX_NOTES = 10            # 硬上限，超了按分数丢最弱的
DEFAULT_BUDGET = 110      # 注入给模型的字数额度

# 等级 -> 每天剩余强度的乘数
DAILY_RATE = {0: 1.0, 1: 0.82, 2: 0.90, 3: 0.96, 4: 0.99, 5: 0.997}
# 等级 -> 初始强度
STRENGTH = {0: 1.0, 1: 0.35, 2: 0.5, 3: 0.65, 4: 0.82, 5: 0.95}
LEVEL_NAMES = {0: "不可忘", 1: "随口", 2: "细节", 3: "平常", 4: "在意", 5: "关键"}

# 规则判级：命中即定档，不做语义猜测
RULES = [
    (0, ("不许", "别碰", "不能提", "别提", "禁区", "底线")),
    (5, ("第一次", "纪念日", "一周年", "刚认识你那天")),
    (4, ("我叫", "我的名字", "我住在", "我生日", "我最喜欢", "我最讨厌",
         "受不了", "我超爱", "我根本不吃", "约定", "答应", "说好")),
    # 群友对你做过什么（投喂、戳一戳、起外号）属于互动流水，不是人设。
    # 定成 2 级让它半个月自然淡掉，不然「投喂可乐」会一直挂在档案里，你天天惦记可乐。
    (2, ("投喂", "戳了戳", "戳我", "叫了", "喊了", "外号", "起名")),
    (2, ("记住", "别忘了", "很重要")),
    (2, ("今天", "刚才", "现在", "等一下", "马上到", "这周")),
    (1, ("早安", "晚安", "在吗", "吃了吗", "哈哈", "在干嘛", "好的", "嗯嗯")),
]

NEGATIONS = ("不", "没", "别", "讨厌", "反感")

# 同一个主题只出一条，避免档案里同义说法堆在一起把模型带成复读机
THEME_WORDS = ("投喂", "戳", "对线", "调侃", "找茬", "挑刺", "阴阳", "外号",
               "爱演", "互动", "模仿", "起哄", "喂食")


def _norm_hash(text: str) -> str:
    cleaned = "".join(ch for ch in text if ch.isalnum())
    return hashlib.sha1(cleaned.encode("utf-8", "ignore")).hexdigest()[:16]


def similarity(a: str, b: str) -> float:
    """字符二元组 Jaccard。中文短句里比单字集合更不容易把「对线」和「对骂」混成一件事。"""
    def grams(text: str) -> set:
        body = "".join(ch for ch in (text or "") if ch.isalnum())
        if len(body) < 2:
            return {body} if body else set()
        return {body[i:i + 2] for i in range(len(body) - 1)}

    left, right = grams(a), grams(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def is_refinement(a: str, b: str) -> bool:
    """一句是另一句的扩写，比如「爱跟小鲸鱼对线」和「爱跟小鲸鱼对线吵架」。"""
    short, long = sorted((a or "", b or ""), key=len)
    if len(short) < 3 or short not in long:
        return False
    return len(short) / max(1, len(long)) >= REFINE_RATIO


def classify_level(text: str) -> int:
    body = (text or "").strip()
    if not body:
        return 3
    for level, words in RULES:
        if any(word in body for word in words):
            return level
    return 3


def _new_atom(text: str, now=None) -> dict:
    now = now or time.time()
    level = classify_level(text)
    return {
        "text": text.strip(),
        "level": level,
        "d": STRENGTH[level],     # 剩余强度
        "d_at": now,              # 上次结算衰减的时间
        "hits": 0,
        "created_at": now,
    }


def normalize(item, now=None):
    """把旧格式（纯字符串）转成原子，认不出来的直接丢。"""
    now = now or time.time()
    if isinstance(item, dict) and item.get("text"):
        atom = _new_atom(str(item["text"]), now)
        for key in ("level", "d", "d_at", "hits", "created_at"):
            if key in item:
                try:
                    atom[key] = float(item[key]) if key in ("d", "d_at") else item[key]
                except (TypeError, ValueError):
                    pass
        atom["level"] = max(0, min(5, int(atom.get("level", 3))))
        atom["hits"] = int(atom.get("hits", 0) or 0)
        return atom
    if isinstance(item, str) and item.strip():
        # 老数据里可能还留着「A；B；C」这种被拼过的串，拆回独立原子
        pieces = [p.strip() for p in re.split(r"[；;]", item) if p.strip()]
        if len(pieces) > 1:
            return [_new_atom(p, now) for p in pieces]
        return _new_atom(item, now)
    return None


def migrate(raw_notes):
    """整表迁移：拆串、去重、补齐字段。返回新列表。"""
    atoms = []
    for item in raw_notes or []:
        norm = normalize(item)
        if isinstance(norm, list):
            for atom in norm:
                _insert(atoms, atom)
        elif norm:
            _insert(atoms, norm)
    return atoms


def _looks_conflicting(new_text: str, old_text: str) -> bool:
    return any(w in new_text for w in NEGATIONS) != any(w in old_text for w in NEGATIONS)


def _insert(atoms, atom):
    """按去重规则插进列表：近似就强化，扩写就换长的，冲突就顶掉旧的，否则新增。"""
    text = atom["text"]
    best, best_ratio = None, 0.0
    for existing in atoms:
        if is_refinement(text, existing["text"]):
            best, best_ratio = existing, 1.0
            break
        ratio = similarity(text, existing["text"])
        if ratio > best_ratio:
            best, best_ratio = existing, ratio
    if best is None:
        atoms.append(atom)
        return atoms
    if best_ratio >= DUP_RATIO:
        # 同一条：强化，不新增。等级取高的，别让强化把重要度洗低
        best["hits"] = int(best.get("hits", 0)) + 1
        best["d"] = min(1.0, best["d"] + 0.05)
        best["level"] = max(int(best["level"]), int(atom["level"]))
        # 长的信息更多，留着它
        if len(text) > len(best["text"]) and is_refinement(text, best["text"]):
            best["text"] = text
        return atoms
    if best_ratio >= CONFLICT_RATIO and _looks_conflicting(text, best["text"]):
        # 改主意了：新条顶掉旧的
        atoms.remove(best)
        atoms.append(atom)
        return atoms
    atoms.append(atom)
    return atoms


def apply_decay(atoms, now=None):
    """按距离上次结算的天数重算剩余强度，顺手淘汰淡掉的条目。幂等。"""
    now = now or time.time()
    kept = []
    for atom in atoms:
        try:
            days = max(0.0, (now - float(atom.get("d_at", now))) / 86400)
            rate = DAILY_RATE.get(int(atom.get("level", 3)), 0.96)
            atom["d"] = max(0.0, float(atom.get("d", 0.6)) * (rate ** days))
            atom["d_at"] = now
        except (TypeError, ValueError):
            atom["d"], atom["d_at"] = 0.6, now
        level = int(atom.get("level", 3))
        if level > 0 and atom["d"] < EVICT_RATIO:
            continue
        kept.append(atom)
    return kept


def _score(atom, now=None):
    now = now or time.time()
    age = max(0.0, (now - float(atom.get("created_at", now))) / 86400)
    recency = 0.7 + 0.3 * (0.995 ** age)
    level = int(atom.get("level", 3))
    weight = 0.55 + 0.45 * (level / 5.0)
    return weight * (0.5 + 0.5 * float(atom.get("d", 0.6))) * recency


def _trim(atoms, now=None):
    """硬上限：按分数留最强的，不让列表无限长。"""
    if len(atoms) <= MAX_NOTES:
        return atoms
    ranked = sorted(atoms, key=lambda a: (_score(a, now), int(a.get("hits", 0))), reverse=True)
    return ranked[:MAX_NOTES]


def add_note(atoms, text, now=None):
    """外部入口：压一条印象进去。返回处理后的列表。"""
    text = (text or "").strip()
    if not text:
        return atoms
    atoms = list(atoms or [])
    _insert(atoms, _new_atom(text, now))
    return _trim(atoms, now)


def mark_used(atoms, used_texts):
    """被注入给模型 = 想起来一次，强度回升一点。只改内存，下次落盘时一起写。"""
    if not used_texts:
        return atoms
    used = set(used_texts)
    for atom in atoms:
        if atom.get("text") in used:
            atom["hits"] = int(atom.get("hits", 0)) + 1
            atom["d"] = min(1.0, float(atom.get("d", 0.6)) + 0.08)
    return atoms


def render(atoms, budget=DEFAULT_BUDGET, now=None):
    """挑今天该被想起的几条，拼成一段。返回 (文本, 用到的原文列表)。

    同一个主题只出一条：档案里常见「投喂可乐 / 友好投喂 / 投喂但调侃」这种同一动作的
    不同说法，全塞进提示词只会让模型抓着这个词反复提。
    """
    now = now or time.time()
    if not atoms:
        return "", []
    ranked = sorted(atoms, key=lambda a: _score(a, now), reverse=True)
    picked, used, total, themes = [], [], 0, set()
    for atom in ranked:
        text = atom.get("text", "")
        hit = {w for w in THEME_WORDS if w in text}
        if hit & themes:
            continue
        cost = len(text) + (1 if picked else 0)
        if picked and total + cost > budget:
            continue
        picked.append(text)
        used.append(text)
        themes |= hit
        total += cost
        if total >= budget:
            break
    return "、".join(picked), used


def stats(atoms):
    """给面板/日志看的一行概况。"""
    if not atoms:
        return {"count": 0, "level": {}, "avg_d": 0.0}
    levels = {}
    for atom in atoms:
        key = int(atom.get("level", 3))
        levels[key] = levels.get(key, 0) + 1
    avg_d = sum(float(a.get("d", 0.6)) for a in atoms) / len(atoms)
    return {"count": len(atoms), "level": levels, "avg_d": round(avg_d, 2)}
