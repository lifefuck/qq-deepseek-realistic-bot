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
import re
import random
import time
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger("meme_manager")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MEMES_FILE = os.path.join(SCRIPT_DIR, "data", "memes_pool.json")
CORE_FILE = os.path.join(SCRIPT_DIR, "data", "memes_core.json")

class MemeManager:
    def __init__(self, pool_size: int = 30, rotation_interval: int = 1800):
        self.pool_size = pool_size
        self.rotation_interval = rotation_interval  # 默认 30 分钟轮换一次 (秒)
        self.last_rotation = 0.0
        self.last_file_mtime = 0.0

        self.core_memes: List[Dict[str, Any]] = []        # 常驻核心梗：每次都注入，不轮换
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

            # 核心梗单独一个文件：自动同步会重写 memes_pool.json，手写内容放那儿会被冲掉
            try:
                with open(CORE_FILE, "r", encoding="utf-8") as cf:
                    self.core_memes = json.load(cf).get("core", [])
            except FileNotFoundError:
                self.core_memes = []
            except Exception as ce:
                logger.warning("核心梗加载失败: %s", ce)

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

        # 圈子配比目标：跟着 pool_size 走（默认 30 条：游戏 12 / 二创 6 / 抽象 6 / 通用 6）
        targets = [
            ("game", 12),
            ("acg", 6),
            ("abstract", 6),
            ("general", 6)
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
                
        # 不足 pool_size 就用其余带释义的词补齐
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

    def match_user_message(self, text: str, limit: int = 3) -> List[Dict[str, Any]]:
        """从这条消息里认出梗：先核心梗，再全库别名，最后才对核心梗做容错召回。

        顺序很重要：核心梗是手写的、带接法示范的，命中就该优先占位；
        容错召回只对核心梗做，全库 1900 条参与模糊匹配会把「牛头人」当成「牢明」。
        """
        clean = re.sub(r"\[[^\]]*\]", " ", text or "").lower()
        matched: List[Dict[str, Any]] = []
        seen = set()

        def add(item: Dict[str, Any]) -> None:
            if item.get("name") and item["name"] not in seen and len(matched) < limit:
                seen.add(item["name"])
                matched.append(item)

        # 1. 名字/别名子串命中。核心梗排前面，同层里长名字优先（「疯狂星期四」先于「星期四」）
        pool = [(0, m) for m in self.core_memes] + [(1, m) for m in self.structured_memes]
        pool.sort(key=lambda x: (x[0], -len(x[1].get("name", ""))))
        for _, item in pool:
            names = [item.get("name", "")] + list(item.get("aliases") or [])
            if any(len(n) >= 2 and n.lower() in clean for n in names):
                add(item)

        # 2. 一个都没认出来，才做容错召回：群里常打错字、漏字（卡啦彼丘），精确子串永远命中不了。
        #    只认三字以上的核心梗，且重合度要够高，否则「又送人头了」会把一堆词召回来。
        if not matched:
            msg_chars = set(clean)
            scored = []
            for item in self.core_memes:
                name = item.get("name", "")
                if len(name) < 3:
                    continue
                chars = set(name)
                shared = len(chars & msg_chars)
                if shared >= 2 and shared / len(chars) >= 0.75:
                    scored.append((shared / len(chars), item))
            for _, item in sorted(scored, key=lambda x: -x[0]):
                add(item)
        return matched

    @staticmethod
    def _short_desc(item: Dict[str, Any], limit: int = 26) -> str:
        desc = (item.get("desc") or "").strip()
        if len(desc) <= limit:
            return desc
        cuts = [i for i in (desc.find("。"), desc.find("；"), desc.find("，")) if 8 <= i <= limit]
        return desc[:min(cuts)] if cuts else desc[:limit]

    def get_prompt_snippet(self, user_message: str = "") -> str:
        """三层注入：常驻核心梗 + 这句沾边的梗 + 一小撮流行词。

        以前是「命中的梗 + 100 条随机词表」，1910 条库里随机抽 100 条，命中率低、
        还占掉一大截注意力。现在常驻的少而准，剩下的靠检索，检索不到就别硬接。
        """
        self.load_pool()
        self.rotate_pool()
        if not self.core_memes and not self.current_pool and not self.structured_memes:
            return ""

        lines: List[str] = []
        if self.core_memes:
            parts = []
            for m in self.core_memes[:14]:
                cue = m.get("cue") or self._short_desc(m)
                parts.append(f"{m['name']}→{cue}" if cue else m["name"])
            lines.append("【群里常聊的梗（照这个口吻接，别背解释）】：" + "｜".join(parts))

        matched = self.match_user_message(user_message) if user_message else []
        core_names = {m["name"] for m in self.core_memes}
        hits = [m for m in matched if m["name"] not in core_names][:3]
        if hits:
            lines.append("【这句沾边的梗】：" + "；".join(
                f"{m['name']}（{self._short_desc(m)}）" for m in hits))

        if self.current_pool:
            words = [
                i["name"] for i in self.current_pool
                if i["name"] not in core_names and re.fullmatch(r"[\u4e00-\u9fa5A-Za-z0-9]{2,8}", i["name"] or "")
            ][:30]
            if words:
                lines.append("【最近常刷到的词】：" + "、".join(words))
        return "\n".join(lines)


# 单例实例
meme_manager = MemeManager(pool_size=30, rotation_interval=1800)

if __name__ == "__main__":
    test_msg = "今天星期四，感觉好想吃炸鸡，天天加班整个人班味好重啊"
    print("=== 测试群友命中 ===")
    print(meme_manager.get_prompt_snippet(test_msg))
    print("\n=== 测试纯轮换 ===")
    print(meme_manager.get_prompt_snippet()[:300] + "...")
