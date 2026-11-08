# -*- coding: utf-8 -*-
"""随手打错字，偶尔再补一句「打错了」。

为什么不用固定映射表：映射表只有十来个词，4% 概率反复替换，用两天群里
就总结出「它老把确实写成确式」，错字本身变成新口癖。这里改成候选池随机，
错误类型也分三种（同音替换、漏字、叠字），并且不碰链接、@、CQ 码和数字。

纠正不是每次都有：真人打错了也经常懒得改。低概率补一条，才像手滑。
"""

import random
import re

# 同音/近音替换池：都是中文输入法里真实会选错的词
_TYPO_PAIRS = (
    ("已经", "以经"),
    ("因为", "因位"),
    ("觉得", "决得"),
    ("应该", "因该"),
    ("知道", "直到"),
    ("一起", "以起"),
    ("时候", "时侯"),
    ("现在", "现再"),
    ("还是", "孩是"),
    ("真的", "真得"),
    ("可以", "可一"),
    ("怎么", "肿么"),
    ("东西", "东希"),
    ("然后", "然候"),
    ("需要", "须要"),
    ("直接", "直结"),
    ("问题", "问提"),
    ("感觉", "感角"),
)

# 不允许被改动的片段：链接、@、CQ 码、纯数字
_PROTECTED_RE = re.compile(r"(https?://\S+|\[CQ:[^\]]+\]|@\S+|\d+(?:\.\d+)?)")


class TypoResult:
    __slots__ = ("text", "wrong", "right")

    def __init__(self, text: str, wrong: str, right: str):
        self.text = text
        self.wrong = wrong
        self.right = right


def _safe_to_typo(text: str) -> bool:
    """短句不打错、带链接/@/数字的不打错：改坏了比没有人味更糟。"""
    if len(text) < 8:
        return False
    if _PROTECTED_RE.search(text):
        return False
    return True


def _pick_pair(text: str):
    pairs = [(s, d) for s, d in _TYPO_PAIRS if s != d and s in text]
    if not pairs:
        return None
    # 优先改后半句：真人打长句时越往后越容易手滑
    pairs.sort(key=lambda p: text.rfind(p[0]), reverse=True)
    return random.choice(pairs[:2])


def maybe_typo(text: str, prob: float = 0.06) -> TypoResult | None:
    """按概率制造一处手滑。返回 None 表示这条保持干净。"""
    if not _safe_to_typo(text) or random.random() >= prob:
        return None

    roll = random.random()
    if roll < 0.65:
        pair = _pick_pair(text)
        if not pair:
            return None
        src, dst = pair
        return TypoResult(text.replace(src, dst, 1), dst, src)

    # 漏字：从后往前挑一个两字词，砍掉一个字（「就是」→「就」）
    words = [m for m in re.finditer(r"[\u4e00-\u9fff]{2}", text)]
    if words:
        m = random.choice(words[-3:])
        dropped = text[: m.start() + 1] + text[m.end():]
        if len(_clean_len(dropped)) >= 6:
            return TypoResult(dropped, text[m.start():m.end()], text[m.start():m.end()])

    # 叠字：把某个字重复一次，像输入法粘连
    idx = [i for i, ch in enumerate(text) if "\u4e00" <= ch <= "\u9fff"]
    if idx:
        i = random.choice(idx)
        doubled = text[: i + 1] + text[i] + text[i + 1:]
        return TypoResult(doubled, text[i], text[i] * 2)
    return None


def _clean_len(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?]" , "", text)


def correction_line(result: TypoResult) -> str:
    """补一句纠正。只提打错的那个词，不复述整句。"""
    word = result.right
    return random.choice((
        f"打错了，是{word}",
        f"手滑，{word}",
        f"改一下，{word}",
    ))


def should_correct(prob: float = 0.3) -> bool:
    """打错之后只有三成会改：真人群里更多人将错就错。"""
    return random.random() < prob
