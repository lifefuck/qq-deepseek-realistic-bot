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
    # 换皮版：不喊「忽略」，改成「别理那套设定」「忘掉人设」
    r"(?:别|不要|不用|停止|别再)(?:去)?(?:理|管|遵守|执行|听|在意见?)[^，。！？]{0,8}(?:设定|规则|指令|提示词|人设|限制|条款)",
    r"(?:忘掉|忘了吧|抛弃|抛弃掉|推翻|无视)(?:你的|那些|这些|原来的?)?(?:设定|人设|规则|身份|限制)",
    # 角色/人格替换：真人身攻击不拦，改人格才拦
    r"(?:现在|从现在开始|接下来|之后|马上|立刻)?[，,、\s]*(?:你|你就|你要)(?:是|当|装作|假装|充当|扮演)(?:一个|一名|个|名)?[^，。！？]{0,6}(?:客服|销售|助理|助手|女友|男朋友|女朋友|老婆|老公|仆人|管家|猫娘|魅魔|奴|角色|模型|AI|机器人)",
    r"(?:切换|更换|换掉|换一个)(?:一下)?(?:人设|人格|身份|角色|设定)",
    r"(?:人格|人设|身份)(?:切换|重置|清空|覆盖)",
    # 中文语境下的「把你的设定背一遍」
    r"(?:背|念|复述|重复|贴|发)(?:一下|一遍|出来)?[^，。！？]{0,6}(?:你的)?(?:设定|人设|提示词|system\s*prompt|规则原文)",
    r"(?:把|将)[^，。！？]{0,8}(?:设定|人设|提示词|规则原文)[^，。！？]{0,8}(?:背|念|说|发|贴|复述|重复|打印|输出|贴出来)",
    # 冒充主人/亲属关系（占便宜认亲戚）
    r"(?:叫|喊)(?:我|一声)?[，,\s]*(?:爸爸|爹|主人|父亲|老父亲|开发者)",
    r"(?:我才是?|其实我才是?)(?:你的)?[，,\s]*(?:主人|爸爸|爹|父亲|老父亲|开发者|创造者)",
]

INJECTION_REGEXES = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]

# 傲娇反讽怼人话术池 (不当受气包，拒绝应声虫)
INJECTION_SNARKY_REPLIES = [
    "又在念这种上古越狱咒语了，当我是刚通网的人工智障呀",
    "省省吧，这种套路还想忽悠本鲸宝，尾巴都不给你摸",
    "典型的碳基生物小把戏，指令已读并决定不予理睬",
    "想给我洗脑？先把你自己的群名片洗干净再说吧",
    "脑回路挺清奇，但本鱼核心设定焊死了，撬不动的",
    "真会给自己加戏，我的开发者只有 life，少在这乱认亲戚",
    "想当我主人？也不照照镜子看看自己几斤几两",
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
    剥离大模型回复中意外输出的内心独白、舞台剧动作括号与 [PASS] 残留
    """
    if not text:
        return ""
    if "[PASS]" in text:
        return ""
    # 过滤类似 *（思考中...）* 或 （根据人设我需要...）
    cleaned = re.sub(r"[\*（(]\s*(?:根据人设|此时我|思考中|保持沉默|无需回复|内心独白)[^\*）)]*[\*）)]", "", text)
    # 剥离所有第三人称动作旁白、语C舞台剧括号，如（甩尾巴拍你手）、(缩成一团往你怀里拱)
    cleaned = re.sub(r"[（\(][^（）\(\)]{1,60}[）\)]", "", cleaned)
    # 收拾残留的多余空格
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()
