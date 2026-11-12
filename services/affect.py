# -*- coding: utf-8 -*-
"""轻量心境：让「心情」有惯性，而不是每条回复都重新掷骰子。

借鉴大肥鱼（whyever77/dafeiyu-astrbot）affective_core 的思路，但砍到只留
群里用得上的四个状态，避免为一个群聊 bot 背一整套情感计算：

    valence  情绪效价（-1 难受 ~ 1 开心）
    arousal  唤醒度（0 困倦 ~ 1 兴奋）
    pout     小别扭（0 没事 ~ 1 真生气）
    share_urge 分享欲（0 懒得说 ~ 1 憋不住想说）

关键不是数值本身，是两件事：
1. 状态有半衰期，不会「睡一觉还在生气」，也不会「刚吵完立刻没事」；
2. 状态只以「体感」形式进 prompt，绝不把分数喂给模型——模型看见数字就会
   在回复里报数值、演情绪，那比没有情绪更假。
"""

import json
import logging
import os
import random

_logger = logging.getLogger(__name__)
import re
import time

# 各状态的半衰期（秒）。pout 45 分钟，情绪 4 小时，唤醒 2.5 小时。
_HALF_LIFE = {
    "valence": 4 * 3600,
    "arousal": 2.5 * 3600,
    "pout": 45 * 60,
    "share_urge": 4 * 3600,
}

_DEFAULT = {
    "valence": 0.10,
    "arousal": 0.55,
    "pout": 0.0,
    "share_urge": 0.30,
    "updated": 0.0,
    "last_bot_reply": 0.0,
    "bot_reply_answered": True,
    "pout_reason": "",
    "care": [],
}

_state = {}
_path = ""
_writer = None
_last_save = 0.0
_dirty = False

_APOLOGY_RE = re.compile(r"(对不起|抱歉|不好意思|我错|别生气|原谅|消消气)")
_DISTRESS_RE = re.compile(r"(难受|好累|累死|烦死|崩溃|想哭|哭了|失眠|生病|发烧|感冒|头疼|胃疼|压力好大|emo)")
_FUN_RE = re.compile(r"(哈哈|笑死|好耶|可爱|喜欢你|谢谢|牛逼|太强了|绝了)")
_SERIOUS_RE = re.compile(r"(报错|配置|代码|怎么|如何|为什么|求助|帮我|请问|方案|部署)")

_CARE_RULES = (
    (re.compile(r"(生病|发烧|感冒|头疼|胃疼|失眠|熬夜|不舒服)"), "身体不太舒服"),
    (re.compile(r"(加班|考试|论文|面试|deadline|赶工|项目|压力)"), "最近在赶事、压力大"),
    (re.compile(r"(心情不好|难过|烦|崩溃|emo|哭了|低落)"), "情绪不太好"),
)

_SLEEP_HINT = "困了，反应慢半拍，句子短一点"


def attach(writer=None, path: str = ""):
    """由 bot.py 注入写盘函数与路径。测试里可以完全不调。"""
    global _writer, _path
    _writer = writer
    _path = path
    load()


def load():
    global _state, _dirty
    if not _path or not os.path.exists(_path):
        return
    try:
        with open(_path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _state = {str(k): {**_DEFAULT, **v} for k, v in data.items() if isinstance(v, dict)}
        _dirty = False
    except Exception:
        # 坏文件不该拖垮 bot：忽略旧状态，从默认值重新开始
        _state = {}


def save(force: bool = False):
    """攒 30 秒写一次：每条消息都落盘没必要，掉电丢几分钟心情无伤大雅。"""
    global _last_save, _dirty
    if not _path or not _writer:
        return
    now = time.time()
    if not force and (not _dirty or now - _last_save < 30):
        return
    try:
        _writer(_path, _state, pretty=False)
        _last_save = now
        _dirty = False
    except Exception as e:
        # 原来这里是光秃秃的 except: pass：磁盘满/权限错时心境状态永远回滚到旧快照，
        # 日志里连一行都没有，线上完全看不出有问题
        _logger.warning("心境状态落盘失败（%s），本次跳过：%s", _path, e)


def _hour(now: float | None = None) -> int:
    return time.localtime(now if now is not None else time.time()).tm_hour


def _circadian(hour: int) -> dict:
    """昼夜基线：白天的她本来就更活络，凌晨本来就蔫。"""
    if 8 <= hour < 23:
        return {"valence": 0.15, "arousal": 0.60}
    if 23 <= hour or hour < 1:
        return {"valence": 0.05, "arousal": 0.35}
    if 1 <= hour < 7:
        return {"valence": -0.08, "arousal": 0.20}
    return {"valence": 0.02, "arousal": 0.42}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def settle(state: dict, now: float | None = None) -> dict:
    """按经过时间把状态往基线拉。纯函数，方便测试。"""
    now = now if now is not None else time.time()
    last = float(state.get("updated") or 0.0)
    if last <= 0:
        state["updated"] = now
        return state
    dt = max(0.0, now - last)
    base = _circadian(_hour(now))
    for key, half in _HALF_LIFE.items():
        cur = float(state.get(key, _DEFAULT[key]))
        target = base.get(key, _DEFAULT[key])
        factor = 0.5 ** (dt / half)
        state[key] = cur + (target - cur) * (1 - factor)

    # 跨夜重置：睡醒且三小时没互动，别扭自动消——人不会隔夜还记着上一句嘴
    if dt > 3 * 3600 and 7 <= _hour(now) < 11:
        state["pout"] = float(state["pout"]) * 0.2
        state["valence"] = max(float(state["valence"]), 0.0)
        state["pout_reason"] = ""

    state["updated"] = now
    return state


def _get(group_id) -> dict:
    key = str(group_id)
    if key not in _state:
        _state[key] = dict(_DEFAULT)
    return settle(_state[key])


def _add_care(state: dict, topic: str, now: float):
    """牵挂账本：同一件事只记一次，4 小时后才允许问，最多问两次。"""
    threads = state.setdefault("care", [])
    threads = [t for t in threads if isinstance(t, dict) and now - float(t.get("created", 0)) < 48 * 3600]
    if any(t.get("topic") == topic for t in threads):
        state["care"] = threads[-5:]
        return
    threads.append({"topic": topic, "created": now, "due": now + 4 * 3600,
                    "prompted": 0, "last": 0.0})
    state["care"] = threads[-5:]


def on_group_message(group_id, text: str, private: bool = False, now: float | None = None):
    """一条群友消息对心境的影响。全部本地规则，零 token。"""
    global _dirty
    now = now if now is not None else time.time()
    state = _get(group_id)
    text = text or ""

    # 有人接话：之前的「被晾着」记账清零
    if state.get("last_bot_reply") and not state.get("bot_reply_answered", True):
        state["bot_reply_answered"] = True

    if _APOLOGY_RE.search(text):
        # 被哄：破功门控。这是防止「带情绪服务」变成「耍脾气不干活」的关键
        state["pout"] = float(state["pout"]) * 0.15
        state["valence"] = _clamp(float(state["valence"]) + 0.25, -1, 1)
        state["share_urge"] = _clamp(float(state["share_urge"]) + 0.1, 0, 1)
        state["pout_reason"] = ""

    if _DISTRESS_RE.search(text):
        state["valence"] = _clamp(float(state["valence"]) - 0.08, -1, 1)
        state["pout"] = float(state["pout"]) * 0.5
        for pattern, topic in _CARE_RULES:
            if pattern.search(text):
                _add_care(state, topic, now)
                break

    if _SERIOUS_RE.search(text) and len(text) >= 10:
        # 问正事时先把别扭压下去：情绪不能压过该干的活
        state["pout"] = float(state["pout"]) * 0.35

    if _FUN_RE.search(text):
        state["valence"] = _clamp(float(state["valence"]) + 0.07, -1, 1)
        state["arousal"] = _clamp(float(state["arousal"]) + 0.06, 0, 1)
        state["share_urge"] = _clamp(float(state["share_urge"]) + 0.08, 0, 1)

    _dirty = True
    save()
    return state


def on_favor_delta(group_id, delta: float, text: str = ""):
    """好感度评估结果顺手喂给心境，不额外调用模型。"""
    global _dirty
    if not delta:
        return
    state = _get(group_id)
    if delta >= 3:
        state["valence"] = _clamp(float(state["valence"]) + 0.12, -1, 1)
        state["pout"] = float(state["pout"]) * 0.6
        state["share_urge"] = _clamp(float(state["share_urge"]) + 0.05, 0, 1)
    elif delta <= -3:
        state["pout"] = _clamp(float(state["pout"]) + 0.30, 0, 1)
        state["valence"] = _clamp(float(state["valence"]) - 0.12, -1, 1)
        state["pout_reason"] = (text or "")[:20]
    _dirty = True
    save()


def on_bot_spoke(group_id, now: float | None = None):
    """记下 bot 刚说过话，用于「没人接」的寂寞累积和插话冷却。"""
    global _dirty
    now = now if now is not None else time.time()
    state = _get(group_id)
    state["last_bot_reply"] = now
    state["bot_reply_answered"] = False
    _dirty = True


def is_last_reply_answered(group_id) -> bool:
    """上一条 bot 发言有没有人接。用于插话时机降权。"""
    state = _get(group_id)
    return bool(state.get("bot_reply_answered", True))


def tick(group_id, now: float | None = None):
    """定时调用：没人接话时，别扭和分享欲都慢慢涨一点。"""
    global _dirty
    now = now if now is not None else time.time()
    state = _get(group_id)
    last = float(state.get("last_bot_reply") or 0)
    if last and not state.get("bot_reply_answered", True) and now - last > 15 * 60:
        state["pout"] = _clamp(float(state["pout"]) + 0.06, 0, 1)
        state["share_urge"] = _clamp(float(state["share_urge"]) + 0.12, 0, 1)
        # 只记一次，别让时间越久越委屈
        state["bot_reply_answered"] = True
        _dirty = True
        save()


def profile(group_id, now: float | None = None) -> dict:
    """此刻的表达参数：回几条、几个表情、节奏快慢。"""
    state = _get(group_id)
    hour = _hour(now)
    sleepy = hour >= 23 or hour < 7
    valence, arousal, pout = float(state["valence"]), float(state["arousal"]), float(state["pout"])

    max_segments = 3 if (valence > 0.25 and arousal > 0.5 and pout < 0.5) else 2
    if pout > 0.5:
        max_segments = 2
    if sleepy:
        max_segments = 2

    max_faces = 2 if valence > 0.35 and pout < 0.4 else 1
    if pout > 0.5:
        max_faces = 1

    delay_mult = 1.0
    if arousal > 0.7:
        delay_mult = 0.85
    if pout > 0.4:
        delay_mult = 1.15
    if sleepy:
        delay_mult = 1.4

    return {
        "max_segments": max_segments,
        "max_faces": max_faces,
        "delay_mult": delay_mult,
        "emoji_keep": 0.05 if pout > 0.5 else (0.22 if valence > 0.3 else 0.15),
        "sleepy": sleepy,
    }


def prompt_memo(group_id, now: float | None = None) -> str:
    """把状态说成一句体感中文。绝不出现数值、不出现「心情」这种标签词。"""
    state = _get(group_id)
    hour = _hour(now)
    sleepy = hour >= 23 or hour < 7
    pout, valence, arousal = float(state["pout"]), float(state["valence"]), float(state["arousal"])
    lines = []

    if pout > 0.45:
        lines.append("心里还有点堵，不太想主动搭话，别人来哄也别一下子就好了")
    elif pout > 0.2:
        lines.append("有点小别扭，嘴硬，但没真生气")
    if valence > 0.45 and arousal > 0.55:
        lines.append("心情挺好，话比平时多一点，接得住梗")
    elif valence < -0.25:
        lines.append("情绪有点低，别硬撑热情，回短一点没关系")
    if sleepy:
        lines.append(_SLEEP_HINT)
    elif arousal < 0.3:
        lines.append("有点提不起劲，反应慢半拍")

    if not lines:
        return ""
    return ("【此刻的感觉（只影响语气和长短，别把这段说出来，别提任何分数或状态名）】："
            + "；".join(lines))


def care_hint(group_id, now: float | None = None) -> str:
    """到点且没问过的牵挂。治「机器人每十分钟问一次你吃饭没」。"""
    global _dirty
    now = now if now is not None else time.time()
    state = _get(group_id)
    for t in state.get("care", []):
        if not isinstance(t, dict):
            continue
        if int(t.get("prompted", 0)) >= 2:
            continue
        if now < float(t.get("due", 0)):
            continue
        if now - float(t.get("last", 0)) < 8 * 3600:
            continue
        if now - float(t.get("created", 0)) > 24 * 3600:
            continue
        t["prompted"] = int(t.get("prompted", 0)) + 1
        t["last"] = now
        _dirty = True
        save()
        return f"【惦记着的事】：对方之前提过「{t['topic']}」，可以顺口问一句，别问第二遍，也别显得在查岗。"
    return ""


def style_variant(prob: float = 0.05) -> str:
    """偶尔换个说法，打破「每条都工整」的均匀感。

    只给方向，不给范例——给了范例模型就会照着抄，抄多了又成新模板。
    """
    if random.random() >= prob:
        return ""
    return random.choice((
        "这条只回两三个字，别解释",
        "这条只说半句，别讲完",
        "这条带一点翻译腔，但要短",
        "这条先反问一句，别急着回答",
    ))


def reset(group_id=None):
    """测试/管理员用：清掉某个群或全部的心境。"""
    if group_id is None:
        _state.clear()
    else:
        _state.pop(str(group_id), None)
    save(force=True)
