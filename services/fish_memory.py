# -*- coding: utf-8 -*-
"""fish_memory —— 借鉴自 DSH (zhugeweimu/DSH-a-complete-life-for-every-fish) 的持久化记忆与检索系统。
适配 QQ 群聊/私聊多群多用户隔离架构：
1. SQLite 本地存储：记录（带 group_id / person_id 隔离）、标签、流水账
2. 自动规则感知（autocapture）：7 类事实/偏好/事件免显式唤醒捕获
3. 中文二元分词 + BM25 字面检索 + 时间衰减召回（hybrid）
4. 会话分段沉淀（turnlog -> event）
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(HERE, "..", "..", "data", "fish_memory.db")

CJK = re.compile(r"[\u4e00-\u9fff]")
NEG = re.compile(r"[不没别未无]")

# 7 大记忆类型与捕获规则
RULES = [
    ("fact", r"我(今年|现在)?\s*\d+\s*岁|我(叫|名字是)|我(在|于).{1,12}(工作|上班|上学|读书)|我住在?.{1,12}"),
    ("preference", r"我(很|最|特别|超)?(喜欢|爱|讨厌|不喜欢|受不了|爱吃|不吃|过敏)"),
    ("plan", r"(明天|后天|下周|下个月|下周末|以后|回头|打算|准备|计划|约好|要去|得去|想去)"),
    ("promise", r"(我会|我答应|一定|保证|说好了|绝不)"),
    ("relationship", r"(我和|跟他|跟她|和朋友|和同事|和家人).{0,20}(吵架|和好|分手|认识|关系|见面)"),
    ("event", r"(今天|昨天|刚才|刚刚|早上|中午|下午|晚上|周末|前几天).{0,30}(了|过|去|见|吃|买|聊|玩|睡)"),
    ("note", r"(记住|记一下|记着|别忘了|别忘记|帮我记)"),
]

SKIP_EXACT = {
    "你好", "在吗", "在么", "晚安", "早安", "哈哈", "嗯", "哦", "好的", "谢谢", "拜拜",
    "没事", "算了", "好的呢", "行", "可以", "收到", "6", "666", "1", "打卡"
}

TYPE_IMPORTANCE = {
    "fact": 0.85, "preference": 0.75, "plan": 0.7, "promise": 0.8,
    "relationship": 0.8, "event": 0.55, "note": 0.85, "plain": 0.35
}

TYPE_TAG = {
    "fact": "事实", "preference": "偏好", "plan": "计划", "promise": "承诺",
    "relationship": "关系", "event": "事件", "note": "指定记录", "plain": "流水"
}

DDL = """
CREATE TABLE IF NOT EXISTS fish_memory_records (
    memory_id          TEXT PRIMARY KEY,
    group_id           TEXT NOT NULL DEFAULT '',
    person_id          TEXT NOT NULL DEFAULT '',
    title              TEXT NOT NULL DEFAULT '',
    content            TEXT NOT NULL,
    memory_type        TEXT NOT NULL DEFAULT 'event',
    importance         REAL NOT NULL DEFAULT 0.5,
    created_at         REAL NOT NULL DEFAULT 0,
    updated_at         REAL NOT NULL DEFAULT 0,
    last_activated_at  REAL NOT NULL DEFAULT 0,
    activation_count   INTEGER NOT NULL DEFAULT 0,
    is_deleted         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_fish_mem_group_person ON fish_memory_records (group_id, person_id);
CREATE INDEX IF NOT EXISTS idx_fish_mem_type         ON fish_memory_records (memory_type);
CREATE INDEX IF NOT EXISTS idx_fish_mem_created      ON fish_memory_records (created_at);

CREATE TABLE IF NOT EXISTS fish_memory_tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id  TEXT NOT NULL,
    tag_value  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fish_tag_memory ON fish_memory_tags (memory_id);

CREATE TABLE IF NOT EXISTS fish_turn_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   TEXT NOT NULL DEFAULT '',
    person_id  TEXT NOT NULL DEFAULT '',
    ts         REAL NOT NULL,
    user_text  TEXT NOT NULL DEFAULT '',
    reply_text TEXT NOT NULL DEFAULT '',
    closed     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fish_log_lookup ON fish_turn_logs (group_id, person_id, closed);
"""


class FishMemoryEngine:
    """本地 SQLite + BM25 + 时间衰减的记忆引擎（多群/私聊隔离）"""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.executescript(DDL)
            conn.commit()

    # ---------------- 分词与特征提取 ----------------
    @staticmethod
    def tokens(text: str) -> List[str]:
        """中文二元分词 + 英文/数字连续串"""
        text = (text or "").lower()
        out = re.findall(r"[a-z0-9_]{2,}", text)
        cjk = "".join(ch if CJK.match(ch) else " " for ch in text)
        for seg in cjk.split():
            if len(seg) == 1:
                out.append(seg)
            else:
                out.extend([seg[i:i + 2] for i in range(len(seg) - 1)])
        return out

    @staticmethod
    def jaccard(a: str, b: str) -> float:
        set_a = set(FishMemoryEngine.tokens(a))
        set_b = set(FishMemoryEngine.tokens(b))
        if not set_a or not set_b:
            return 0.0
        return len(set_a & set_b) / max(1, len(set_a | set_b))

    @staticmethod
    def fingerprint(group_id: str, person_id: str, content: str, memory_type: str = "") -> str:
        raw = f"{group_id}|{person_id}|{content.strip()}|{memory_type}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    # ---------------- 自动捕获（Autocapture） ----------------
    @staticmethod
    def detect_types(text: str) -> List[str]:
        hits = []
        s = text or ""
        for kind, pat in RULES:
            m = re.search(pat, s)
            if not m:
                continue
            # 否定词前缀过滤（如「不是我」）
            prefix = s[max(0, m.start() - 4):m.start()]
            if NEG.search(prefix):
                continue
            hits.append(kind)
        return hits

    @staticmethod
    def should_skip(text: str) -> bool:
        t = (text or "").strip()
        if not t or t in SKIP_EXACT or len(t) < 4:
            return True
        # 纯问句不记
        if len(t) <= 12 and (t.endswith("吗") or t.endswith("呢") or t.endswith("?") or t.endswith("？")):
            return True
        return False

    # ---------------- 写入与去重合并 ----------------
    def add_memory(
        self,
        group_id: str,
        person_id: str,
        content: str,
        title: str = "",
        memory_type: str = "event",
        importance: Optional[float] = None,
        tags: Optional[List[str]] = None,
        ts: Optional[float] = None
    ) -> Dict:
        """写入单条记忆，精确指纹或语义近重（Jaccard > 0.75）则合并"""
        content = (content or "").strip()
        if not content:
            return {"merged": False, "skipped": True}

        title = (title or content[:20]).strip()
        now_ts = float(ts if ts is not None else time.time())
        imp = importance if importance is not None else TYPE_IMPORTANCE.get(memory_type, 0.5)

        with self._get_conn() as conn:
            # 1. 精确指纹排重
            fp = self.fingerprint(group_id, person_id, content, memory_type)
            row = conn.execute(
                "SELECT memory_id, content, activation_count FROM fish_memory_records "
                "WHERE group_id=? AND person_id=? AND is_deleted=0 AND memory_id=?",
                (group_id, person_id, fp)
            ).fetchone()

            if row:
                conn.execute(
                    "UPDATE fish_memory_records SET activation_count=activation_count+1, "
                    "last_activated_at=?, updated_at=? WHERE memory_id=?",
                    (now_ts, now_ts, row["memory_id"])
                )
                conn.commit()
                return {"id": row["memory_id"], "merged": True, "score": 1.0}

            # 2. 局部近重搜索
            candidates = conn.execute(
                "SELECT memory_id, title, content, activation_count FROM fish_memory_records "
                "WHERE group_id=? AND person_id=? AND memory_type=? AND is_deleted=0 "
                "ORDER BY created_at DESC LIMIT 50",
                (group_id, person_id, memory_type)
            ).fetchall()

            for cand in candidates:
                sim = self.jaccard(content, cand["content"])
                if sim >= 0.75:
                    new_content = content if len(content) > len(cand["content"]) else cand["content"]
                    conn.execute(
                        "UPDATE fish_memory_records SET content=?, activation_count=activation_count+1, "
                        "last_activated_at=?, updated_at=? WHERE memory_id=?",
                        (new_content, now_ts, now_ts, cand["memory_id"])
                    )
                    conn.commit()
                    return {"id": cand["memory_id"], "merged": True, "score": round(sim, 2)}

            # 3. 插入新记录
            conn.execute(
                "INSERT INTO fish_memory_records (memory_id, group_id, person_id, title, content, "
                "memory_type, importance, created_at, updated_at, last_activated_at, activation_count, is_deleted) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)",
                (fp, group_id, person_id, title, content, memory_type, imp, now_ts, now_ts, now_ts)
            )
            for tag in (tags or [TYPE_TAG.get(memory_type, "记忆")]):
                conn.execute(
                    "INSERT INTO fish_memory_tags (memory_id, tag_value) VALUES (?, ?)",
                    (fp, tag)
                )
            conn.commit()
            return {"id": fp, "merged": False, "score": 0.0}

    # ---------------- BM25 检索与时间衰减召回 ----------------
    def recall(
        self,
        group_id: str,
        person_id: str,
        query: str,
        top_k: int = 2,
        half_life_days: float = 14.0
    ) -> List[Dict]:
        """根据当前发言检索该群友最相关的历史记忆条目。
        融合分数 = BM25字面分 * 重要度 * 时间半衰期衰减
        """
        q_tokens = self.tokens(query)
        if not q_tokens:
            return []

        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT memory_id, title, content, memory_type, importance, created_at, "
                "last_activated_at, activation_count FROM fish_memory_records "
                "WHERE group_id=? AND person_id=? AND is_deleted=0",
                (group_id, person_id)
            ).fetchall()

        if not rows:
            return []

        now_ts = time.time()
        docs = [dict(r) for r in rows]
        n_docs = len(docs)
        avgdl = sum(len(self.tokens(d["content"])) for d in docs) / max(1, n_docs)

        # 统计 IDF
        df = {}
        for d in docs:
            dtoks = set(self.tokens(d["content"]))
            for t in set(q_tokens):
                if t in dtoks:
                    df[t] = df.get(t, 0) + 1

        k1, b = 1.2, 0.75
        results = []

        for d in docs:
            dtoks = self.tokens(d["content"])
            doc_len = len(dtoks)
            bm25 = 0.0
            for t in q_tokens:
                if t not in df:
                    continue
                idf = math.log((n_docs - df[t] + 0.5) / (df[t] + 0.5) + 1.0)
                tf = dtoks.count(t)
                denom = tf + k1 * (1.0 - b + b * (doc_len / max(1, avgdl)))
                bm25 += idf * (tf * (k1 + 1.0)) / max(0.001, denom)

            if bm25 <= 0.1:
                continue

            # 半衰期衰减因子: 越久远没激活的分数越低（但 fact/preference 衰减很慢）
            age_days = (now_ts - d.get("last_activated_at", d["created_at"])) / 86400.0
            effective_half_life = half_life_days * (2.0 if d["memory_type"] in ("fact", "preference") else 1.0)
            decay = math.exp(-0.693 * max(0.0, age_days) / max(1.0, effective_half_life))

            score = bm25 * d["importance"] * decay
            results.append({
                "id": d["memory_id"],
                "content": d["content"],
                "type": d["memory_type"],
                "score": score
            })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    # ---------------- 流水账记录与自动捕获 ----------------
    def record_turn(self, group_id: str, person_id: str, user_text: str, reply_text: str = ""):
        """记录回合流水账，并自动探测是否包含值得捕获的 7 类记忆"""
        clean_user = (user_text or "").strip()
        if not clean_user:
            return

        now_ts = time.time()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO fish_turn_logs (group_id, person_id, ts, user_text, reply_text, closed) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (group_id, person_id, now_ts, clean_user, reply_text or "")
            )
            conn.commit()

        # 规则自动捕获
        if not self.should_skip(clean_user):
            detected = self.detect_types(clean_user)
            for m_type in detected:
                self.add_memory(
                    group_id=group_id,
                    person_id=person_id,
                    content=clean_user,
                    memory_type=m_type,
                    importance=TYPE_IMPORTANCE.get(m_type, 0.7),
                    ts=now_ts
                )


# 单例实例
fish_memory_engine = FishMemoryEngine()
