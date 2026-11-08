# -*- coding: utf-8 -*-
"""反模板：只削形状，不拦内容。

整句重复好判，难判的是「每条都同一个起手」「每条都用同一句尾巴收」——
单看每条都正常，连着看就是复读机。这里把这些形状特征抽出来做比对，
命中后砍掉模板部分，剩下的仍模型自己的话。

原则：宁可少削，不可削空。突然不理人比说得假更伤。
"""

import random
import re

# 纯语气起手：删掉不伤语义，留着就成了口癖
_FILLER_OPENERS = (
    "不是", "不是吧", "好家伙", "说真的", "说实话", "讲真", "确实", "啊这",
    "笑死", "服了", "我服了", "离谱", "绝了", "行吧", "好吧", "emmm", "嗯",
)

_PUNCT = " \t，。！？、,.!?~～…—-\"'“”‘’()（）[]【】\n"


def _clean(text: str) -> str:
    """去掉所有标点与空白，只留字。

    只 strip 首尾是不够的：「不是，这也」和「不是，你认」其实同一个起手，
    中间那个逗号会把它们判成两句不同的话。
    """
    return re.sub(r"[\s，。！？、,.!?~～…—\-'\"“”‘’()（）\[\]【】\n]", "", text or "")


_PARTICLES = "啊呀呢吧嘛哦哈啦"


def strip_echo_opener(reply: str, user_text: str, min_keep: int = 4) -> str:
    """砍掉「把对方的话搬回来 + 问号」这种开场。

    「cpdd？」「神子？」「确实？」连着出现，一眼就是复读机：真人不会每条都
    先复述对方的词再表态。只砍掉开头那一截，后面的内容原样保留。
    """
    text = (reply or "").strip()
    if not text or not user_text:
        return reply
    m = re.match(r"^([^\s，。！？,.!?]{1,8})[？?]\s*", text)
    if not m:
        return reply
    echo = m.group(1)
    if len(echo) < 2 or echo not in _clean(user_text):
        return reply
    rest = text[m.end():].strip()
    if len(rest) < min_keep:
        return reply
    return rest


def trim_particle(text: str, recent) -> str:
    """连着三条都用「啊/呢/呀」收尾时，把第三条的尾巴摘掉。

    单看每条都正常，连起来就是同一个呼吸节奏；摘一个语气词比改一句话便宜。
    """
    text = (text or "").strip()
    if not text or text[-1] not in _PARTICLES:
        return text
    tail = text[-1]
    same = sum(1 for r in (recent or [])[-2:] if r and str(r).strip()[-1:] == tail)
    if same >= 2:
        return text[:-1].rstrip("，, ")
    return text


def opening(text: str, width: int = 4) -> str:
    """起手式：去掉标点后的前几个字。"""
    return _clean(text)[:width]


def _opening_key(text: str) -> str:
    """起手式的比较键。

    光比前四个字不够：「不是，这也太离谱」和「不是，你认真的」前四个字不同，
    但群里一眼看过去就是同一个口癖。所以先认语气起手，再退回前四字。
    """
    clean = _clean(text)
    for filler in sorted(_FILLER_OPENERS, key=len, reverse=True):
        if clean.startswith(filler):
            return "F:" + filler
    return "N:" + clean[:4]


def ending(text: str, width: int = 3) -> str:
    """收尾词：去掉标点后的最后几个字。"""
    return _clean(text)[-width:]


def split_clauses(text: str) -> list[str]:
    return [c.strip() for c in re.split(r"[，,。！？!?~～\n]+", text or "") if c.strip()]


def match(text: str, recent: list[str]) -> str:
    """返回命中的模板类型：opening / ending / none。

    recent 传本群最近发过的原文（越新越靠后）。
    """
    if not text or not recent:
        return "none"
    op = _opening_key(text)
    if len(op) >= 3:
        for r in recent[-4:]:
            if _opening_key(r) == op:
                return "opening"
    end = ending(text)
    if len(end) >= 2:
        for r in recent[-3:]:
            if ending(r) == end:
                return "ending"
    return "none"


def reshape(text: str, reason: str) -> str:
    """按命中的模板类型削形。

    opening：把「不是，」「好家伙，」这种纯语气起手去掉；没有可去的就是换开头，
             交给模型下一轮，不硬改内容。
    ending：结尾那个短促的反问/吐槽尾巴（「是吧」「真服了」）砍掉。
    """
    original = text
    text = (text or "").strip()
    if not text:
        return original

    if reason == "opening":
        clauses = split_clauses(text)
        if len(clauses) > 1 and clauses[0] in _FILLER_OPENERS:
            text = "，".join(clauses[1:])
        elif len(clauses) > 2:
            # 起手撞车但内容还多：留后半段，别整条删
            text = "，".join(clauses[1:])
    elif reason == "ending":
        clauses = split_clauses(text)
        if len(clauses) > 1 and len(clauses[-1]) <= 6:
            text = "，".join(clauses[:-1])
        elif len(clauses) == 1 and len(clauses[0]) > 12:
            # 单句太长又用了口头禅收尾，把尾巴那截砍掉
            tail = ending(text, 3)
            if tail and text.endswith(tail):
                text = text[: -len(tail)].rstrip(_PUNCT)

    text = text.strip(_PUNCT + "，")
    if not text:
        return original
    return text


def shape_report(text: str) -> dict:
    """给日志/测试看的形状快照。"""
    return {
        "opening": opening(text),
        "ending": ending(text),
        "clauses": len(split_clauses(text)),
        "length": len(_clean(text)),
        "has_question": bool(re.search(r"[?？吗呢嘛]", text or "")),
        "has_ellipsis": "…" in (text or "") or "..." in (text or ""),
    }
