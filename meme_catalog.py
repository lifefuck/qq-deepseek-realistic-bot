#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
表情包索引（meme_catalog）

memes_catalog.json 是给 138 张图逐张打过标的成品：图上的字、画面描述、情绪、关键词。
这里把它变成选图用的索引，不再扫目录、不再调视觉模型。

作者: life (lifefuck)
"""

import base64
import collections
import json
import logging
import os
import random
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("DeepSeekBot")

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CATALOG_FILE = os.path.join(_SCRIPT_DIR, "meme-pack", "memes_catalog.json")

# 模型只需要认这 8 个情绪，太细的标签它记不住，反而乱用
BUCKETS: Dict[str, Tuple[str, ...]] = {
    "卖萌": ("卖萌", "心动"),
    "得意": ("得意", "庆祝", "赞同"),
    "嘲讽": ("嘲讽", "嫌弃", "吃瘪"),
    "震惊": ("震惊", "困惑"),
    "愤怒": ("愤怒",),
    "委屈": ("委屈", "哭泣", "要饭"),
    "无奈": ("无奈", "摆烂", "睡觉"),
    "干饭": ("干饭",),
}

_TAG_RE = re.compile(r"[\[【]\s*MEME\s*[:：]\s*([^\]】]{1,12})\s*[\]】]", re.IGNORECASE)

_by_emotion: Dict[str, List[dict]] = collections.defaultdict(list)
_by_keyword: Dict[str, List[dict]] = collections.defaultdict(list)
_recent_paths: collections.deque = collections.deque(maxlen=12)


def _load() -> None:
    if _by_emotion:
        return
    if not os.path.exists(CATALOG_FILE):
        logger.warning("表情包索引不存在: %s", CATALOG_FILE)
        return
    try:
        with open(CATALOG_FILE, "r", encoding="utf-8-sig") as f:
            items = json.load(f)
    except Exception as e:
        logger.warning("读取表情包索引失败: %s", e)
        return

    for it in items:
        fpath = it.get("file_path") or ""
        if fpath and not os.path.isabs(fpath):
            fpath = os.path.join(_SCRIPT_DIR, fpath)
            it["file_path"] = fpath
        if not fpath or not os.path.exists(fpath):
            continue
        emo = (it.get("emotion") or "").strip()
        if emo:
            _by_emotion[emo].append(it)
        for kw in it.get("keywords") or []:
            kw = str(kw).strip()
            # 单字关键词命中率太高，选出来的图往往驴唇不对马嘴
            if len(kw) >= 2:
                _by_keyword[kw].append(it)
    logger.info("表情包索引载入完成: %d 条，%d 个情绪标签", len(items), len(_by_emotion))


def extract_tag(text: str) -> Tuple[str, str]:
    """把回复里的 [MEME:嘲讽] 剥出来，返回 (干净文本, 情绪标签)"""
    if not text:
        return text, ""
    m = _TAG_RE.search(text)
    if not m:
        # 模型手滑写成 [MEME:]/[MEME：情绪+解释] 时也要清掉，别把残渣发给群友
        # 结尾括号也可能被 max_tokens 截断（实测出现过 [MEME:嘲讽 没有右括号），闭合括号设为可选
        leftover = re.sub(r"[\[【]\s*MEME\s*[:：][^\]】\n]*[\]】]?", "", text, flags=re.I)
        return leftover.strip(), ""
    tag = m.group(1).strip()
    return _TAG_RE.sub("", text).strip(), tag


def _candidates_for_tag(tag: str) -> List[dict]:
    _load()
    tag = tag.strip()
    if not tag:
        return []
    for bucket, raws in BUCKETS.items():
        if tag == bucket or tag in raws:
            pool = []
            for raw in raws:
                pool.extend(_by_emotion.get(raw, []))
            if pool:
                return pool
    # 模型自己发明了个不在桶里的标签，退回精确匹配
    return _by_emotion.get(tag, [])


def _candidates_for_text(text: str) -> List[dict]:
    """没有标签时，用回复本身去撞关键词，撞不到再用高频情绪兜底"""
    _load()
    if not text:
        return []
    pool: List[dict] = []
    seen = set()
    for kw, items in _by_keyword.items():
        if kw in text:
            for it in items:
                if it["file_path"] not in seen:
                    seen.add(it["file_path"])
                    pool.append(it)
    if pool:
        return pool
    fallback = []
    for raw in ("卖萌", "得意", "无奈"):
        fallback.extend(_by_emotion.get(raw, []))
    return fallback


MAX_BYTES = 1200 * 1024  # 再大就会撑爆 WebSocket

def _pick_file(items: List[dict]) -> Optional[str]:
    if not items:
        return None
    # 先按体积筛一遍，别挑中了才发现发不出去
    usable = []
    for it in items:
        try:
            if os.path.getsize(it["file_path"]) <= MAX_BYTES:
                usable.append(it)
        except OSError:
            continue
    if not usable:
        return None
    fresh = [it for it in usable if it["file_path"] not in _recent_paths] or usable
    chosen = random.choice(fresh)["file_path"]
    _recent_paths.append(chosen)
    return chosen


# 桶里图太少或者全被体积过滤掉时的备选，别让「干饭」这种只有一个桶直接发不出图
FALLBACK = {
    "干饭": ("卖萌", "得意"),
    "心动": ("卖萌",),
    "委屈": ("卖萌", "无奈"),
    "愤怒": ("嘲讽",),
    "震惊": ("困惑", "卖萌"),
}

def pick_b64(tag: str = "", text: str = "") -> Optional[str]:
    """按情绪标签优先、文本关键词兜底挑一张图，返回 base64；挑不到返回 None"""
    path = None
    if tag:
        for candidate in (tag,) + FALLBACK.get(tag, ()):
            path = _pick_file(_candidates_for_tag(candidate))
            if path:
                break
    if not path:
        path = _pick_file(_candidates_for_text(text))
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except Exception as e:
        logger.warning("读取表情包失败 %s: %s", path, e)
        return None


def stats() -> Dict[str, int]:
    _load()
    return {k: len(v) for k, v in sorted(_by_emotion.items(), key=lambda x: -len(x[1]))}
