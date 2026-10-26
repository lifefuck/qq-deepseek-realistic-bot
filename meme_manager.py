#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网络流行语与热梗轮换管理器 (MemeManager)
作者: life (lifefuck)

功能:
1. 加载 data/memes_pool.json 词库
2. 维护 ~100 个词的当前动态轮换池 (默认每 2 小时自动洗牌轮换一批)
3. 扫描群友发言，自动命中并优先置顶匹配到的热梗及释义
4. 生成紧凑、低 Token 占用的 Prompt 注入文本
"""

import json
import logging
import os
import random
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("meme_manager")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MEMES_FILE = os.path.join(SCRIPT_DIR, "data", "memes_pool.json")

class MemeManager:
    def __init__(self, pool_size: int = 100, rotation_interval: int = 7200):
        self.pool_size = pool_size
        self.rotation_interval = rotation_interval  # 默认 2 小时轮换一次 (秒)
        self.last_rotation = 0.0
        self.last_file_mtime = 0.0

        self.all_memes: List[Dict[str, str]] = []
        self.structured_memes: List[Dict[str, str]] = []  # 带解释的高质量梗
        self.word_only_memes: List[str] = []              # 纯热词
        self.current_pool: List[Dict[str, str]] = []      # 当前 100 个词轮换池
        
        self.load_pool()

    def load_pool(self, force: bool = False):
        """从 JSON 加载词库并初始化轮换池"""
        if not os.path.exists(MEMES_FILE):
            logger.warning("词库文件不存在: %s，等待同步脚本生成", MEMES_FILE)
            return

        try:
            mtime = os.path.getmtime(MEMES_FILE)
            if not force and mtime == self.last_file_mtime and self.all_memes:
                return

            with open(MEMES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.all_memes = data.get("memes", [])
            self.last_file_mtime = mtime

            # 分流：带解释的高质量梗 vs 纯流行词
            self.structured_memes = [m for m in self.all_memes if m.get("desc")]
            self.word_only_memes = [m["name"] for m in self.all_memes if not m.get("desc")]

            self.rotate_pool(force=True)
            logger.info("已成功载入热梗词库: 总词数 %d (带释义: %d, 纯热词: %d)，当前轮换池: %d 条",
                        len(self.all_memes), len(self.structured_memes), len(self.word_only_memes), len(self.current_pool))
        except Exception as e:
            logger.error("加载词库文件异常: %s", e)

    def rotate_pool(self, force: bool = False):
        """定期随机洗牌轮换一批 100 个词"""
        now = time.time()
        if not force and (now - self.last_rotation) < self.rotation_interval and self.current_pool:
            return

        # 优先使用带释义的高质量梗（CHIME 与 经典梗）
        pool_candidates = self.structured_memes if self.structured_memes else self.all_memes
        num_to_pick = min(self.pool_size, len(pool_candidates))
        selected = random.sample(pool_candidates, num_to_pick) if pool_candidates else []

        combined = []
        for item in selected:
            desc = item.get("desc", "").strip()
            # 压缩解释：取前 20 字，保留核心意义
            if len(desc) > 20:
                desc = desc[:18] + "..."
            combined.append({"name": item["name"], "desc": desc})

        self.current_pool = combined
        self.last_rotation = now
        logger.debug("动态热词轮换池已刷新，当前选取 %d 条热词/梗", len(self.current_pool))

    def match_user_message(self, message: str) -> List[Dict[str, str]]:
        """检测群友发言是否命中了词库中的特定梗词（优先检查带释义的梗）"""
        if not message or len(message.strip()) < 2:
            return []

        clean_msg = message.strip()
        matched = []
        # 先在全部带释义的优质梗里扫描（只匹配长度 >= 2 的词，避免单字误报）
        for item in self.structured_memes:
            name = item["name"]
            if len(name) >= 2 and name in clean_msg:
                matched.append(item)
                if len(matched) >= 3:  # 最多置顶 3 个命中梗
                    break
        return matched

    def get_prompt_snippet(self, user_message: str = "") -> str:
        """
        只返回本句命中的梗，没命中就返回空串。

        以前是把 100 条带释义的轮换池整包拼进 System Prompt，实测约 3000 字符
        （≈1500~2000 tokens），比整个 System Prompt 还长，而九成发言一个梗都不碰。
        轮换池现在只留在内存里当候选，永不进 Prompt。
        """
        self.load_pool()
        matched = self.match_user_message(user_message) if user_message else []
        if not matched:
            return ""

        parts = []
        for m in matched[:2]:
            desc = (m.get("desc") or "").strip()
            # 按句读截断，别把释义切成「不按常规策」这种半截话
            if len(desc) > 12:
                cut = min([i for i in (desc.find("，"), desc.find("。")) if i > 4] or [12])
                desc = desc[:cut]
            parts.append(f"{m['name']}({desc})" if desc else m["name"])
        return "【群友这句提到的梗】：" + "；".join(parts)


# 单例实例
meme_manager = MemeManager(pool_size=100, rotation_interval=7200)

if __name__ == "__main__":
    test_msg = "今天星期四，感觉好想吃炸鸡，天天加班整个人班味好重啊"
    print("=== 测试群友命中 ===")
    print(meme_manager.get_prompt_snippet(test_msg))
    print("\n=== 测试纯轮换 ===")
    print(meme_manager.get_prompt_snippet()[:300] + "...")
