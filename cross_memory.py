"""
跨群公共话题滚动记忆模块
1. 共享滚动话题事件总线：提取客观公共事件/热梗（不含私聊及群友个人好感），让多群聊天有连贯认知。
2. 零 I/O 阻塞：写内存 + 脏标记，后台独立线程/异步任务每 2 秒合并刷盘一次，杜绝热路径磁盘阻塞。
"""
import asyncio
import json
import logging
import os
import time
from collections import deque
from typing import Deque, Dict, List, Optional

logger = logging.getLogger("DeepSeekBot")

CROSS_GROUP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cross_group_topics.json")
MAX_TOPICS = 30
FLUSH_INTERVAL = 2.0  # 2秒异步合并刷盘

class CrossGroupTopicManager:
    def __init__(self, file_path: str = CROSS_GROUP_FILE):
        self.file_path = file_path
        self.topics: Deque[Dict[str, any]] = deque(maxlen=MAX_TOPICS)
        self.is_dirty = False
        self._flush_task: Optional[asyncio.Task] = None
        self._load()

    def _load(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for item in data:
                        if isinstance(item, dict) and "content" in item:
                            self.topics.append(item)
                logger.info("跨群公共话题记忆已加载: %d 条记录", len(self.topics))
            except Exception as e:
                logger.warning("加载跨群记忆异常: %s", e)

    def record_topic(self, group_id: int, sender_nick: str, content: str, topic_tag: Optional[str] = None):
        """记录一条公共话题（热路径仅写内存与置脏标记）"""
        # 过滤过短或无营养的水聊
        c = content.strip()
        if len(c) < 5 or c.startswith(("/", "#", "*", "%")):
            return

        self.topics.append({
            "ts": time.time(),
            "group_id": group_id,
            "sender": sender_nick,
            "tag": topic_tag,
            "content": c[:150]
        })
        self.is_dirty = True

    def get_recent_topics_prompt(self, exclude_group_id: Optional[int] = None, limit: int = 4) -> str:
        """取其他群的近期话题，给模型当背景，不直接当台词"""
        now = time.time()
        candidates = []
        for t in reversed(self.topics):
            # 排除当前群自身，只看跨群话题；且只看 3 小时内的热点
            if exclude_group_id and t.get("group_id") == exclude_group_id:
                continue
            if now - t.get("ts", 0) > 10800:
                continue
            candidates.append(f"· 有群友在讨论: 「{t['content']}」")
            if len(candidates) >= limit:
                break
        
        if not candidates:
            return ""
        return "\n【隔壁群近期热聊话题（只是背景，不是本群发生的事，别主动提起）】:\n" + "\n".join(candidates) + "\n"

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
