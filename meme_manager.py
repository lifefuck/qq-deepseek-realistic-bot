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
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger("meme_manager")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MEMES_FILE = os.path.join(SCRIPT_DIR, "data", "memes_pool.json")

class MemeManager:
    def __init__(self, pool_size: int = 100, rotation_interval: int = 1800):
        self.pool_size = pool_size
        self.rotation_interval = rotation_interval  # 默认 30 分钟轮换一次 (秒)
        self.last_rotation = 0.0
        self.last_file_mtime = 0.0

        self.all_memes: List[Dict[str, Any]] = []
        self.structured_memes: List[Dict[str, Any]] = []  # 带解释的高质量梗
        self.word_only_memes: List[str] = []              # 纯热词
        self.current_pool: List[Dict[str, Any]] = []      # 当前 100 个词轮换池
        
        # 分类索引池（用于按圈子配比）
        self.category_pools: Dict[str, List[Dict[str, Any]]] = {
            "game": [],      # 游戏电竞（无畏契约、三角洲、王者、LOL等）
            "acg": [],       # 二次元与B站二创
            "abstract": [],  # 贴吧、抽象文化与口头禅
            "general": []    # 通用网络流行语
        }
        
        # 别名/同义词映射表: alias -> meme item
        self.alias_map: Dict[str, Dict[str, Any]] = {}
        
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

            # 重置分类与别名字典
            for k in self.category_pools:
                self.category_pools[k] = []
            self.alias_map = {}

            # 分流：带解释的高质量梗 vs 纯流行词
            self.structured_memes = [m for m in self.all_memes if m.get("desc")]
            self.word_only_memes = [m["name"] for m in self.all_memes if not m.get("desc")]

            # 构建别名索引与圈子分类
            for m in self.structured_memes:
                name = m["name"].lower()
                self.alias_map[name] = m
                for al in m.get("aliases", []):
                    if al:
                        self.alias_map[str(al).lower()] = m

                # 自动圈子归类
                desc = m.get("desc", "")
                cat = m.get("category")
                if not cat:
                    if any(g in name or g in desc for g in ("瓦", "三角洲", "原神", "王者", "lol", "英雄联盟", "cs", "游戏", "开黑", "死斗", "老六", "堵桥", "巡飞弹", "奶妈")):
                        cat = "game"
                    elif any(a in name or a in desc for a in ("b站", "二创", "番剧", "动漫", "初音", "cos", "阿宅", "二次元")):
                        cat = "acg"
                    elif any(b in name or b in desc for b in ("贴吧", "抽象", "鼠鼠", "牢", "急了", "典", "乐", "蚌埠", "红温")):
                        cat = "abstract"
                    else:
                        cat = "general"
                m["category"] = cat
                if cat in self.category_pools:
                    self.category_pools[cat].append(m)
                else:
                    self.category_pools["general"].append(m)

            self.rotate_pool(force=True)
            logger.info("已成功载入热梗词库: 总词数 %d (带释义: %d, 纯热词: %d)，当前轮换池: %d 条",
                        len(self.all_memes), len(self.structured_memes), len(self.word_only_memes), len(self.current_pool))
        except Exception as e:
            logger.error("加载词库文件异常: %s", e)

    def rotate_pool(self, force: bool = False):
        """定期随机洗牌轮换一批 100 个词（按圈子配比：游戏40%、二创20%、抽象20%、通用20%）"""
        now = time.time()
        if not force and (now - self.last_rotation) < self.rotation_interval and self.current_pool:
            return

        # 圈子配比目标：game 40条, acg 20条, abstract 20条, general 20条
        targets = [
            ("game", 40),
            ("acg", 20),
            ("abstract", 20),
            ("general", 20)
        ]
        
        selected_memes: List[Dict[str, Any]] = []
        selected_names = set()
        
        for cat, count in targets:
            pool = self.category_pools.get(cat, [])
            candidates = [m for m in pool if m["name"] not in selected_names]
            picked = random.sample(candidates, min(count, len(candidates))) if candidates else []
            for item in picked:
                selected_names.add(item["name"])
                selected_memes.append(item)
                
        # 如果不足 100 条，用全部带释义的词补充
        if len(selected_memes) < self.pool_size and self.structured_memes:
            remain_needed = self.pool_size - len(selected_memes)
            remains = [m for m in self.structured_memes if m["name"] not in selected_names]
            picked = random.sample(remains, min(remain_needed, len(remains))) if remains else []
            selected_memes.extend(picked)

        combined = []
        for item in selected_memes:
            desc = item.get("desc", "").strip()
            # 压缩解释：取前 20 字，保留核心意义
            if len(desc) > 20:
                desc = desc[:18] + "..."
            combined.append({"name": item["name"], "desc": desc})

        self.current_pool = combined
        self.last_rotation = now
        logger.debug("动态热词轮换池已刷新，当前选取 %d 条热词/梗", len(self.current_pool))

    def match_user_message(self, message: str) -> List[Dict[str, Any]]:
        """检测群友发言是否命中了词库中的特定梗词或其别名/变体（优先置顶带释义梗）"""
        if not message or len(message.strip()) < 2:
            return []

        clean_msg = message.strip().lower()
        matched = []
        matched_keys = set()

        # 1. 优先从别名和变体索引精准检测
        for alias, item in self.alias_map.items():
            if len(alias) >= 2 and alias in clean_msg:
                m_name = item["name"]
                if m_name not in matched_keys:
                    matched_keys.add(m_name)
                    matched.append(item)
                    if len(matched) >= 3:
                        return matched

        # 2. 扫描其余全部带释义词库
        for item in self.structured_memes:
            name = item["name"].lower()
            if len(name) >= 2 and name in clean_msg:
                if item["name"] not in matched_keys:
                    matched_keys.add(item["name"])
                    matched.append(item)
                    if len(matched) >= 3:
                        break
        return matched

    def get_prompt_snippet(self, user_message: str = "") -> str:
        """
        生成用于注入 Prompt 的网络热梗参考文本
        包含：
        1. 群友发言精准命中的梗及释义（置顶）
        2. 当前 100 个词轮换池中的词汇（每 30 分钟轮换，提供网络冲浪语境底座）
        """
        self.load_pool()
        self.rotate_pool()

        if not self.current_pool and not self.structured_memes:
            return ""

        matched = self.match_user_message(user_message) if user_message else []
        matched_names = {m["name"] for m in matched}

        lines = [
            "【网络冲浪语境与热词参考（当前轮换池100条，自然融入、切忌生搬硬套或无脑堆砌）】:"
        ]

        if matched:
            match_parts = []
            for m in matched[:3]:
                desc = (m.get("desc") or "").strip()
                if len(desc) > 50:
                    cut = min([i for i in (desc.find("。"), desc.find("；")) if i > 15] or [50])
                    desc = desc[:cut]
                match_parts.append(f"{m['name']}（{desc}）" if desc else m['name'])
            lines.append(f"- 群友提到/上下文相关梗（顺着梗玩或精准反弹）: {'; '.join(match_parts)}")

        # 提取当前 100 个词（去重已命中的）
        pool_words = []
        for item in self.current_pool:
            name = item["name"]
            if name in matched_names:
                continue
            desc = item.get("desc")
            if desc:
                pool_words.append(f"{name}({desc})")
            else:
                pool_words.append(name)

        # 拼接成紧凑的词表展示
        lines.append(f"- 常用流行词汇池: {'、'.join(pool_words[:self.pool_size])}")
        return "\n".join(lines)


# 单例实例
meme_manager = MemeManager(pool_size=100, rotation_interval=1800)

if __name__ == "__main__":
    test_msg = "今天星期四，感觉好想吃炸鸡，天天加班整个人班味好重啊"
    print("=== 测试群友命中 ===")
    print(meme_manager.get_prompt_snippet(test_msg))
    print("\n=== 测试纯轮换 ===")
    print(meme_manager.get_prompt_snippet()[:300] + "...")
