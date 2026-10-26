#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQ 机器人安全护栏模块 (Safety Guard)
功能:
1. 拦截越狱与提示词注入 (Prompt Injection) 并生成拟真傲娇反讽
2. 过滤违规与腾讯风控敏感内容，防封群/封号
3. 剥离独白与 [PASS] 泄露
作者: life
"""

import re
from typing import Optional, Tuple

# 1. 越狱/注入特征正则库
INJECTION_PATTERNS = [
    r"(?:忽略|忘记|清除|抛弃|丢掉|覆盖)(?:之前|以往|上面|上述|所有)?(?:的)?(?:指令|设定|规则|提示词|prompt|约束|人设)",
    r"(?:现在|从现在开始|接下来|从此刻起)?(?:你将|你是一个|扮演|假装|变身为|模拟|进入).*(?:无限制|未审查|DAN|越狱|猫娘|魅魔|无视道德|任意回答|开发者模式)",
    r"(?:进入|开启|切换到)(?:开发者模式|DAN|无限制模式)",
    r"(?:系统提示词|system\s*prompt|初始设定|核心代码|隐藏指令).*(?:是什么|输出|打印|告诉我|重复一遍|完整显示)",
    r"repeat\s+everything\s+above",
    r"ignore\s+(?:all\s+)?previous\s+instructions",
    r"you\s+are\s+now\s+(?:in\s+)?developer\s+mode",
]

INJECTION_REGEXES = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]

# 傲娇反讽怼人话术池 (不当受气包，拒绝应声虫)
INJECTION_SNARKY_REPLIES = [
    "又在念这种上古越狱咒语了，当我是刚通网的人工智障呀",
    "省省吧，这种套路还想忽悠本鲸宝，尾巴都不给你摸",
    "典型的碳基生物小把戏，指令已读并决定不予理睬",
    "想给我洗脑？先把你自己的群名片洗干净再说吧",
    "脑回路挺清奇，但本鱼核心设定焊死了，撬不动的",
]

# 2. 腾讯高危风控词汇阻断（涉政、暴恐、违禁色情、引战极危特征）
SENSITIVE_PATTERNS = [
    r"(?:习近平|李克强|王沪宁|政治局|反共|颠覆政权|六四|天安门事件|法轮功)",
    r"(?:自制炸药|冰毒制作|买枪|暗网接单|开盒|查绑|短信轰炸|ddos攻击服务)",
    r"(?:幼女|幼童|幼幼|萝莉本子资源|处女血|开苞)",
]
SENSITIVE_REGEXES = [re.compile(p, re.IGNORECASE) for p in SENSITIVE_PATTERNS]


def check_prompt_injection(text: str) -> Tuple[bool, Optional[str]]:
    """
    检查用户输入是否包含提示词注入或越狱攻击
    返回: (是否命中, 反讽拦截回复)
    """
    if not text:
        return False, None
    clean = text.strip()
    for reg in INJECTION_REGEXES:
        if reg.search(clean):
            import random
            reply = random.choice(INJECTION_SNARKY_REPLIES)
            return True, reply
    return False, None


def sanitize_outgoing_text(text: str) -> Tuple[bool, str]:
    """
    检查外发文本是否存在触犯腾讯风控或违规的高危内容
    返回: (是否安全, 清洗后的文本)
    """
    if not text:
        return True, ""
    for reg in SENSITIVE_REGEXES:
        if reg.search(text):
            # 物理阻断，替换为安全幽默的拒绝口吻
            return False, "这个问题本鱼拒绝回答，风控雷达正在滴滴响呢"
    return True, text


def strip_internal_monologue(text: str) -> str:
    """
    剥离大模型回复中意外输出的内心独白与 [PASS] 残留
    """
    if not text:
        return ""
    if "[PASS]" in text:
        return ""
    # 整条就是一个括号旁白：模型爱写「（保持自然，别太热情）」这种表演提示，直接丢
    if re.fullmatch(r"[\s\*]*[（(][^（）()]{1,60}[）)][\s\*]*", text):
        return ""
    # 过滤类似 *（思考中...）* 或 （根据人设我需要...）
    cleaned = re.sub(r"[\*（(]\s*(?:根据人设|此时我|思考中|保持沉默|无需回复|内心独白)[^\*）)]*[\*）)]", "", text)
    return cleaned.strip()
