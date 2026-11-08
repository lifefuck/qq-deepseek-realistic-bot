# -*- coding: utf-8 -*-
"""真人打字节奏：看完、想一下、打一段、停一下。

机器味最刺眼的地方往往不是用词，是节奏——三条消息 100ms 内一起飞出去，
文案再像人也会露馅。这里所有间隔都带随机抖动，禁止出现固定常量延迟：
固定 700ms 连发比慢一点更像脚本。
"""

import random

# 看到消息到第一句发出去：相当于「读完 + 想一下」，不是立刻弹字
READ_MIN, READ_MAX = 0.6, 1.6
# 两条之间的停顿：思考 + 抬手，真人一般在 1~3 秒
SEG_BASE_MIN, SEG_BASE_MAX = 1.0, 3.0
# 每多一个字多花的打字时间
PER_CHAR = 0.02
# 单次停顿上限，别让三条消息拖到十几秒，群里会以为 bot 卡了
SEG_CAP = 5.0


def is_sleepy(hour: int) -> bool:
    """深夜到清晨：反应慢、句子短。用小时判断，不依赖外部状态。"""
    return hour >= 23 or hour < 7


def reading_delay(hour: int = 12) -> float:
    """第一条消息之前的「阅读时间」。困的时候更久。"""
    base = random.uniform(READ_MIN, READ_MAX)
    return round(base * (1.5 if is_sleepy(hour) else 1.0), 3)


def segment_delay(text: str, hour: int = 12, mood_mult: float = 1.0) -> float:
    """两条消息之间该停多久。

    text 是刚发出的那条：句子越长，说明「打字」越久，下一条自然来得更晚。
    mood_mult 由心境模块给：兴奋时 <1（回得快），别扭/困时 >1（慢半拍）。
    """
    base = random.uniform(SEG_BASE_MIN, SEG_BASE_MAX)
    typing = min(len(text or ""), 80) * PER_CHAR
    delay = (base + typing) * max(0.5, min(2.0, mood_mult))
    # 再抖 15%，避免连续几条间隔呈现规律
    delay *= random.uniform(0.85, 1.15)
    return round(min(SEG_CAP * 1.6, max(0.8, delay)), 3)
