"""
跨群公共话题滚动记忆模块
1. 共享滚动话题事件总线：提取客观公共事件/热梗（不含私聊及群友个人好感），让多群聊天有连贯认知。
2. 零 I/O 阻塞：写内存 + 脏标记，后台独立线程/异步任务每 2 秒合并刷盘一次，杜绝热路径磁盘阻塞。
3. 同一件事只留一条：近似话题合并计数，注入时同簇只出一次，同一批内容 2 小时内不再重复喂。
   没有这层的时候，群里连着几天聊「投喂可乐」，六条可乐霸占了全部注入名额，模型天天惦记可乐。
"""
import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from typing import Deque, Dict, List, Optional

import memory_atoms

logger = logging.getLogger("DeepSeekBot")

CROSS_GROUP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cross_group_topics.json")
MAX_TOPICS = 30
FLUSH_INTERVAL = 2.0  # 2秒异步合并刷盘
DUP_RATIO = 0.45      # 整句改写时才靠它兜底
FRESH_WINDOW = 6 * 3600     # 只看 6 小时内的话题，老梗别当新闻讲
REPEAT_COOLDOWN = 2 * 3600  # 同一批话题 2 小时内不再注入，避免连着几轮都在提可乐

# 「投喂可乐」「再给你一箱冰镇可乐」句式完全不同，重合度算出来只有 0.06，
# 但它们确实是同一件事。所以同簇判定看的是共享的实词，不是句式。
_STOP_GRAMS = {
    "今天", "明天", "现在", "刚才", "什么", "怎么", "这个", "那个", "我们", "你们",
    "他们", "可以", "就是", "不是", "没有", "一个", "一下", "因为", "所以", "但是",
    "真的", "还是", "已经", "自己", "知道", "觉得", "应该", "不要", "一起", "东西",
    "时候", "然后", "如果", "感觉", "有点", "好像", "哈哈", "这样", "那样",
}


def _grams(text: str) -> set:
    body = "".join(ch for ch in (text or "") if ch.isalnum())
    return {body[i:i + 2] for i in range(len(body) - 1)} if len(body) >= 2 else set()


def same_topic(a: str, b: str) -> bool:
    """共享一个非停用的双字实词就算同一件事。"""
    shared = {g for g in _grams(a) & _grams(b) if g not in _STOP_GRAMS}
    return bool(shared) or memory_atoms.similarity(a, b) >= DUP_RATIO

def _clean(content: str) -> str:
    """CQ 码是协议细节，留着会原样进提示词，模型看到一堆 [CQ:at,qq=...] 只会发懵。"""
    text = re.sub(r"\[CQ:[^\]]+\]", "", content or "").strip()
    return re.sub(r"\s+", " ", text)


class CrossGroupTopicManager:
    def __init__(self, file_path: str = CROSS_GROUP_FILE):
        self.file_path = file_path
        self.topics: Deque[Dict[str, any]] = deque(maxlen=MAX_TOPICS)
        self.is_dirty = False
        self._last_inject: Dict[str, float] = {}  # 话题内容 -> 上次注入时间
        self._flush_task: Optional[asyncio.Task] = None
        self._load()

    def _load(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    before = len(self.topics)
                    merged = 0
                    for item in data:
                        if isinstance(item, dict) and "content" in item:
                            if _clean(item.get("content", "")) != item.get("content"):
                                self.is_dirty = True  # 老数据里带 CQ 码，清洗后顺手落盘
                            merged += 1 if self._push(item) else 0
                if merged > 0:
                    self.is_dirty = True  # 合并过就顺手把清洗结果落盘
                logger.info("跨群公共话题记忆已加载: %d 条记录（合并 %d 条近似项）", len(self.topics), merged)
            except Exception as e:
                logger.warning("加载跨群记忆异常: %s", e)

    def _find_similar(self, content: str) -> Optional[Dict[str, any]]:
        """在已存的话题里找说的是同一件事的那条。"""
        for item in reversed(self.topics):
            if same_topic(content, item.get("content", "")):
                return item
        return None

    def _push(self, item: Dict[str, any]):
        content = _clean(item.get("content", ""))
        if len(content) < 5:
            return False
        item["content"] = content[:150]
        same = self._find_similar(content)
        if same is not None:
            # 同一件事只累加热度，不刷新时间戳——刷新等于给老梗续命，它就永远不会过期。
            # 内容换成最新那句：池子回答的是「隔壁群最近在聊什么」，不是「最早提过什么」
            same["hits"] = int(same.get("hits", 1)) + 1
            same["last_seen"] = item.get("ts", time.time())
            same["content"] = content[:150]
            same["sender"] = item.get("sender", same.get("sender"))
            return True
        item.setdefault("hits", 1)
        self.topics.append(item)
        return False

    def record_topic(self, group_id: int, sender_nick: str, content: str, topic_tag: Optional[str] = None):
        """记录一条公共话题（热路径仅写内存与置脏标记）"""
        c = _clean(content)
        # 过滤过短或无营养的水聊
        if len(c) < 5 or c.startswith(("/", "#", "*", "%")):
            return

        self._push({
            "ts": time.time(),
            "group_id": group_id,
            "sender": sender_nick,
            "tag": topic_tag,
            "content": c[:150]
        })
        self.is_dirty = True

    def get_recent_topics_prompt(self, exclude_group_id: Optional[int] = None, limit: int = 2) -> str:
        """取其他群的近期话题，给模型当背景，不直接当台词"""
        now = time.time()
        candidates: List[tuple] = []
        for t in self.topics:
            if exclude_group_id and t.get("group_id") == exclude_group_id:
                continue
            age = now - t.get("ts", 0)
            if age > FRESH_WINDOW:
                continue
            if now - self._last_inject.get(t.get("content", ""), 0) < REPEAT_COOLDOWN:
                continue
            if int(t.get("hits", 1)) >= 8:
                continue  # 已经聊了七八轮的梗，模型早知道了，再喂就是复读
            freshness = 1.0 - age / FRESH_WINDOW                 # 越新越靠前
            heat = 1.0 + 0.3 * min(int(t.get("hits", 1)) - 1, 3)  # 反复出现算热点，但别让它压过新鲜事
            candidates.append((heat * freshness, t))

        # 同簇只留分数最高的一条，其余当重复
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        picked: List[Dict[str, any]] = []
        for score, item in candidates:
            if score <= 0:
                continue
            if any(same_topic(item.get("content", ""), p.get("content", "")) for p in picked):
                continue
            picked.append(item)
            if len(picked) >= limit:
                break

        if not picked:
            return ""
        for item in picked:
            self._last_inject[item.get("content", "")] = now
        lines = [f"· 有群友在讨论: 「{item['content']}」" for item in picked]
        return "\n【隔壁群近期热聊话题（只是背景，不是本群发生的事，别主动提起）】:\n" + "\n".join(lines) + "\n"

    async def _flush_loop(self):
        """后台异步刷盘协程，每 2 秒检测一次脏标记落盘"""
        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            if self.is_dirty:
                try:
                    data = list(self.topics)
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, self._write_disk, data)
                    self.is_dirty = False
                except Exception as e:
                    logger.debug("跨群记忆刷盘异常: %s", e)

    def _write_disk(self, data: List[dict]):
        tmp_path = self.file_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.file_path)

    def start_background_flusher(self):
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())

cross_group_mgr = CrossGroupTopicManager()
