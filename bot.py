#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQ 群聊 & 私聊 DeepSeek 拟人化智能助手 - 蓝色小鲸鱼娘（大肥鱼/鲸宝）版本
基于 OneBot 11 (NapCat) 正向 WebSocket 与 SiliconFlow DeepSeek-V3
特性:
1. 支持多群隔离、主动插话、被@秒回与管理员私聊直通
2. 完整识别 QQ 引用回复（获取原消息内容拼接）
3. 真实独立性格：允许且鼓励对事实错误/观点相左进行理性反驳，拒绝应声虫
4. 管理员快捷指令: /new (重置上下文), /stop (一键刹车中止思考) 且均有执行汇报
作者: life
"""

import asyncio
import collections
import json
import logging
import os
import random
import base64
import re
import time
import datetime
import uuid
from typing import Any, Deque, Dict, Optional, Set, Tuple
import aiohttp
import web_dashboard
import games
import connectors
import cross_memory
import media_resolver
from meme_manager import meme_manager
from prompts import (SYSTEM_PROMPT, ROOT_SYSTEM_PROMPT, GAMES_KB, needs_games_kb,
                     _POKE_QUIPS, _POKE_ADMIN_QUIPS)
import meme_catalog
import safety_guard
import cross_interaction
import vision_resolver

# ==================== 基础配置 ====================
# 连接信息全部走环境变量，仓库里只留占位符
NAPCAT_WS_URL = os.environ.get("NAPCAT_WS_URL", "ws://127.0.0.1:3001")
BOT_QQ = int(os.environ.get("BOT_QQ", "1000000000"))
ADMIN_QQ = int(os.environ.get("ADMIN_QQ", "1000000001"))   # 最高超级管理员

ALLOWED_GROUPS: Set[int] = {
    int(x) for x in os.environ.get("WHITELIST_GROUPS", "123456789,987654321").replace("，", ",").split(",")
    if x.strip().isdigit()
}

SILICONFLOW_API_URL = os.environ.get("DEEPSEEK_API_URL", "https://api.siliconflow.cn/v1/chat/completions")
# 密钥只从环境变量读，仓库里只有占位符；本地放 ~/.config/qq-deepseek-bot.env，由 systemd 注入
SILICONFLOW_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "sk-your-key-here")
MODEL_NAME = os.environ.get("DEEPSEEK_MODEL", "deepseek-ai/DeepSeek-V3")

# 行为与节奏参数
TIMELINE_MAX_LEN = 8            # 群时间线保留最近消息条数
PRIVATE_TIMELINE_MAX_LEN = 10   # 管理员私聊时间线保留条数
INACTIVITY_TIMEOUT = 300.0      # 超过 5 分钟未说话重置时间线
DEBOUNCE_DELAY = 2.0            # 主动插话防抖 2.0 秒
ACTIVE_INTERRUPT_CD = 45.0      # 主动插话冷却 45 秒
DIRECT_AT_CD = 2.5              # 被 @ 时的用户级基础冷却（秒）
FOLLOW_UP_WINDOW = 300.0        # 持续对话追问窗口为 5 分钟 (300 秒)
FOLLOW_UP_MAX_COUNT = 5         # 追问窗口内最多免 @ 回答 5 次，有新的 @ 则重置配额
MAX_OUTPUT_TOKENS = 200         # 限制单次生成长度，防长篇大论

# 拟真作息睡眠参数 (01:00 ~ 07:00 CST 为入睡期)
SLEEP_START_HOUR = 1
SLEEP_END_HOUR = 7
sleep_override_until: float = 0.0          # 主人专属叫醒生效截止时间戳
user_sleep_reply_cd: Dict[tuple, float] = collections.defaultdict(float)  # 限制半夜对单个群友每 3 分钟最多回一句梦话

# 疲劳度衰减参数 (防群友连续狂 @ 调戏)
user_mention_burst: Dict[tuple, Deque[float]] = collections.defaultdict(lambda: collections.deque(maxlen=10))
user_fatigue_until: Dict[tuple, float] = collections.defaultdict(float)

# 群聊冷热场时间戳追踪 (活跃期提权 + 冷场降权)
group_last_msg_time: Dict[int, float] = collections.defaultdict(float)        # 群内上一条消息时间
group_last_bot_reply_time: Dict[int, float] = collections.defaultdict(float)  # 机器人在该群上次说话时间

# 反模板套路削平队列 (记录最近发出的 6 条回复)
recent_bot_replies: Deque[str] = collections.defaultdict(lambda: collections.deque(maxlen=6))[0]

BOT_KEYWORDS = {
    "大肥鱼", "肥鱼", "小鲸鱼", "鲸鱼", "鲸宝", "deepseek", "v3", "r1", 
    "bot", "机器人", "ai", "人工智障", "人工智能", "什么生物", "回消息", "理人", "说话",
    "笨蛋", "傻子", "傻鱼", "小鱼", "死鱼", "咸鱼"
}

# 日志格式
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("DeepSeekBot")
web_dashboard.setup_log_capture(logger)
BOT_START_TIME = time.time()

# ==================== Token 消耗与 DeepSeek 峰谷计费追踪 ====================
TOKEN_USAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token_usage.json")

# DeepSeek 全套模型官方与平台峰谷计费单价表 (元 / 1M tokens)
# 谷时 (Off-Peak): 周末全天；工作日 00:00-09:00, 12:00-14:00, 18:00-24:00
# 峰时 (Peak): 工作日 09:00-12:00, 14:00-18:00
DEEPSEEK_MODELS_PRICING: Dict[str, Dict[str, Any]] = {
    "deepseek-ai/DeepSeek-V3": {
        "name": "DeepSeek-V3",
        "provider": "SiliconFlow / 官方",
        "offpeak": {"input": 1.0, "output": 2.0},
        "peak": {"input": 2.0, "output": 8.0}
    },
    "deepseek-ai/DeepSeek-R1": {
        "name": "DeepSeek-R1 (推理)",
        "provider": "SiliconFlow / 官方",
        "offpeak": {"input": 4.0, "output": 16.0},
        "peak": {"input": 8.0, "output": 32.0}
    },
    "deepseek-chat": {
        "name": "DeepSeek-V3 (官方直连)",
        "provider": "DeepSeek 官方",
        "offpeak": {"input": 1.0, "output": 2.0},
        "peak": {"input": 2.0, "output": 8.0}
    },
    "deepseek-reasoner": {
        "name": "DeepSeek-R1 (官方直连)",
        "provider": "DeepSeek 官方",
        "offpeak": {"input": 4.0, "output": 16.0},
        "peak": {"input": 8.0, "output": 32.0}
    },
    "deepseek-v4-flash": {
        "name": "DeepSeek-V4-Flash",
        "provider": "DeepSeek 官方",
        "offpeak": {"input": 1.0, "output": 4.0},
        "peak": {"input": 2.0, "output": 8.0}
    },
    "deepseek-v4-pro": {
        "name": "DeepSeek-V4-Pro",
        "provider": "DeepSeek 官方",
        "offpeak": {"input": 4.5, "output": 13.5},
        "peak": {"input": 9.0, "output": 27.0}
    },
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B": {
        "name": "R1-Distill-Qwen-32B",
        "provider": "SiliconFlow",
        "offpeak": {"input": 1.26, "output": 1.26},
        "peak": {"input": 1.26, "output": 1.26}
    },
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B": {
        "name": "R1-Distill-Qwen-7B (免费)",
        "provider": "SiliconFlow",
        "offpeak": {"input": 0.0, "output": 0.0},
        "peak": {"input": 0.0, "output": 0.0}
    }
}

class TokenTracker:
    """Token 消耗与峰谷金额换算管理器，支持按模型、时段与群粒度持久化统计"""
    def __init__(self, file_path: str = TOKEN_USAGE_FILE):
        self.file_path = file_path
        self.data: Dict[str, Any] = self._load()

    def _default_data(self) -> Dict[str, Any]:
        cst_tz = datetime.timezone(datetime.timedelta(hours=8))
        today_str = datetime.datetime.now(cst_tz).strftime("%Y-%m-%d")
        return {
            "total": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "requests": 0,
                "cost_cny": 0.0
            },
            "periods": {
                "peak": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_cny": 0.0},
                "offpeak": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost_cny": 0.0}
            },
            "today": {
                "date": today_str,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "requests": 0,
                "cost_cny": 0.0
            },
            "by_model": {},
            "by_group": {}
        }

    def _load(self) -> Dict[str, Any]:
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                    default = self._default_data()
                    for k, v in default.items():
                        if k not in d:
                            d[k] = v
                    return d
            except Exception as e:
                logger.error("加载 Token 消耗记录异常: %s", e)
        return self._default_data()

    def save(self):
        try:
            tmp = self.file_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.file_path)
        except Exception as e:
            logger.error("保存 Token 消耗记录失败: %s", e)

    @staticmethod
    def get_period(dt: Optional[datetime.datetime] = None) -> str:
        """判定指定时间是否为 DeepSeek 高峰或低谷时段 (按北京时间 CST)"""
        if dt is None:
            cst_tz = datetime.timezone(datetime.timedelta(hours=8))
            dt = datetime.datetime.now(cst_tz)
        if dt.weekday() >= 5:
            return "offpeak"
        total_mins = dt.hour * 60 + dt.minute
        if (540 <= total_mins < 720) or (840 <= total_mins < 1080):
            return "peak"
        return "offpeak"

    def record_usage(self, usage: dict, model_name: str, group_id: Optional[int] = None):
        """记录单次 API 调用的 Token 消耗并按当前时段精确计费"""
        if not usage or not isinstance(usage, dict):
            return

        p_tokens = int(usage.get("prompt_tokens") or 0)
        c_tokens = int(usage.get("completion_tokens") or 0)
        t_tokens = int(usage.get("total_tokens") or (p_tokens + c_tokens))
        if t_tokens <= 0:
            return

        cst_tz = datetime.timezone(datetime.timedelta(hours=8))
        now_dt = datetime.datetime.now(cst_tz)
        today_str = now_dt.strftime("%Y-%m-%d")
        period = self.get_period(now_dt)

        pricing = DEEPSEEK_MODELS_PRICING.get(model_name) or DEEPSEEK_MODELS_PRICING["deepseek-ai/DeepSeek-V3"]
        rates = pricing[period]
        cost = (p_tokens / 1_000_000.0) * rates["input"] + (c_tokens / 1_000_000.0) * rates["output"]

        tot = self.data["total"]
        tot["prompt_tokens"] += p_tokens
        tot["completion_tokens"] += c_tokens
        tot["total_tokens"] += t_tokens
        tot["requests"] += 1
        tot["cost_cny"] = round(tot["cost_cny"] + cost, 6)

        per = self.data["periods"][period]
        per["prompt_tokens"] += p_tokens
        per["completion_tokens"] += c_tokens
        per["total_tokens"] += t_tokens
        per["cost_cny"] = round(per["cost_cny"] + cost, 6)

        today_data = self.data["today"]
        if today_data.get("date") != today_str:
            self.data["today"] = {
                "date": today_str,
                "prompt_tokens": p_tokens,
                "completion_tokens": c_tokens,
                "total_tokens": t_tokens,
                "requests": 1,
                "cost_cny": round(cost, 6)
            }
        else:
            today_data["prompt_tokens"] += p_tokens
            today_data["completion_tokens"] += c_tokens
            today_data["total_tokens"] += t_tokens
            today_data["requests"] += 1
            today_data["cost_cny"] = round(today_data["cost_cny"] + cost, 6)

        if model_name not in self.data["by_model"]:
            self.data["by_model"][model_name] = {
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "requests": 0, "cost_cny": 0.0
            }
        m_data = self.data["by_model"][model_name]
        m_data["prompt_tokens"] += p_tokens
        m_data["completion_tokens"] += c_tokens
        m_data["total_tokens"] += t_tokens
        m_data["requests"] += 1
        m_data["cost_cny"] = round(m_data["cost_cny"] + cost, 6)

        gid_key = str(group_id) if group_id else "private"
        if gid_key not in self.data["by_group"]:
            self.data["by_group"][gid_key] = {"total_tokens": 0, "cost_cny": 0.0, "requests": 0}
        g_data = self.data["by_group"][gid_key]
        g_data["total_tokens"] += t_tokens
        g_data["requests"] += 1
        g_data["cost_cny"] = round(g_data["cost_cny"] + cost, 6)

        self.save()

    def get_status_summary(self) -> Dict[str, Any]:
        """导出提供给 Web 仪表盘的实时状态"""
        cst_tz = datetime.timezone(datetime.timedelta(hours=8))
        now_dt = datetime.datetime.now(cst_tz)
        today_str = now_dt.strftime("%Y-%m-%d")
        if self.data["today"].get("date") != today_str:
            self.data["today"] = {
                "date": today_str,
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "requests": 0, "cost_cny": 0.0
            }

        current_period = self.get_period(now_dt)
        return {
            "total": self.data["total"],
            "periods": self.data["periods"],
            "today": self.data["today"],
            "by_model": self.data["by_model"],
            "by_group": self.data["by_group"],
            "current_period": current_period,
            "pricing_table": DEEPSEEK_MODELS_PRICING
        }

token_tracker = TokenTracker()

# ==================== 群友持久化记忆与好感度 (按群隔离) ====================
MEMORY_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_memories.json")
# 数据结构: str(group_id) -> { str(user_id) -> {"nickname": str, "favor": int, "traits": str, "notes": list[str], "updated_at": float} }
# 私聊的 group_id 为 "private"
user_memories: Dict[str, Dict[str, dict]] = {}

def load_user_memories():
    """从磁盘加载按群隔离的群友记忆库"""
    global user_memories
    if os.path.exists(MEMORY_FILE_PATH):
        try:
            with open(MEMORY_FILE_PATH, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
                # 兼容旧版本平铺结构，如果是平铺则自动迁移或初始化
                if raw_data and not any(isinstance(v, dict) and any(isinstance(sub_v, dict) for sub_v in v.values()) for v in raw_data.values() if isinstance(v, dict)):
                    user_memories = {"default": raw_data}
                else:
                    user_memories = raw_data
            total_users = sum(len(users) for users in user_memories.values() if isinstance(users, dict))
            logger.info("已成功加载按群隔离记忆库，共 %d 个分区，%d 位群友档案！", len(user_memories), total_users)
        except Exception as e:
            logger.error("加载群友记忆库失败: %s", e)
            user_memories = {}
    else:
        user_memories = {}

def save_user_memories():
    """将群友记忆库安全写回磁盘"""
    try:
        tmp_path = MEMORY_FILE_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(user_memories, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, MEMORY_FILE_PATH)
    except Exception as e:
        logger.error("保存群友记忆库失败: %s", e)

def get_or_create_user_memory(user_id: int, nickname: str = "", group_id: Optional[int] = None) -> dict:
    """获取或初始化群友记忆（按群隔离）"""
    gid_key = str(group_id) if group_id else "private"
    if gid_key not in user_memories or not isinstance(user_memories[gid_key], dict):
        user_memories[gid_key] = {}
        
    group_scope = user_memories[gid_key]
    uid_str = str(user_id)
    is_admin_user = (int(user_id) == ADMIN_QQ)
    
    if uid_str not in group_scope:
        group_scope[uid_str] = {
            "nickname": "life" if is_admin_user else (nickname or str(user_id)),
            "favor": 80 if is_admin_user else 50,  # 主人默认高好感基准，群友初始为 50 基准
            "traits": "主人兼创造者" if is_admin_user else "普通群友",
            "notes": ["专属主人兼创造者 life"] if is_admin_user else [],
            "updated_at": time.time()
        }
    if is_admin_user:
        group_scope[uid_str]["nickname"] = "life"
        group_scope[uid_str]["traits"] = "主人兼创造者"
        group_scope[uid_str]["notes"] = ["专属主人兼创造者 life"]
    elif nickname and group_scope[uid_str].get("nickname") != nickname:
        group_scope[uid_str]["nickname"] = nickname
    return group_scope[uid_str]

def clear_single_user_memory(target_uid: int, group_id: Optional[int] = None) -> bool:
    """清除单个群或全局的群友记忆"""
    uid_str = str(target_uid)
    cleared = False
    if group_id:
        gid_key = str(group_id)
        if gid_key in user_memories and uid_str in user_memories[gid_key]:
            del user_memories[gid_key][uid_str]
            cleared = True
    else:
        # 未指定群时在所有分区清理该用户
        for gid_key, group_scope in user_memories.items():
            if isinstance(group_scope, dict) and uid_str in group_scope:
                del group_scope[uid_str]
                cleared = True
    if cleared:
        save_user_memories()
    return cleared

def clear_group_all_memories(group_id: int) -> int:
    """清空指定群的所有记忆档案"""
    gid_key = str(group_id)
    if gid_key in user_memories:
        count = len(user_memories[gid_key])
        user_memories[gid_key] = {}
        save_user_memories()
        return count
    return 0

def clear_all_user_memories() -> int:
    """清除所有群的记忆（保留结构）"""
    global user_memories
    count = sum(len(users) for users in user_memories.values() if isinstance(users, dict))
    user_memories = {}
    save_user_memories()
    return count

# 好感度打分的节流：同一人 60 秒内只判一次，中间的话攒着下次合并判
FAVOR_MIN_INTERVAL = 60.0
_favor_last_run: Dict[tuple, float] = {}
_favor_pending: Dict[tuple, list] = {}

# 这些内容不值得单独花一次 API：开黑报点、纯应答
_FAVOR_NOISE = ("上号", "开黑", "组队", "掉线", "卡了", "卡死", "延迟", "在吗", "来了", "收到", "嗯嗯")

def _is_favor_worthy(text: str) -> bool:
    """这句话值不值得花一次判定请求"""
    if not text or not text.strip():
        return False
    plain = re.sub(r"\[CQ:[^\]]*\]", "", text).strip()
    if len(plain) < 4:
        return False
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", plain):
        return False
    # 哈哈哈 / 66666 这种复读，情绪信息量为零
    if len(set(plain)) <= 2:
        return False
    return plain not in _FAVOR_NOISE

async def evaluate_favor_with_ai(user_id: int, sender_nick: str, dialogue_snippet: str, bot_reply: str = "", group_id: Optional[int] = None) -> Tuple[int, str]:
    """请求 SiliconFlow 大模型动态分析群友真实态度并给出好感变化 delta 与印象"""
    if not dialogue_snippet.strip():
        return 0, ""
        
    headers = {
        "Authorization": f"Bearer {SILICONFLOW_API_KEY}",
        "Content-Type": "application/json"
    }
    
    sys_prompt = (
        "你是群聊好感度判定器。根据群友发言和小鲸鱼的回复，判断该群友对小鲸鱼的真实态度。\n"
        "只输出 JSON：{\"delta\": -5~5 的整数, \"impression\": 10 字以内印象，没有就填 null}\n"
        "扣分：骂人、叫大肥鱼、阴阳怪气、找茬对线、想吃鱼。\n"
        "0 分：普通水群、就事论事的闲聊，绝不无脑加分。\n"
        "加分：友好调侃 +1，夸奖投喂 +2，表白 +4。"
    )
    user_prompt = f"群友「{sender_nick}」发言：{dialogue_snippet}\n小鲸鱼回复：{bot_reply or '（无）'}"
    
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 80,
        "temperature": 0.1
    }
    
    try:
        global http_session
        if http_session is None or http_session.closed:
            http_session = aiohttp.ClientSession()
        async with http_session.post(SILICONFLOW_API_URL, headers=headers, json=payload, timeout=8) as resp:
            if resp.status == 200:
                data = await resp.json()
                if "usage" in data:
                    token_tracker.record_usage(data["usage"], MODEL_NAME, group_id=group_id)
                raw_text = data["choices"][0]["message"]["content"].strip()
                m = re.search(r"\{.*?\}", raw_text, re.DOTALL)
                if m:
                    res_json = json.loads(m.group(0))
                    delta = int(res_json.get("delta", 0))
                    delta = max(-5, min(5, delta))
                    impression = str(res_json.get("impression") or "").strip()
                    if impression.lower() in ["null", "none", "无"]:
                        impression = ""
                    return delta, impression
    except Exception as e:
        logger.warning("AI 动态好感度判定异常: %s", e)
    return 0, ""

async def update_user_memory_async(user_id: int, sender_nick: str, dialogue_snippet: str, group_id: Optional[int] = None, bot_reply: str = ""):
    """由 AI 大模型异步动态判定群友的好感度变化与印象画像（按群隔离）"""
    if user_id == BOT_QQ:
        return
    # 纯水聊/开黑噪音不打分，省掉一次完整请求
    if not _is_favor_worthy(dialogue_snippet) and dialogue_snippet != "戳了戳小鲸鱼":
        return

    key = (group_id, user_id)
    now_ts = time.time()
    if now_ts - _favor_last_run.get(key, 0.0) < FAVOR_MIN_INTERVAL:
        buf = _favor_pending.setdefault(key, [])
        buf.append(dialogue_snippet)
        del buf[:-5]  # 只留最近 5 句，防止长期不说话时无限攒
        return
    pending = _favor_pending.pop(key, [])
    if pending:
        dialogue_snippet = "；".join(pending[-2:] + [dialogue_snippet])
    _favor_last_run[key] = now_ts

    mem = get_or_create_user_memory(user_id, sender_nick, group_id=group_id)
    
    # 0. 特殊固定动作快速通道：戳一戳
    if dialogue_snippet == "戳了戳小鲸鱼":
        favor_delta = 1
        new_note = "喜欢戳小鲸鱼尾巴互动"
    else:
        # 1. 委托大模型深度理解真实语境并给出动态判定
        favor_delta, new_note = await evaluate_favor_with_ai(user_id, sender_nick, dialogue_snippet, bot_reply=bot_reply, group_id=group_id)

    # 主人特殊保护：如果用户是专属主人兼开发者，不录入外部混乱标签，好感度保底 80，保持最高默契
    if user_id == ADMIN_QQ:
        mem["nickname"] = "life"
        mem["traits"] = "主人兼创造者"
        mem["notes"] = ["专属主人兼创造者 life"]
        old_favor = mem.get("favor", 80)
        mem["favor"] = max(80, min(100, old_favor + max(0, favor_delta)))
        mem["updated_at"] = time.time()
        logger.info("[好感判定] 主人(%s) 好感维持: %d/100 (免脏标签污染)", ADMIN_QQ, mem["favor"])
        async with memory_write_lock:
            save_user_memories()
        return

    old_favor = mem.get("favor", 50)
    mem["favor"] = max(0, min(100, old_favor + favor_delta))
    if new_note and new_note not in mem["notes"]:
        mem["notes"].append(new_note)
        # 记忆条目与字符预算自愈引擎 (源自 dsh-companion 协议)：
        # 当条目超过 6 条或总字符超过 120 字时，自动合并压缩为高密度事实
        total_chars = sum(len(n) for n in mem["notes"])
        if len(mem["notes"]) > 6 or total_chars > 120:
            compact_notes = "；".join(mem["notes"])
            mem["notes"] = [compact_notes[:100]]  # 截取提炼高密度关键印象，永不超额
            
    mem["updated_at"] = time.time()
    logger.info("[好感判定] 群 %s 用户 %s(%s): delta=%+d, impression=%r, 好感度: %d -> %d/100", 
                group_id or "私聊", sender_nick, user_id, favor_delta, new_note, old_favor, mem["favor"])
    async with memory_write_lock:
        save_user_memories()

# 模块加载时直接读取记忆库
load_user_memories()

memory_write_lock = asyncio.Lock()

# 单群发信速率硬保护 (滑动窗口: 60秒内最多发 8 条，防腾讯单群风控禁言)
GROUP_RATE_LIMIT_WINDOW = 60.0
GROUP_MAX_MESSAGES_PER_WINDOW = 8
group_send_history: Dict[int, Deque[float]] = collections.defaultdict(collections.deque)
group_rate_limit_warned: Dict[int, float] = {}

def check_and_record_group_rate_limit(group_id: int) -> bool:
    """检查并记录群发信。60秒内超8条返回 False (防腾讯风控)"""
    now = time.time()
    dq = group_send_history[group_id]
    while dq and now - dq[0] > GROUP_RATE_LIMIT_WINDOW:
        dq.popleft()
    if len(dq) >= GROUP_MAX_MESSAGES_PER_WINDOW:
        return False
    dq.append(now)
    return True

# ==================== 状态存储（按群隔离） ====================
# 群聊近期图片缓存池 (用于两段式搜番与以图搜图): group_id -> deque of (image_url, timestamp, sender_nick)
group_recent_images: Dict[int, Deque[Tuple[str, float, str]]] = collections.defaultdict(lambda: collections.deque(maxlen=10))

# 情绪立绘目录
AVATARS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "avatars", "v2-状态立绘")

def get_status_avatar_b64(emotion: str = "angry") -> Optional[str]:
    """根据大肥鱼情绪获取状态立绘 base64 (angry / sleep / shy / think)"""
    mapping = {
        "angry": "01_生气鼓鼓_大头.webp",
        "sleep": "02_睡衣晚安_大头.webp",
        "shy": "03_脸红害羞_大头.webp",
        "think": "04_好奇思考_大头.webp"
    }
    fname = mapping.get(emotion)
    if not fname:
        return None
    p = os.path.join(AVATARS_DIR, fname)
    if os.path.exists(p):
        try:
            with open(p, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        except Exception:
            pass
    return None

group_timelines: Dict[int, Deque[Dict[str, any]]] = collections.defaultdict(lambda: collections.deque(maxlen=TIMELINE_MAX_LEN))
# 长窗口历史池（保留最近 200 条）
group_history_pool: Dict[int, Deque[Dict[str, any]]] = collections.defaultdict(lambda: collections.deque(maxlen=200))
group_last_activity: Dict[int, float] = collections.defaultdict(float)
group_last_active_reply: Dict[int, float] = collections.defaultdict(float)
group_msg_count_since_bot: Dict[int, int] = collections.defaultdict(int)
group_debounce_tasks: Dict[int, Optional[asyncio.Task]] = {}
group_silence_until: Dict[int, float] = {}  # 管理员 /stop 后的群主动插话静默期: group_id -> timestamp
group_names_cache: Dict[int, str] = {}      # 群号与群名称映射缓存: group_id -> group_name
# 问答追问配额表：(group_id, user_id) -> {"expire": float, "remain": int}
# 规则：5分钟内免 @ 连续追问最多 5 次；若有新的 @，重置为 5 分钟 + 5 次
user_follow_up_state: Dict[tuple, Dict[str, any]] = {}

# 用户 @ 冷却表: (group_id, user_id) -> timestamp
user_last_at_time: Dict[tuple, float] = {}

# 戳一戳冷却表: (group_id, user_id) -> timestamp (防恶意狂戳刷屏)
user_last_poke_time: Dict[tuple, float] = {}

# 群禁言与全员禁言状态监测（防禁言期间盲跑大模型烧 Token）
group_bot_ban_expire: Dict[int, float] = {}   # group_id -> 机器人解禁时间戳
group_all_banned: Dict[int, bool] = collections.defaultdict(bool)      # group_id -> 是否全员禁言
last_active_ban_check: Dict[int, float] = {}   # group_id -> 上次主动通过 API 校验的时间戳

async def check_bot_banned_realtime(ws: aiohttp.ClientWebSocketResponse, group_id: int) -> bool:
    """双重保障检查：优先查内存事件；若处于被动保护期，主动调 OneBot11 API 核实真实禁言状态"""
    now = time.time()
    if group_all_banned.get(group_id, False):
        return True
    expire = group_bot_ban_expire.get(group_id, 0.0)
    if now >= expire:
        return False
        
    # 如果处于被动保护期内，且距离上次主动核实已超过 5 秒，调用 get_group_member_info 验证真实状态
    last_check = last_active_ban_check.get(group_id, 0.0)
    if now - last_check >= 5.0 and ws:
        last_active_ban_check[group_id] = now
        try:
            info = await call_action(ws, "get_group_member_info", {"group_id": group_id, "user_id": BOT_QQ, "no_cache": True}, timeout=1.5)
            if info and info.get("status") == "ok":
                shut_up_time = info.get("data", {}).get("shut_up_timestamp", 0)
                if shut_up_time > now:
                    group_bot_ban_expire[group_id] = shut_up_time
                    logger.info("[群 %d] 主动核实：机器人确实处于禁言中，截止至 %s", group_id, time.strftime('%H:%M:%S', time.localtime(shut_up_time)))
                    return True
                else:
                    # 真实状态已经解禁，立即提前复活！
                    group_bot_ban_expire.pop(group_id, None)
                    logger.info("[群 %d] 主动核实：机器人已被管理员提前解禁，立即恢复正常响应！", group_id)
                    return False
        except Exception as e:
            logger.debug("[群 %d] 主动核实禁言状态异常: %s", group_id, e)
            
    return True

# 管理员私聊历史记录
admin_private_timeline: Deque[str] = collections.deque(maxlen=PRIVATE_TIMELINE_MAX_LEN)
# 管理员私聊长记录池（保留最近 100 条）
admin_private_history: Deque[str] = collections.deque(maxlen=100)

# WebSocket 请求回调映射与运行中模型任务集合
pending_requests: Dict[str, asyncio.Future] = {}
running_tasks: Set[asyncio.Task] = set()

# ==================== 按群独立配置管理 ====================
GROUP_CONFIGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "group_configs.json")
DEFAULT_GROUP_CONFIG = {
    "mode": "user",            # "user" (拟真闲聊) 或 "root" (技术老搭档)
    "enable_interject": True,   # 是否允许主动插话
    "interject_prob": 0.35,     # 主动插话基准概率 (0.0 ~ 1.0)
    "enable_sleep": True,       # 是否启用 01:00~07:00 拟真作息
    "enable_poke": True,        # 是否开启戳一戳互动与回戳
    "enable_games": True,       # 是否开启数字炸弹/掷骰子等文字游戏
    "enable_search": True,      # 是否开启联网热点检索 (鸭鸭搜/B站)
    "enable_bilibili_push": False, # 是否开启定时热门视频推送 (默认关)
    "enable_yunzai": True       # 是否开启云崽游戏生态 (原神/星铁/米游社)
}
DEFAULT_PRIVATE_CONFIG = {
    "mode": "user"
}
group_configs: Dict[str, dict] = {}

def load_group_configs():
    global group_configs
    if os.path.exists(GROUP_CONFIGS_FILE):
        try:
            with open(GROUP_CONFIGS_FILE, "r", encoding="utf-8") as f:
                group_configs = json.load(f)
        except Exception as e:
            logger.error("加载群独立配置失败: %s", e)
            group_configs = {}
    for gid in ALLOWED_GROUPS:
        sgid = str(gid)
        if sgid not in group_configs:
            group_configs[sgid] = dict(DEFAULT_GROUP_CONFIG)
        else:
            for k, v in DEFAULT_GROUP_CONFIG.items():
                if k not in group_configs[sgid]:
                    group_configs[sgid][k] = v
    if "private" not in group_configs:
        group_configs["private"] = dict(DEFAULT_PRIVATE_CONFIG)
    save_group_configs()

def save_group_configs():
    try:
        with open(GROUP_CONFIGS_FILE, "w", encoding="utf-8") as f:
            json.dump(group_configs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("保存群独立配置失败: %s", e)

YUNZAI_OTHER_YAML = os.environ.get("YUNZAI_OTHER_YAML", os.path.expanduser("~/projects/trss-yunzai/config/config/other.yaml"))

def sync_yunzai_whitelist(group_id: int, enable: bool):
    """同步修改云崽 other.yaml 中的 whiteGroup 白名单"""
    if not os.path.exists(YUNZAI_OTHER_YAML):
        return
    try:
        with open(YUNZAI_OTHER_YAML, "r", encoding="utf-8") as f:
            lines = f.readlines()

        new_lines = []
        in_white_group = False
        white_groups = []
        header_index = -1

        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("whiteGroup:"):
                in_white_group = True
                header_index = len(new_lines)
                new_lines.append(line)
                continue
            elif in_white_group:
                # 检查是否到了下一个顶格配置项
                if line and not line.startswith(" ") and not line.startswith("\t") and not stripped.startswith("#"):
                    in_white_group = False
                elif stripped.startswith("-"):
                    m = re.search(r"-\s*(\d+)", stripped)
                    if m:
                        white_groups.append(int(m.group(1)))
                    continue  # 稍后统一重写白名单列表项

            new_lines.append(line)

        # 增删 group_id
        if enable:
            if group_id not in white_groups:
                white_groups.append(group_id)
        else:
            white_groups = [g for g in white_groups if g != group_id]

        # 重新插入白名单群号
        group_lines = [f"  - {g}\n" for g in white_groups]
        if header_index != -1:
            for gl in reversed(group_lines):
                new_lines.insert(header_index + 1, gl)

        with open(YUNZAI_OTHER_YAML, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        logger.info("[云崽联动] 已同步群 %d 云崽白名单状态 -> %s (当前白名单: %s)", group_id, enable, white_groups)
    except Exception as e:
        logger.error("[云崽联动] 同步 other.yaml 失败: %s", e)

def get_group_config(group_id: Optional[int]) -> dict:
    if group_id is None:
        return group_configs.get("private", DEFAULT_PRIVATE_CONFIG)
    sgid = str(group_id)
    if sgid not in group_configs:
        group_configs[sgid] = dict(DEFAULT_GROUP_CONFIG)
    return group_configs[sgid]

def get_channel_mode(group_id: Optional[int]) -> str:
    return get_group_config(group_id).get("mode", "user")

def update_group_config(group_id: Optional[int], key: str, value: Any) -> bool:
    cfg = get_group_config(group_id)
    cfg[key] = value
    save_group_configs()
    logger.info("[配置更新] 频道 %s 更新 %s -> %r", "私聊" if group_id is None else f"群 {group_id}", key, value)
    if key == "enable_yunzai" and group_id is not None:
        sync_yunzai_whitelist(group_id, bool(value))
    return True

load_group_configs()
ADMIN_MODE: str = "user"  # 兼容旧引用

async def resolve_image_and_record(session: aiohttp.ClientSession, group_id: int, sender_nick: str, image_url: str):
    """后台异步提取图片要点，并记录至群聊近期上下文"""
    try:
        summary = await vision_resolver.transcribe_image_summary(session, SILICONFLOW_API_KEY, image_url)
        if summary:
            # 存入群近期语义时间线
            group_timeline[group_id].append({
                "role": "user",
                "content": f"[{sender_nick} 发送了一张图片/表情包: {summary}]",
                "time": time.time(),
                "sender_nick": sender_nick
            })
            logger.info("[群 %d] 视觉识别梗概已存入上下文: %s", group_id, summary)
    except Exception as e:
        logger.debug("视觉后台提取异常: %s", e)

# ==================== 大肥鱼专属表情包系统 ====================
MEME_PACK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meme-pack", "memes")

def get_random_meme_b64(text_hint: str = "") -> Optional[str]:
    """根据语境情绪匹配或随机挑选一张大肥鱼表情包返回 base64 字符串"""
    if not os.path.isdir(MEME_PACK_DIR):
        return None
    category = None
    lower = text_hint.lower()
    # 1. 游戏与二次元神图 (原神/鸣潮/米哈游/感觉不如...)
    if any(k in lower for k in ["原神", "鸣潮", "星铁", "崩铁", "米哈游", "感觉不如", "genshin"]):
        category = "_genshin"
    # 2. 股市与理财神图 (股票/A股/基金/跌了/亏/抄底/绿)
    elif any(k in lower for k in ["股票", "a股", "大盘", "基金", "跌", "亏", "抄底", "绿了", "韭菜"]):
        category = "_stock" if random.random() < 0.6 else "_green"
    elif any(k in lower for k in ["涨停", "大涨", "红了", "发财", "赚"]):
        category = "_red"
    # 3. 猖狂嘲笑/吃瘪乐子 (哈哈/笑死/乐/吃瘪/绷不住)
    elif any(k in lower for k in ["哈哈", "笑死", "绷不住", "乐子", "笑嘻", "吃瘪"]):
        category = "_laugh"
    # 4. 打工加班与职场 (上班/加班/打工/老板/周一/下班/累)
    elif any(k in lower for k in ["加班", "上班", "打工", "周一", "老板", "下班", "干活"]):
        category = "work"
    # 5. 睡觉晚安与熬夜 (睡觉/晚安/困/失眠/熬夜)
    elif any(k in lower for k in ["睡觉", "晚安", "好困", "困了", "熬夜", "失眠"]):
        category = "sleep"
    # 6. 要饭/红包 (给钱/v我50/红包/乞讨)
    elif any(k in lower for k in ["给钱", "v我50", "红包", "要饭", "打钱"]):
        category = "givemoney"
    # 7. 生气反驳炸毛 (气/才不是/骂/大肥鱼/炸毛/咬人/烦/哼)
    elif any(k in lower for k in ["气", "才不是", "骂", "大肥鱼", "炸毛", "咬人", "烦", "哼"]):
        category = "angry"
    # 8. 逆天串子/无敌/草 (草/闹麻/无敌/逆天/神了/6)
    elif any(k in lower for k in ["草", "闹麻", "无敌", "逆天", "神了", "6"]):
        category = "fool"
    # 9. 笨蛋/嫌弃 (笨蛋/傻/二货/嫌弃)
    elif any(k in lower for k in ["笨蛋", "傻", "二货", "嫌弃"]):
        category = "baka"
    # 10. 亲近开心 (喜欢/好耶/开心/真棒/爱了/摸摸)
    elif any(k in lower for k in ["喜欢", "好耶", "开心", "真棒", "爱了", "摸摸"]):
        category = "happy"
    # 11. 疑惑不解 (？/?/啥/为什么/为啥/看不懂/卡了)
    elif any(k in lower for k in ["？", "?", "啥", "为什么", "为啥", "看不懂", "卡了"]):
        category = "confused"
        
    cat_dir = os.path.join(MEME_PACK_DIR, category) if category else None
    if not cat_dir or not os.path.isdir(cat_dir):
        cats = [c for c in os.listdir(MEME_PACK_DIR) if not c.startswith("_") and os.path.isdir(os.path.join(MEME_PACK_DIR, c))]
        if not cats:
            return None
        cat_dir = os.path.join(MEME_PACK_DIR, random.choice(cats))
    
    files = [
        f for f in os.listdir(cat_dir)
        if f.lower().endswith(('.webp', '.jpg', '.jpeg', '.gif'))
        and os.path.getsize(os.path.join(cat_dir, f)) <= 1200 * 1024  # 限制在 1.2MB 以内，防止大图撑爆 WebSocket
    ]
    if not files:
        return None
    chosen = os.path.join(cat_dir, random.choice(files))
    try:
        with open(chosen, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except Exception as e:
        logger.warning("读取表情包失败: %s", e)
        return None

async def send_meme_message(ws: aiohttp.ClientWebSocketResponse, group_id: Optional[int] = None, user_id: Optional[int] = None, b64_data: str = ""):
    """使用 OneBot 11 原生结构化消息发送图片，完全规避字符截断与 CQ 码乱码"""
    if not b64_data:
        return
    msg_payload = [{"type": "image", "data": {"file": f"base64://{b64_data}"}}]
    try:
        if group_id:
            await call_action(ws, "send_group_msg", {"group_id": group_id, "message": msg_payload}, timeout=4.0)
            logger.info("[群 %d] ✅ 专属大肥鱼表情包图片已送达", group_id)
        elif user_id:
            await call_action(ws, "send_private_msg", {"user_id": user_id, "message": msg_payload}, timeout=4.0)
            logger.info("[私聊 %d] ✅ 专属大肥鱼表情包图片已送达", user_id)
    except Exception as e:
        logger.warning("发送表情包失败: %s", e)

# ==================== 大肥鱼专属表情包与消息清洗系统 ====================
def parse_card_content(text: str) -> str:
    """从分享卡片段（json / xml / share）里抽标题、描述、BV号与链接，避免被一刀切抹掉"""
    def _parse_card_match(m):
        body = m.group(2)
        titles = []
        bvs = []
        
        # 提取 BV 号
        bv_match = re.search(r'(BV[0-9A-Za-z]{10})', body)
        if bv_match:
            bvs.append(bv_match.group(1))
            
        # 寻找 JSON data/content
        m_val = re.search(r'(?:data|content)=([\s\S]+)', body)
        raw_val = m_val.group(1).strip() if m_val else ""
        if raw_val:
            # 兼容 HTML/CQ 转义
            unescaped = (
                raw_val.replace("&#44;", ",")
                .replace("&amp;", "&")
                .replace("&#91;", "[")
                .replace("&#93;", "]")
                .replace("&quot;", '"')
            )
            # 有时整个 JSON 外部被双引号包裹
            if unescaped.startswith('"') and unescaped.endswith('"'):
                try:
                    unescaped = json.loads(unescaped)
                except Exception:
                    unescaped = unescaped[1:-1]
            try:
                obj = json.loads(unescaped) if isinstance(unescaped, str) else unescaped
                def walk(o):
                    if isinstance(o, dict):
                        for k, v in o.items():
                            if isinstance(v, str):
                                if k.lower() in ("title", "desc", "summary", "prompt") and v.strip():
                                    titles.append(v.strip())
                            else:
                                walk(v)
                    elif isinstance(o, list):
                        for item in o:
                            walk(item)
                walk(obj)
            except Exception:
                pass
                
        if not titles:
            for key in ["title", "desc", "summary"]:
                for m_t in re.finditer(rf'{key}=["\']?([^"\'\],]+)', body, re.IGNORECASE):
                    val = m_t.group(1).replace("&#44;", ",").replace("&amp;", "&").strip()
                    if val and val not in titles:
                        titles.append(val)
        
        header = "[分享视频卡片]" if bvs else "[分享卡片]"
        title_str = " · ".join(list(dict.fromkeys(titles))[:2]) if titles else "（网页/卡片分享）"
        extra = f" (BV号: {bvs[0]})" if bvs else ""
        return f"{header} {title_str}{extra}"

    return re.sub(r"\[CQ:(share|json|xml),([\s\S]*?)\]", _parse_card_match, text)

async def expand_forward_messages(ws: aiohttp.ClientWebSocketResponse, text: str, depth: int = 1) -> str:
    """识别合并转发 CQ 码 [CQ:forward,id=xxx]，向 NapCat 拉取详情并递归展开多行聊天记录与应用卡片"""
    if depth > 2:  # 最大嵌套深度限制为 2 层，防止死循环与 token 溢出
        return text

    m = re.search(r"\[CQ:forward,id=([^\]]+)\]", text)
    if not m:
        return text
    fid = m.group(1)
    
    # 尝试拉取合并转发节点
    res = await call_action(ws, "get_forward_msg", {"message_id": fid}, timeout=2.5)
    if not res or res.get("status") != "ok":
        res = await call_action(ws, "get_forward_msg", {"id": fid}, timeout=2.5)
        
    messages = []
    if res and isinstance(res.get("data"), dict):
        messages = res["data"].get("messages") or []
    elif res and isinstance(res.get("data"), list):
        messages = res["data"]
        
    if not messages:
        return text.replace(m.group(0), "[合并转发聊天记录]")
        
    indent = "  " * depth
    lines = [f"{indent}[合并转发聊天记录 · 共 {len(messages)} 条]"]
    for msg_node in messages[:12]:  # 截取前 12 条避免过长
        sender = (msg_node.get("sender") or {}).get("nickname") or (msg_node.get("sender") or {}).get("card") or "某人"
        raw_msg = msg_node.get("raw_message") or msg_node.get("content") or msg_node.get("message") or ""
        if isinstance(raw_msg, list):
            segs = []
            for seg in raw_msg:
                if isinstance(seg, dict):
                    seg_type = seg.get("type")
                    seg_data = seg.get("data") or {}
                    if seg_type == "text":
                        segs.append(seg_data.get("text", ""))
                    elif seg_type == "image":
                        segs.append("[图片]")
                    elif seg_type == "face":
                        segs.append("[表情]")
                    elif seg_type in ("json", "xml", "share"):
                        # 深度解析合并记录里的应用卡片与小程序，保留应用名与标题
                        card_raw = seg_data.get("data") or seg_data.get("content") or json.dumps(seg_data, ensure_ascii=False)
                        parsed_card = parse_card_content(f"[CQ:{seg_type},data={card_raw}]")
                        segs.append(f" {parsed_card} ")
                    elif seg_type == "forward":
                        sub_fid = seg_data.get("id") or seg_data.get("message_id")
                        if sub_fid and depth < 2:
                            sub_text = await expand_forward_messages(ws, f"[CQ:forward,id={sub_fid}]", depth=depth+1)
                            segs.append(f"\n{sub_text}\n")
                        else:
                            segs.append("[嵌套合并转发]")
                elif isinstance(seg, str):
                    segs.append(seg)
            raw_msg = "".join(segs)

        # 检查单条消息文本中是否还内嵌有 [CQ:forward,id=xxx]
        if "[CQ:forward,id=" in str(raw_msg) and depth < 2:
            raw_msg = await expand_forward_messages(ws, str(raw_msg), depth=depth+1)

        clean_node_text = clean_cq_codes(str(raw_msg)).strip()
        if clean_node_text:
            lines.append(f"{indent}  {sender}: {clean_node_text[:140]}")
            
    expanded_text = "\n".join(lines)
    return text.replace(m.group(0), expanded_text)

# ==================== 群文件精简标题拉取 (不读正文防浪费 Token) ====================
async def get_group_files_brief(ws: aiohttp.ClientWebSocketResponse, group_id: int, limit: int = 8) -> str:
    """向 NapCat 查询群根目录文件列表，仅提取文件名、大小与上传者，绝不下载正文"""
    try:
        res = await call_action(ws, "get_group_root_files", {"group_id": group_id}, timeout=3.0)
        if not res or res.get("status") != "ok":
            return ""
        data = res.get("data") or {}
        files = data.get("files") or []
        if not files:
            return "（本群文件列表为空）"
        lines = []
        for f in files[:limit]:
            fname = f.get("file_name") or "未命名文件"
            size_bytes = f.get("file_size") or 0
            if size_bytes >= 1024 * 1024:
                size_str = f"{size_bytes / (1024 * 1024):.1f}MB"
            elif size_bytes >= 1024:
                size_str = f"{size_bytes / 1024:.0f}KB"
            else:
                size_str = f"{size_bytes}B"
            uploader = f.get("uploader_name") or str(f.get("uploader") or "")
            lines.append(f"- {fname} ({size_str}, 上传者: {uploader})")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("获取群文件列表异常: %s", e)
        return ""

def clean_cq_codes(text: str) -> str:
    """去除表情、图片、文件等大段 CQ 码，保留纯文本与卡片语义"""
    # 先做卡片语义提取
    text = parse_card_content(text)
    # 再剥离纯媒体与文件类 CQ 码（包含 file, image, record, video, face, poke 等）
    text = re.sub(r"\[CQ:(image|record|video|file|audio)[^\]]*\]", "", text)
    text = re.sub(r"\[CQ:(face|poke)[^\]]*\]", "", text)
    return text.strip()

def strip_markdown(text: str) -> str:
    """去除 Markdown 符号，适应手机 QQ 纯文本阅读"""
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\*(.*?)\*", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"`{3}[a-zA-Z]*\n?([\s\S]*?)`{3}", r"\1", text)
    text = re.sub(r"`(.*?)`", r"\1", text)
    return text.strip()

def is_low_info_message(text: str) -> bool:
    """识别毫无信息量的水群废话"""
    clean = re.sub(r"[\s\d\W_]+", "", text)
    if len(clean) < 2:
        return True
    low_info_words = {"哈哈", "哈哈哈", "666", "好的", "收到", "确实", "可以", "牛逼", "问号", "点赞"}
    return clean in low_info_words

def sanitize_text(text: str) -> str:
    """清理非法 Unicode 孤立代理项及残损控制字符，防止发给大模型 API 时报 400"""
    if not text:
        return ""
    return text.encode("utf-8", "replace").decode("utf-8", "ignore")

def is_bot_sleeping(group_id: Optional[int] = None) -> bool:
    """判断当前是否为夜间入睡期 (01:00 ~ 07:00 CST)，主人叫醒期除外，按群检查是否开启作息"""
    global sleep_override_until
    now = time.time()
    if now < sleep_override_until:
        return False
    if group_id is not None:
        cfg = get_group_config(group_id)
        if not cfg.get("enable_sleep", True):
            return False
    # 获取中国标准时间 CST (UTC+8) 小时
    cst_hour = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).hour
    return SLEEP_START_HOUR <= cst_hour < SLEEP_END_HOUR

def check_user_fatigue(group_id: int, user_id: int) -> tuple[bool, bool]:
    """
    群友疲劳度衰减检查：防无聊群友高频疯狂 @ 调戏机器人
    返回: (is_fatigued: bool, is_first_trigger: bool)
    """
    if user_id == ADMIN_QQ:
        return False, False  # 主人享有特权，永远不疲劳

    now = time.time()
    key = (group_id, user_id)
    if now < user_fatigue_until[key]:
        return True, False  # 正在疲劳冷却中

    dq = user_mention_burst[key]
    while dq and now - dq[0] > 120.0:
        dq.popleft()
    dq.append(now)

    if len(dq) >= 5:
        # 2 分钟内连续 @ 达到 5 次，触发 60 秒疲劳冷却
        user_fatigue_until[key] = now + 60.0
        return True, True
    return False, False

def de_template(reply: str) -> str:
    """
    反模板套路削平闸门 (参考 Estrella Bot anti-template 设计)
    连续出现破折号抖机灵（——...）或相同词起手时，削平形状保留原话，拒绝句式坍塌
    """
    global recent_bot_replies
    text = reply.strip()
    if not text:
        return reply

    # 1. 削平破折号抖机灵 (如连续多句以 "——..." 结尾)
    if "——" in text or "—" in text:
        dash_count = sum(1 for r in recent_bot_replies if "——" in r or "—" in r)
        if dash_count >= 2:
            m = re.search(r"[—]{1,2}[^，。！？\n]{2,15}$", text)
            if m:
                trimmed = text[:m.start()].strip()
                if len(trimmed) >= 3:
                    logger.info("[反模板闸] 削平连续破折号抖机灵: %r -> %r", text, trimmed)
                    text = trimmed

    # 2. 削平同一口头禅连续起手 (如连续多句以 "好家伙" / "行吧" / "啧" / "不是" 起手)
    clean_op = re.sub(r"^[~～!！?？\s,，。]+", "", text)
    if len(clean_op) >= 2:
        op2 = clean_op[:2]
        op3 = clean_op[:3]
        match_count = 0
        for r in recent_bot_replies:
            clean_r = re.sub(r"^[~～!！?？\s,，。]+", "", r)
            if clean_r.startswith(op3) or (len(op2) == 2 and clean_r.startswith(op2)):
                match_count += 1
        if match_count >= 2:
            cut_len = len(op3) if clean_op.startswith(op3) else len(op2)
            trimmed = clean_op[cut_len:].lstrip("，, 。.！!~～")
            if len(trimmed) >= 2:
                logger.info("[反模板闸] 削平连续口头禅起手: %r -> %r", text, trimmed)
                text = trimmed

    return text

def calculate_interject_probability(group_id: int, base_prob: float) -> tuple[float, str]:
    """
    计算插话冷热场动态权重 (参考 Estrella Bot interject 活跃提权 + 冷场降权)
    """
    now = time.time()
    last_msg = group_last_msg_time.get(group_id, 0.0)
    last_reply = group_last_bot_reply_time.get(group_id, 0.0)

    # 冷场降权：若本条之前群里沉寂超过 10 分钟 (600s)，大幅降权至 0.25x，绝不当破冰显眼包
    if last_msg > 0 and (now - last_msg) >= 600.0:
        return base_prob * 0.25, "冷场降权(0.25x)"

    # 热场提权：若机器人在 10 分钟内刚在该群说过话，说明参与热聊，提权 1.5x 积极接话
    if last_reply > 0 and (now - last_reply) < 600.0:
        return min(0.85, base_prob * 1.5), "热场提权(1.5x)"

    return base_prob, "常规概率"

# 旁观插话防打扰关键词黑名单：群友日常约饭、开黑组队、掉线卡死或特定游戏私聊，机器人绝不主动插嘴
INTERJECT_NOISE_KEYWORDS = {
    "开黑", "上号", "上车", "组队", "打本", "吃鸡", "双排", "三排", "五排",
    "打把", "来把", "打一把", "谁来", "带我", "拉我", "吃啥", "吃什么", "喝啥", "喝什么",
    "晚饭", "午饭", "夜宵", "有人吗", "谁在", "闪退", "卡死", "掉线", "连不上",
    "登不上", "进不去", "更新了", "封号", "睡了", "睡觉", "晚安", "下了", "撤了"
}

def should_active_interrupt(text: str, group_id: int = 0, msg_count_since: int = 0) -> tuple[bool, bool]:
    """
    判断是否应该主动插话（结合各群独立开关、黑名单降噪与冷热场动态调节）。
    返回: (should_trigger: bool, is_mention_bot: bool)
    """
    cfg = get_group_config(group_id if group_id else None)
    if not cfg.get("enable_interject", True):
        return False, False

    # 睡眠期严格禁止任何主动插话
    if is_bot_sleeping(group_id):
        return False, False

    lower = text.lower()
    # 1. 明确提到机器人、模型、AI或相关代称梗词（100% 触发）
    if any(kw in lower for kw in BOT_KEYWORDS):
        return True, True
    
    # 2. 命中日常私聊/开黑/组队黑名单词汇，坚决不瞎插话打扰群友
    if any(nkw in text for nkw in INTERJECT_NOISE_KEYWORDS):
        return False, False

    # 3. 问句或讨论，结合冷热场动态介入（严格要求文本长度 >= 6，排除无主语碎片追问）
    has_question = any(q in text for q in ["？", "?", "怎么", "如何", "为什么", "为啥", "什么原因", "有没有", "是谁", "啥"])
    if has_question and len(text) >= 6:
        base_p = float(cfg.get('interject_prob', 0.35))
        final_prob, why = calculate_interject_probability(group_id, base_p)
        logger.info("[群 %d] 插话问句触发判定: 基准=0.35, 调整后=%.2f (%s)", group_id, final_prob, why)
        import random
        return (random.random() < final_prob), False

    return False, False

# ==================== OneBot11 API 请求包装 ====================
async def call_action(ws: aiohttp.ClientWebSocketResponse, action: str, params: dict, timeout: float = 2.0) -> Optional[dict]:
    """通过 WebSocket 发送带 echo 的 API 调用并等待响应"""
    echo_id = str(uuid.uuid4())
    future = asyncio.get_running_loop().create_future()
    pending_requests[echo_id] = future
    payload = {"action": action, "params": params, "echo": echo_id}
    try:
        await ws.send_str(json.dumps(payload))
        return await asyncio.wait_for(future, timeout=timeout)
    except Exception as e:
        logger.debug("call_action %s 超时或异常: %s", action, e)
        return None
    finally:
        pending_requests.pop(echo_id, None)

def is_game_or_yunzai_command(text: str) -> bool:
    """判断是不是云崽或其他插件的专用指令，避免抢答"""
    t = text.strip()
    if not t:
        return False
    
    # 1. 主流 Bot / 云崽生态专属前缀符号 (包含英文与中文全角符号)
    # 包括: # / * % ! ？ ? ~ ～ 井 井号
    cmd_prefixes = (
        "#", "井", "井号",
        "*", "＊",
        "%", "％",
        "~", "～",
        "!", "！",
        "?", "？",
        "$", "￥"
    )
    if t.startswith(cmd_prefixes):
        # 排除管理员的系统级斜杠管理指令 (/root, /user, /new, /stop 等依然保留给 AI)
        return True
        
    # 2. 云崽及游戏插件常用免前缀功能指令集合
    patterns = [
        # 异环与塔吉多前缀指令免抢答
        r"^(?:异环|yh|塔吉多|tjd|幻塔|ht).*",
        # 抽卡模拟与抽卡记录分析
        r"^(?:10|[武器池常驻]*[十]+|抽|单)[连抽卡奖][123武器池常驻]*$",
        r"^(?:原神|星铁|星穹铁道|崩铁|绝区零|崩三|崩坏3)?(?:强制)?(?:全部)?(?:抽卡|抽奖|角色|武器|集录|常驻|up|新手|光锥)?(?:池)?(?:记录|祈愿|分析|统计)$",
        r"^(?:导入|导出)记录(?:json)?(?:v2|v4)?$",
        r"^(?:记录帮助|抽卡帮助|安卓帮助|苹果帮助|电脑帮助|pc帮助|ios帮助)$",
        
        # 账号与 UID 绑定类
        r"^(?:原神|星铁|绝区零)?(?:绑定|我的|删除|解绑)?(?:uid|UID)(?:\s|\+)*\d*$",
        r"^(?:原神|星铁|绝区零)?(?:绑定|我的|删除|配置|检查)?c(?:oo)?k(?:ie)?(?:状态|代码|帮助)?$",
        r"^(?:体力|[Cc](?:oo)?[Kk](?:ie)?)帮助$",
        r".*_MHYUUID.*",
        r"^(?:接受)?绑定(?:主|子)?(?:用户|账户|账号).*",
        
        # 面板、练度与伤害计算 (支持: 雷神面板, 流萤遗器, 纳西妲伤害, 角色面板等)
        r"^.+(?:详细|详情|面板|面版|圣遗物|遗器|伤害(?:[1-9]+\d*)?)(?:\s*\d{9,10})?.*$",
        r"^(?:星铁|原神)?(?:面板角色|角色面板|面板)(?:列表)?(?:\s*\d{9,10})?$",
        r"^(?:星铁|原神)?(?:全部面板更新|更新全部面板|获取游戏角色详情|更新面板|面板更新)(?:\s*\d{9,10})?$",
        r"^(?:星铁|原神)?(?:圣遗物|遗器)列表(?:\s*\d{9,10})?$",
        r"^(?:星铁|原神)?(?:面板|喵喵)?练度统计$",
        r"^(?:角色|换|更换)?面[板版]帮助$",
        
        # 深渊、幻境剧诗、虚构叙事与战绩
        r"^(?:上期|往期|本期)?(?:深渊|深境|深境螺旋)(?:上期|往期|本期)?(?:第)?(?:9|10|11|12|九|十|十一|十二)?(?:层)?(?:\s*\d+)?.*$",
        r"^(?:深渊|深境螺旋|末日幻影|虚构叙事|忘却之庭|混沌回忆)(?:战绩|查询|配队|组队)?.*$",
        r"^(?:本期|上期)?(?:幻想|幻境|剧诗|幻想真境剧诗)(?:\s*\d+)?.*$",
        
        # 实时便签、体力、树脂、开拓力、原石与星琼札记
        r"^(?:原神|星铁|崩铁|绝区零)?(?:体力|树脂|开拓力|查询体力)$",
        r"^(?:原石|札记|(?:星铁)?星琼)(?:[0-9]|[一二两三四五六七八九十]+)*月*$",
        r"^(?:原石|札记|(?:星铁)?星琼)(?:统计|任务)$",
        
        # 攻略、素材、日历与兑换码
        r"^.+(?:突破|材料|素材|培养|攻略(?:[1-7])?)$",
        r"^(?:今日|每日|今天|我的|明天|明日|周[1-7一二三四五六日])*(?:素材|材料|天赋|日历)(?:列表)?(?:\s*\d+)?$",
        r"^(?:原神|星铁|崩铁|崩三|崩坏三|崩坏3|绝区零)?(?:直播|前瞻)?兑换码$",
        
        # 系统/功能菜单 (喵喵帮助, 星铁帮助, 喵喵版本等)
        r"^(?:喵喵|星铁|崩铁|星轨|绝区零|zzz|鸣潮)?(?:命令|帮助|菜单|help|说明|功能|指令|使用说明|版本|更新日志)$",
        r"^(?:原神|星铁|绝区零|鸣潮)?(?:邦布|人偶|七圣召唤.*)$",
        r"^(?:留影(?:叙佳期)?|(?:领)?(?:角色)?生日(?:卡)?)$",
        
        # 椰奶与土块娱乐互动指令 (免前缀)
        r"^(?:点歌|网易点歌|qq点歌|酷狗点歌)\s*.+$",
        r"^(?:摸摸头|摸头|抱抱|亲亲|咬|拍|敲|撕|贴)\s*.*$",
        r"^(?:60秒早报|早报|今日早报|每日早报|晚报)$",
        r"^(?:疯狂星期四|疯四|v50|kfc|肯德基)$",
        r"^(?:答案之书|抽取答案)\s*.*$"
    ]
    for p in patterns:
        if re.search(p, t, re.IGNORECASE):
            return True
    return False

async def extract_reply_context(ws: aiohttp.ClientWebSocketResponse, event: dict, raw_msg: str) -> Optional[str]:
    """提取被回复引用的原消息内容"""
    # 1. 直接从 event 中的 reply 字段获取（NapCat 标准支持）
    if "reply" in event and isinstance(event["reply"], dict):
        reply_obj = event["reply"]
        nick = reply_obj.get("sender", {}).get("nickname") or reply_obj.get("sender", {}).get("card") or "群友"
        orig_text = clean_cq_codes(reply_obj.get("raw_message") or reply_obj.get("message") or "")
        if orig_text:
            return f'[引用 {nick}: "{orig_text[:50]}"]'

    # 2. 从 [CQ:reply,id=xxx] 解析并异步调用 get_msg
    m = re.search(r"\[CQ:reply,id=(-?\d+)\]", raw_msg)
    if m:
        msg_id = int(m.group(1))
        res = await call_action(ws, "get_msg", {"message_id": msg_id}, timeout=1.0)
        if res and res.get("status") == "ok":
            data = res.get("data", {})
            nick = data.get("sender", {}).get("nickname") or data.get("sender", {}).get("card") or "群友"
            orig_text = clean_cq_codes(data.get("raw_message") or "")
            if orig_text:
                return f'[引用 {nick}: "{orig_text[:50]}"]'
    return None

# ==================== LLM 调用 ====================
async def call_deepseek(timeline_text: str, is_at: bool, is_admin: bool = False, is_private: bool = False, is_mention: bool = False, target_uid: Optional[int] = None, sender_nick: str = "", group_id: Optional[int] = None) -> Optional[str]:
    """请求 SiliconFlow DeepSeek-V3 模型"""
    global http_session
    headers = {
        "Authorization": f"Bearer {SILICONFLOW_API_KEY}",
        "Content-Type": "application/json"
    }
    
    identity_note = f"（补充认知：你在 QQ 上的名字/昵称叫「小鲸鱼」或「鲸宝」，你的 QQ 号是 {BOT_QQ}，你叫 life bot fish 是因为部署账号的昵称，那也是你！严禁把自己的名字当成别人或山寨货）\n"

    # 群友记忆与好感度上下文注入
    user_mem_prompt = ""
    if target_uid and target_uid != BOT_QQ:
        mem = get_or_create_user_memory(target_uid, sender_nick, group_id=group_id)
        if target_uid == ADMIN_QQ:
            user_mem_prompt = f"【当前交谈者身份】：专属主人兼创造者 life（QQ {ADMIN_QQ}），拥有绝对最高权限与天然极高好感度。\n"
        else:
            notes_str = "、".join(mem.get("notes", [])) if mem.get("notes") else "初次接触"
            user_mem_prompt = f"【当前交谈群友信息】：昵称: {mem.get('nickname')}, 好感度: {mem.get('favor')}/100, 历史印象: {notes_str}\n"

    # 注入群内焦点互动关系（仅群聊生效）
    if group_id and not is_private:
        relations_hint = cross_interaction.get_group_relations_summary(group_id, current_user_id=target_uid)
        if relations_hint:
            user_mem_prompt += relations_hint

    channel_mode = get_channel_mode(group_id if not is_private else None)
    is_root_work = is_admin and channel_mode == "root"
    if is_admin and is_private:
        if channel_mode == "root":
            user_prompt = (
                f"{identity_note}"
                f"【当前交谈者身份】：专属主人兼创造者「{sender_nick}」(QQ {ADMIN_QQ})\n"
                f"以下是你们的历史对话记录：\n{timeline_text}\n\n"
                f"【核心交互原则】：\n"
                f"1. 你依然是主人身边那条熟悉的大肥鱼/小鲸鱼，在 /root 模式下你是他的私人技术搭档与秘书；\n"
                f"2. 懂主人的日常口语、黑话、意图与暗语，绝不咬文嚼字，绝不打官腔背书；\n"
                f"3. 像天天开黑写代码的老朋友一样交流，直接奔着解决问题去，干脆利落自然回复（1~2句，绝不用句号）："
            )
        else:
            user_prompt = (
                f"{identity_note}"
                f"{user_mem_prompt}"
                f"以下是你与专属主人「{sender_nick}」(QQ {ADMIN_QQ}) 的私聊历史：\n{timeline_text}\n\n"
                f"【主人发来私信】：\n"
                f"当前跟你说话的是你的专属主人兼创造者。请清楚认同主人身份，态度自然真实、听主人的话，结合好感度自然交流（20~40 字）："
            )
    elif is_admin and is_at:
        if channel_mode == "root":
            user_prompt = (
                f"{identity_note}"
                f"以下是当前群聊对话记录：\n{timeline_text}\n\n"
                f"【主人「{sender_nick}」(QQ {ADMIN_QQ}) 在群里找你】：\n"
                f"当前是 /root 工作模式，你是主人的技术老搭档兼秘书。懂主人的大白话与口语意图，像熟人开黑一样干脆利落接话（1~2句，绝不用句号）："
            )
        else:
            user_prompt = (
                f"{identity_note}"
                f"{user_mem_prompt}"
                f"以下是当前群聊对话记录：\n{timeline_text}\n\n"
                f"【注意识别对话者身份】：当前是你的专属主人兼创造者 life (QQ {ADMIN_QQ}) 在群里呼叫你！\n"
                f"（清楚知道对方是你的主人 life，态度自然真实、俏皮亲近，听主人的话，绝不搞错称呼）\n"
                f"请结合当前对主人的极高好感度自然回复（20~35 字，绝不用句号）："
            )
    elif is_at or is_mention:
        user_prompt = (
            f"{identity_note}"
            f"{user_mem_prompt}"
            f"群友「{sender_nick}」发来消息：\n{timeline_text}\n\n"
            f"请以大肥鱼口吻极简回复（默认1句，最多2句，绝不用句号，别写小作文）："
        )
    else:
        user_prompt = (
            f"{identity_note}"
            f"群友在群聊中的近期发言：\n{timeline_text}\n\n"
            f"【旁观规则（最高优先级）】：\n"
            f"1. 你正在旁观。若群友是在私聊互动、打游戏开黑/组队、讨论内部黑话、或你没有十足把握理解其前因后果，【必须直接且仅输出 [PASS] 彻底闭嘴】；\n"
            f"2. 严禁不懂装懂、严禁答非所问、严禁发出反问套话（如「你在说什么呀」、「我也打不开」）；\n"
            f"3. 只有当你 100% 搞懂了话题并能给出极其一针见血的 1 句短接话时才说；插不上话输出 [PASS]："
        )
    
    # 联网检索增强：当对话涉及最新时效词且群开启搜索时，自动注入轻量网页搜索上下文
    web_search_context = ""
    cfg = get_group_config(group_id)
    if cfg.get("enable_search", True) and http_session:
        time_keywords = ("今天", "最新", "热搜", "发布会", "刚刚", "最近发生", "发生什么事", "有什么新闻")
        if any(kw in timeline_text for kw in time_keywords):
            # 提取简短检索关键词
            m_q = re.search(r'(?:今天|最新|刚刚|查查|搜搜|看下)?\s*([^\n，。？！]{3,20})', timeline_text)
            query_str = m_q.group(1).strip() if m_q else timeline_text[-20:].strip()
            if query_str:
                search_res = await connectors.search_web_brief(http_session, query_str, limit=2)
                if search_res:
                    snippets_str = " | ".join([f"{r['title']}: {r['snippet']}" for r in search_res])
                    web_search_context = f"\n【实时联网搜索参考（仅供知识辅助，依然保持自身人设）】：{snippets_str}\n"

    # System Prompt 保持绝对静态：热梗、面板指令这些随消息变的内容全部走 user 末尾，
    # 这样每次请求的前缀一致，服务端不用反复重算开头那一段
    system_content = sanitize_text(ROOT_SYSTEM_PROMPT if is_root_work else SYSTEM_PROMPT)

    # 随消息变化的上下文，统一挂在 user 消息末尾
    dynamic_context = ""
    if not is_root_work:
        memes_context = meme_manager.get_prompt_snippet(timeline_text)
        if memes_context:
            dynamic_context += f"\n{memes_context}\n"
        # 面板指令两千多字符，只有真在问用法时才带，平时一个字都不发
        if needs_games_kb(timeline_text):
            dynamic_context += f"\n【云崽指令清单（群友在问用法，按这个答）】：\n{GAMES_KB}\n"
    # 群文件上下文增强（当群友询问群文件时，仅拉取文件列表标题与元信息，绝对不读文件正文）
    group_files_context = ""
    if group_id and current_ws and any(kw in timeline_text for kw in ("群文件", "文件列表", "群里有没有", "群里发过", "发群文件", "找文件")):
        files_summary = await get_group_files_brief(current_ws, group_id)
        if files_summary:
            group_files_context = f"\n【本群文件列表（仅供参考文件名与上传人，未读取文件正文）】：\n{files_summary}\n"

    user_prompt = sanitize_text(user_prompt + web_search_context + group_files_context)
    # 注入跨群公共话题记忆（仅供认知背景，不破坏当前群独立性）
    if group_id:
        cross_topics_hint = cross_memory.cross_group_mgr.get_recent_topics_prompt(exclude_group_id=group_id, limit=3)
        if cross_topics_hint:
            user_prompt = sanitize_text(user_prompt + cross_topics_hint)
    if dynamic_context:
        user_prompt = sanitize_text(user_prompt + dynamic_context)
    max_tokens_val = 1500 if is_root_work else MAX_OUTPUT_TOKENS
    temperature_val = 0.75 if is_root_work else 0.8
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt}
        ],
        "max_tokens": max_tokens_val,
        "temperature": temperature_val
    }
    
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession()

    t0 = time.time()
    try:
        async with http_session.post(SILICONFLOW_API_URL, headers=headers, json=payload, timeout=20) as resp:
            elapsed = time.time() - t0
            if resp.status != 200:
                err_text = await resp.text()
                logger.error("API 调用失败 (%d, 耗时 %.2fs): %s", resp.status, elapsed, err_text)
                return None
            data = await resp.json()
            if "usage" in data:
                token_tracker.record_usage(data["usage"], MODEL_NAME, group_id=group_id)
            reply = data["choices"][0]["message"]["content"].strip()
            logger.info("模型生成完成 (耗时 %.2fs): %s", elapsed, reply[:30])
            return reply
    except asyncio.CancelledError:
        logger.info("DeepSeek 请求被中止。")
        raise
    except Exception as e:
        logger.error("调用大模型异常 (耗时 %.2fs): %s", time.time() - t0, e)
        return None

# 全局 HTTP 会话（复用连接池减少延迟）
http_session: Optional[aiohttp.ClientSession] = None
current_ws: Optional[aiohttp.ClientWebSocketResponse] = None

# ==================== 发送消息 ====================
def remove_trailing_period(text: str) -> str:
    """去除句末死板的句号与无意义括号，消除 AI 人机感"""
    text = text.strip()
    while text.endswith(("。", ".")):
        text = text[:-1].strip()
    # 彻底剔除句尾常见的人机味括号（如“（）”、“（”、“）”）
    text = re.sub(r'[\(（][\)）]?\s*$', '', text).strip()
    return text

# ==================== 外链防风控混淆器 ====================
_OBFUSCATE_CHARS = ("中", "的", "了", "啊", "呀", "呢", "哦", "嘛", "啦", "哈")
_OBFUSCATE_HOST_RE = re.compile(r"(https?://)([^/\s?#（）【】《》，。；：、！？]+)", re.IGNORECASE)
_CN_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")

def obfuscate_urls_for_qq(text: str) -> str:
    """在每个外链域名内部随机插一个汉字，打破 QQ 外链探测，100% 避开外链风控吞消息"""
    if not text or "://" not in text:
        return text

    def _replace(m):
        proto = m.group(1)
        host = m.group(2)
        if _CN_CHAR_RE.search(host):
            return m.group(0)  # 已经混淆或包含中文，幂等跳过
        ch = random.choice(_OBFUSCATE_CHARS)
        if host.startswith("www.") and len(host) > 5:
            pos = random.randint(1, 4)
        else:
            pos = random.randint(1, max(1, len(host) - 1))
        return f"{proto}{host[:pos]}{ch}{host[pos:]}"

    return _OBFUSCATE_HOST_RE.sub(_replace, text)

# 群消息发送频率滑动窗口限制（QQ 群官方/NapCat 风控：单群 1 分钟最多 10 条）
# 为确保绝对安全且留出管理与紧急指令配额，单群限制为每 60 秒最多 8 条，超出的自动排队或延时，避免被风控 1200 拦截
group_sent_times: Dict[int, list[float]] = collections.defaultdict(list)
group_send_locks: Dict[int, asyncio.Lock] = collections.defaultdict(asyncio.Lock)

async def send_group_message(ws: aiohttp.ClientWebSocketResponse, group_id: int, text: str, is_command_report: bool = False, at_user_id: Optional[int] = None):
    """向 NapCat 发送群消息并捕获发送状态（支持可选 @ 对方，并内置群级 60 秒频控保护防 1200 拦截）"""
    if "[PASS]" in text:
        return
    clean_text = strip_markdown(text).strip()
    # 链接混淆防风控
    clean_text = obfuscate_urls_for_qq(clean_text)
    # 过滤末尾句号
    clean_text = remove_trailing_period(clean_text)
    # 普通聊天强控 200 字防水群；管理汇报与 memory 概览解除字数硬截断限制（放宽至 2000 字）
    max_len = 2000 if is_command_report else 200
    if len(clean_text) > max_len:
        clean_text = clean_text[:max_len - 3] + "..."
    if not clean_text:
        return
        
    # 如果指定了目标群友且不是全员指令汇报，前置 [CQ:at,qq=xxx] 艾特对方
    if at_user_id and at_user_id != BOT_QQ and not is_command_report:
        msg_payload = f"[CQ:at,qq={at_user_id}] {clean_text}"
    else:
        msg_payload = clean_text

    async with group_send_locks[group_id]:
        now = time.time()
        # 清理 60 秒之前的发送记录
        group_sent_times[group_id] = [t for t in group_sent_times[group_id] if now - t < 60.0]
        
        # 如果最近 60 秒内已经发了 8 条，进入安全限流等待，避免触发 QQ 风控 (本群每分钟只能发10条)
        if len(group_sent_times[group_id]) >= 8:
            oldest = group_sent_times[group_id][0]
            wait_time = max(0.5, 60.0 - (now - oldest) + 0.5)
            logger.warning("[群 %d] 达到频控安全线 (8条/60s)，主动延时 %.1f 秒再发，防止被 QQ 风控拦截...", group_id, wait_time)
            await asyncio.sleep(wait_time)
            # 唤醒后再次清理
            now = time.time()
            group_sent_times[group_id] = [t for t in group_sent_times[group_id] if now - t < 60.0]

        res = await call_action(ws, "send_group_msg", {"group_id": group_id, "message": msg_payload}, timeout=3.0)
        
        # 如果不幸还是遇到了 299/1200 (每分钟只能发10条)，进行指数退避自动重试
        if res and res.get("status") == "failed" and "每分钟只能发10条" in str(res.get("message", "")):
            logger.warning("[群 %d] 触发 QQ 每分钟 10 条限制！等待 6 秒后自动重试...", group_id)
            await asyncio.sleep(6.0)
            res = await call_action(ws, "send_group_msg", {"group_id": group_id, "message": msg_payload}, timeout=3.0)

        if res and res.get("status") == "ok":
            group_sent_times[group_id].append(time.time())
            logger.info("[群 %d] ✅ 消息成功送达 QQ 群: %s", group_id, msg_payload[:40])
        else:
            logger.warning("[群 %d] ⚠️ 发送消息可能被拦截或异常: %s | 响应: %s", group_id, msg_payload[:40], res)
            # 被动容错熔断：如果响应中明确包含“禁言”或特定错误码，立刻给该群施加 60 秒禁言保护
            res_str = str(res or "").lower()
            if any(k in res_str for k in ["禁言", "banned", "1200"]):
                group_bot_ban_expire[group_id] = time.time() + 60.0
                logger.warning("[群 %d] 检测到发信被禁言拦截，自动开启 60 秒免调用大模型熔断保护！", group_id)

def split_reply_segments(text: str) -> list[str]:
    """智能正则切分回复，生成像真人在 QQ 连续打字发出的 1~3 条短消息"""
    clean = strip_markdown(text).strip()
    clean = remove_trailing_period(clean)
    if not clean:
        return []
    lines = [l.strip() for l in clean.split('\n') if l.strip()]
    if len(lines) > 1:
        return lines[:3]
    if len(clean) <= 16:
        return [clean]
    # 针对中文标点（句号、感叹号、问号、分号）切分
    m = re.split(r'([!！?？~。；\s]{1,3})', clean)
    parts = []
    buf = ''
    for token in m:
        if not token:
            continue
        buf += token
        if re.search(r'[!！?？~。；\s]', token) and len(buf.strip()) >= 5:
            parts.append(buf.strip())
            buf = ''
    if buf.strip():
        parts.append(buf.strip())
    if len(parts) >= 2:
        return parts[:3]
    return [clean]

async def send_segmented_group_message(ws: aiohttp.ClientWebSocketResponse, group_id: int, text: str, is_command_report: bool = False, at_user_id: Optional[int] = None):
    """分段拟真发送：闲聊时智能切分 1~2 条连续发送，root 模式整块发送"""
    # 经过反模板闸削平套路，避免句式坍塌
    channel_mode = get_channel_mode(group_id)
    if not is_command_report and channel_mode != "root":
        text = de_template(text)
    # [MEME:xx] 是给选图用的，正文不能带出去，也不能被切段切开
    text, meme_tag = meme_catalog.extract_tag(text)
    if not text and not meme_tag:
        return
    recent_bot_replies.append(text)
    group_last_bot_reply_time[group_id] = time.time()

    if is_command_report or len(text) > 120 or channel_mode == "root":
        await send_group_message(ws, group_id, text, is_command_report=is_command_report, at_user_id=at_user_id)
        return
        
    # 单群限频检查（防腾讯风控）
    if not check_and_record_group_rate_limit(group_id):
        now = time.time()
        last_warn = group_rate_limit_warned.get(group_id, 0.0)
        if now - last_warn > 30.0:
            group_rate_limit_warned[group_id] = now
            logger.warning("[群 %d] 60s 内已发 %d 条，触发风控红线冷静期", group_id, GROUP_MAX_MESSAGES_PER_WINDOW)
            await send_group_message(ws, group_id, "别排队艾特我啦，让本鲸宝喘口气缓一缓", is_command_report=False)
        return
        
    # 安全护栏过滤
    is_safe, text = safety_guard.sanitize_outgoing_text(text)
    text = safety_guard.strip_internal_monologue(text)
    if not text:
        return

    segments = split_reply_segments(text)
    # 25% 概率或强烈情绪时触发表情包
    should_send_meme = bool(meme_tag) or (random.random() < 0.25) or any(k in text for k in ["大肥鱼", "炸毛", "闹麻了", "草", "无敌了", "才不是", "哼", "气死"])
    # 情绪立绘或表情包选择：如果文本带有强烈情绪，优先匹配状态立绘，否则匹配随机表情包
    status_avatar = None
    if "气" in text or "死" in text or "滚" in text or "闭嘴" in text:
        status_avatar = get_status_avatar_b64("angry")
    elif "害羞" in text or "夸" in text or "爱" in text or "喜欢" in text:
        status_avatar = get_status_avatar_b64("shy")
    elif "为什么" in text or "怎么" in text or "思考" in text:
        status_avatar = get_status_avatar_b64("think")

    final_pic_b64 = status_avatar
    if not final_pic_b64 and should_send_meme:
        # 模型给了情绪就按情绪选，没给就用正文关键词兜底，再不行退回老逻辑
        final_pic_b64 = meme_catalog.pick_b64(meme_tag, text) or get_random_meme_b64(text)

    for i, seg in enumerate(segments):
        target_at = at_user_id if i == 0 else None
        await send_group_message(ws, group_id, seg, is_command_report=False, at_user_id=target_at)
        if i < len(segments) - 1:
            delay = min(1.4, max(0.6, len(seg) * 0.05))
            await asyncio.sleep(delay)
            
    # 文字发完后，如果触发了立绘或表情包，延迟 0.5s 单独甩出一张真实图片
    if final_pic_b64:
        await asyncio.sleep(0.5)
        await send_meme_message(ws, group_id=group_id, b64_data=final_pic_b64)

async def send_private_message(ws: aiohttp.ClientWebSocketResponse, user_id: int, text: str, is_command_report: bool = False):
    """向 NapCat 发送私聊消息并捕获发送状态"""
    if "[PASS]" in text:
        return
    if not is_command_report and get_channel_mode(None) != "root":
        text = de_template(text)
    recent_bot_replies.append(text)

    clean_text = strip_markdown(text).strip()
    clean_text = obfuscate_urls_for_qq(clean_text)
    # 过滤末尾句号
    clean_text = remove_trailing_period(clean_text)
    max_len = 2000 if is_command_report else 200
    if len(clean_text) > max_len:
        clean_text = clean_text[:max_len - 3] + "..."
    if not clean_text:
        return
        
    res = await call_action(ws, "send_private_msg", {"user_id": user_id, "message": clean_text}, timeout=3.0)
    if res and res.get("status") == "ok":
        logger.info("[私聊 %d] ✅ 私信成功送达: %s", user_id, clean_text[:40])
    else:
        logger.warning("[私聊 %d] ⚠️ 私信发送可能异常: %s | 响应: %s", user_id, clean_text[:40], res)

async def generate_agent_command_reply(action_type: str, detail: str = "", is_private: bool = False, group_id: Optional[int] = None) -> str:
    """由大模型根据当前情境临场随机生成拟真执行反馈，杜绝死板模板读条感"""
    headers = {
        "Authorization": f"Bearer {SILICONFLOW_API_KEY}",
        "Content-Type": "application/json"
    }
    
    channel_mode = get_channel_mode(None)
    if channel_mode == "root":
        prompt = (
            f"主人（开发者 life）下达了管理指令：{action_type}，细节：{detail}。\n"
            f"当前是 root 工作模式，请用专业、干练、利落的工程师口吻临场给出 1 句简明执行确认（10~25字以内，拒绝废话，绝不用句号）："
        )
    else:
        prompt = (
            f"主人（life）在{'私聊' if is_private else '群里'}下达了管理指令：{action_type}，执行结果细节：{detail}。\n"
            f"请以大肥鱼/小鲸鱼随性、生动的口吻临场随机给出一句回复（每次措辞自拟，拒绝死板模板，可俏皮、可利落，1 句，15~30字以内，绝不用句号）："
        )

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": ROOT_SYSTEM_PROMPT if get_channel_mode(group_id if not is_private else None) == "root" else SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 50,
        "temperature": 0.85
    }

    try:
        global http_session
        if http_session is None or http_session.closed:
            http_session = aiohttp.ClientSession()
        async with http_session.post(SILICONFLOW_API_URL, headers=headers, json=payload, timeout=8) as resp:
            if resp.status == 200:
                data = await resp.json()
                if "usage" in data:
                    token_tracker.record_usage(data["usage"], MODEL_NAME, group_id=group_id)
                reply = data["choices"][0]["message"]["content"].strip()
                return remove_trailing_period(reply)
    except Exception as e:
        logger.debug("临场生成指令反馈异常: %s", e)
    
    # 极简保底（防止网络超时卡壳）
    return f"已完成 {action_type} ({detail})" if detail else f"已搞定 {action_type}"

# ==================== 管理员快捷指令处理 ====================
async def handle_admin_command(ws: aiohttp.ClientWebSocketResponse, cmd: str, is_private: bool, group_id: Optional[int] = None) -> bool:
    """处理管理员快捷指令并汇报结果（大小写不敏感，秒级汇报）"""
    global ADMIN_MODE
    cmd = cmd.strip()
    cmd_lower = cmd.lower()
    
    # 1. /new - 重置上下文 (支持 /new all 重置所有群与私聊)
    if cmd_lower.startswith("/new"):
        args = cmd.split()
        if len(args) > 1 and args[1].lower() in ["all", "所有", "全部"]:
            group_timelines.clear()
            admin_private_timeline.clear()
            group_history_pool.clear()
            admin_private_history.clear()
            user_follow_up_state.clear()
            group_silence_until.clear()
            reply_text = "已重置所有群与私聊的上下文、历史记录及追问状态"
            if is_private:
                await send_private_message(ws, ADMIN_QQ, reply_text)
            else:
                await send_group_message(ws, group_id, reply_text)
            return True
        if is_private:
            admin_private_timeline.clear()
            admin_private_history.clear()
            reply_text = "已清空私聊上下文与历史池"
            await send_private_message(ws, ADMIN_QQ, reply_text)
        else:
            group_timelines[group_id].clear()
            group_history_pool[group_id].clear()
            # 清空该群追问状态并解除静音
            for k in list(user_follow_up_state.keys()):
                if k[0] == group_id:
                    user_follow_up_state.pop(k, None)
            group_silence_until.pop(group_id, None)
            reply_text = f"已重置本群 ({group_id}) 上下文与发言历史池"
            await send_group_message(ws, group_id, reply_text)
        return True

    # 2. /stop - 一键紧急刹车中止所有思考与排队任务并开启静默
    elif cmd_lower.startswith("/stop"):
        stopped_count = 0
        # 1. 取消所有群的防抖任务
        for gid, task in list(group_debounce_tasks.items()):
            if task and not task.done():
                task.cancel()
                stopped_count += 1
        # 2. 取消所有运行中的大模型思考/生成请求
        for task in list(running_tasks):
            if not task.done():
                task.cancel()
                stopped_count += 1
                
        # 3. 清理追问窗口并开启静默保护，杜绝刚叫停又自动插话
        if group_id:
            for k in list(user_follow_up_state.keys()):
                if k[0] == group_id:
                    user_follow_up_state.pop(k, None)
            group_silence_until[group_id] = time.time() + 600.0  # 10 分钟静默期
            group_msg_count_since_bot[group_id] = 0
            reply_text = f"已一键急刹车，中止了 {stopped_count} 个任务，并开启本群静默（不再主动插话）" if stopped_count > 0 else "当前无进行中任务，已清空追问并开启本群静默，保持安静"
        else:
            user_follow_up_state.clear()
            for gid in ALLOWED_GROUPS:
                group_silence_until[gid] = time.time() + 600.0
            reply_text = f"已一键急刹车，中止了 {stopped_count} 个任务，并开启全局群静默" if stopped_count > 0 else "当前无进行中任务，已清空追问并开启全局群静默"

        if is_private:
            await send_private_message(ws, ADMIN_QQ, reply_text)
        else:
            await send_group_message(ws, group_id, reply_text)
        return True

    # 3. /root - 切换到工作模式 (支持按群独立切换)
    elif cmd_lower.startswith("/root"):
        target_gid = None
        parts = cmd.split()
        if len(parts) > 1 and parts[1].isdigit():
            target_gid = int(parts[1])
        elif not is_private:
            target_gid = group_id

        update_group_config(target_gid, "mode", "root")
        desc = f"群 {target_gid} 已切换至 root 技术伙伴模式" if target_gid else "私聊已切换至 root 技术伙伴模式"
        reply_text = await generate_agent_command_reply("切换工作模式", desc, is_private=is_private, group_id=target_gid)
        if is_private:
            await send_private_message(ws, ADMIN_QQ, reply_text)
        else:
            await send_group_message(ws, group_id, reply_text)
        return True

    # 4. /user - 切换回肥鱼娱乐模式 (支持按群独立切换)
    elif cmd_lower.startswith("/user"):
        target_gid = None
        parts = cmd.split()
        if len(parts) > 1 and parts[1].isdigit():
            target_gid = int(parts[1])
        elif not is_private:
            target_gid = group_id

        update_group_config(target_gid, "mode", "user")
        desc = f"群 {target_gid} 已切回日常大肥鱼人设" if target_gid else "私聊已切回日常大肥鱼人设"
        reply_text = await generate_agent_command_reply("切换娱乐模式", desc, is_private=is_private, group_id=target_gid)
        if is_private:
            await send_private_message(ws, ADMIN_QQ, reply_text)
        else:
            await send_group_message(ws, group_id, reply_text)
        return True

    # 5. /memes 或 /reload_memes - 手动重载与刷新网络热梗轮换池
    elif cmd_lower.startswith("/reload_memes") or cmd_lower.startswith("/memes"):
        meme_manager.load_pool(force=True)
        count = len(meme_manager.current_pool)
        total = len(meme_manager.all_memes)
        reply_text = f"热梗词库已重新洗牌刷新，当前轮换池: {count} 条，总词库: {total} 条"
        if is_private:
            await send_private_message(ws, ADMIN_QQ, reply_text)
        else:
            await send_group_message(ws, group_id, reply_text)
        return True

    return False

async def handle_memory_summary_command(ws: aiohttp.ClientWebSocketResponse, group_id: Optional[int] = None, user_id: Optional[int] = None):
    """公开指令: /memory 或 /memary 生成当前群友记忆库与好感度总结（按群隔离）"""
    gid_key = str(group_id) if group_id else "private"
    current_scope = user_memories.get(gid_key, {})
    if not current_scope:
        report = "当前群聊暂无群友记忆记录" if group_id else "私聊暂无记忆记录"
    else:
        title = f"本群群友记忆档案（共 {len(current_scope)} 位）：" if group_id else f"私聊记忆档案（共 {len(current_scope)} 位）："
        lines = [title]
        for uid, info in current_scope.items():
            nick = info.get("nickname", uid)
            favor = info.get("favor", 50)
            notes = "、".join(info.get("notes", [])) if info.get("notes") else "无特殊印象"
            # Galgame 好感度评级
            admin_suffix = " (主人)" if uid == str(ADMIN_QQ) else ""
            if favor >= 80:
                status = "💖恋慕/宠溺"
            elif favor >= 65:
                status = "😊亲近/喜欢"
            elif favor >= 40:
                status = "😐普通群友"
            else:
                status = "💢炸毛/嫌弃"
            lines.append(f"• {nick} (QQ {uid}){admin_suffix}: 好感度 {favor}/100 [{status}] | 印象: {notes}")
        report = "\n".join(lines)
        
    if group_id:
        await send_group_message(ws, group_id, report, is_command_report=True)
    elif user_id:
        await send_private_message(ws, user_id, report, is_command_report=True)

# ==================== 消息事件处理 ====================
async def process_group_trigger(ws: aiohttp.ClientWebSocketResponse, group_id: int, is_at: bool, sender_nick: str, is_admin: bool = False, is_mention: bool = False, target_uid: Optional[int] = None, user_text: str = ""):
    """汇总群时间线向模型发起决策"""
    timeline = group_timelines[group_id]
    if not timeline and not user_text:
        return
        
    # 轻量未读消息机制：群聊闲聊仅传当前发言/未读上下文，杜绝滚雪球历史堆叠，降低 90% Token 并防认错人
    # 如果是 root 模式则提供完整时间线供技术分析，否则仅传递当前群友最新发言
    channel_mode = get_channel_mode(group_id)
    if is_admin and channel_mode == "root":
        dialogue_text = "\n".join([f"{m['sender']}: {m['content']}" for m in timeline])
    else:
        dialogue_text = user_text or (timeline[-1]['content'] if timeline else "")
    
    logger.info("[群 %d] 触发轻量会话分析 (is_at=%s, is_admin=%s), 发言内容: %s", group_id, is_at, is_admin, dialogue_text[:40])
    
    current_task = asyncio.current_task()
    if current_task:
        running_tasks.add(current_task)
        
    try:
        reply = await call_deepseek(dialogue_text, is_at=is_at, is_admin=is_admin, is_private=False, is_mention=is_mention, target_uid=target_uid, sender_nick=sender_nick, group_id=group_id)
    finally:
        if current_task:
            running_tasks.discard(current_task)
            
    if not reply:
        return
        
    # 如果回复中带有 [PASS]（或者模型啰嗦解释了为什么保持沉默）
    # 只要判定包含 [PASS]，不管后面带不带括号解释，都必须彻底保持静默！
    if "[PASS]" in reply:
        logger.info("[群 %d] 模型输出包含 [PASS]，保持绝对静默不发送群消息。", group_id)
        return
        
    # 额外拦截模型常见的内心括号自我独白（如“（根据规则保持沉默）”、“（保持静默）”）
    if any(silent_kw in reply for silent_kw in ["保持沉默", "保持静默", "无需回复", "不便插话", "选择沉默"]):
        logger.info("[群 %d] 拦截到模型静默解释独白: %s，直接丢弃。", group_id, reply)
        return

    # 旁观插话时（非 @ 且非管理员）：若模型生成的内容属于没搞懂语境的反问、敷衍套话，一律丢弃
    if not is_at and not is_admin:
        confused_patterns = [
            r"你在说[啥什么]", r"什么意思[呀呢啊]?", r"不知道你在说", r"没搞[懂明]",
            r"没听懂", r"谁进不去", r"哪款游戏", r"发生了什么事[呀呢啊]?", r"你在聊[啥什么]"
        ]
        if any(re.search(pat, reply) for pat in confused_patterns):
            logger.info("[群 %d] 拦截到旁观插话中没搞懂的困惑反问套话: %s，保持静默直接丢弃", group_id, reply)
            return
        
    # 机器人每次在群里发话后，重置该群的消息计数
    group_msg_count_since_bot[group_id] = 0
    if not is_at and not is_admin:
        group_last_active_reply[group_id] = time.time()
    
    await asyncio.sleep(1.0)
    # 当群友主动艾特机器人呼叫时，回复必须带上 [CQ:at] 艾特该群友；若为主动旁观插话则自然接话不强行艾特
    at_target = target_uid if is_at else None
    await send_segmented_group_message(ws, group_id, reply, is_command_report=(is_admin and get_channel_mode(group_id) == "root"), at_user_id=at_target)

    # 将机器人自己的回复也写入当前群的时间线与长历史池，让对话上下文完整，防止机器人自言自语或遗忘自己说过的话
    # 存进上下文的必须是纯正文，[MEME:xx] 这种内部标记不能回灌给模型
    clean_reply_record = remove_trailing_period(strip_markdown(meme_catalog.extract_tag(reply)[0])).strip()
    if clean_reply_record:
        bot_record = {
            "sender": "小鲸鱼",
            "content": clean_reply_record,
            "time": time.time()
        }
        group_timelines[group_id].append(bot_record)
        group_history_pool[group_id].append(bot_record)

    # 成功发回后，更新追问窗口状态
    if target_uid:
        # 异步更新当前群友的记忆与好感度（结合群友发言与机器人回复由 AI 动态判定）
        content_to_analyze = user_text or (timeline[-1]["content"] if timeline else "")
        asyncio.create_task(update_user_memory_async(target_uid, sender_nick, content_to_analyze, group_id=group_id, bot_reply=clean_reply_record))

async def handle_group_message(ws: aiohttp.ClientWebSocketResponse, event: dict):
    """群消息主处理流水线"""
    group_id = event.get("group_id")
    sender_id = event.get("user_id")
    sender_nick = event.get("sender", {}).get("card") or event.get("sender", {}).get("nickname") or str(sender_id)
    sender_title = (event.get("sender", {}).get("title") or "").strip()
    sender_display = f"{sender_nick}【头衔: {sender_title}】" if sender_title else sender_nick
    raw_message = event.get("raw_message", "")
    
    if group_id not in ALLOWED_GROUPS or sender_id == BOT_QQ:
        return
        
    # 前置短路检查：如果机器人在该群处于禁言期（单人禁言或全员禁言），直接静默拦截，绝对不浪费 1 个 Token
    if await check_bot_banned_realtime(ws, group_id):
        logger.debug("[群 %d] 机器人处于禁言保护期，忽略用户 %s 的消息，省下大模型 Token", group_id, sender_id)
        return
        
    is_admin = (sender_id == ADMIN_QQ)
    now = time.time()
    
    # 提取纯文本与精准 @ 判定（基于 OneBot 11 规范的 QQ 号精准判定）
    # 匹配针对机器人自己的 @ CQ 码（兼容任何可选属性）
    is_at = bool(re.search(rf"\[CQ:at,[^\]]*qq={BOT_QQ}[^\]]*\]", raw_message))
    
    # 也检查 OneBot11 原始 message 数组节点（结构化消息最稳健）
    if not is_at and isinstance(event.get("message"), list):
        for seg in event["message"]:
            if isinstance(seg, dict) and seg.get("type") == "at":
                target_qq = str(seg.get("data", {}).get("qq", ""))
                if target_qq == str(BOT_QQ):
                    is_at = True
                    break
                
    # 打印原始接收日志，方便精准观测
    logger.info("[群 %d] 收到原始消息 raw_message=%r, is_at=%s", group_id, raw_message, is_at)
    
    # 累加该群自上次机器人发言以来的消息条数
    group_msg_count_since_bot[group_id] += 1

    # 捕获群聊中的图片进入两段式近期图片缓冲池，并尝试进行超轻量视觉转录
    m_imgs = re.findall(r'\[CQ:image,[^\]]*url=([^ control\,\]]+)', raw_message)
    if m_imgs:
        for u in m_imgs:
            u_clean = u.replace("&amp;", "&")
            group_recent_images[group_id].append((u_clean, time.time(), sender_nick))
            # 异步看图提炼画面梗概，融入聊天上下文
            if http_session:
                asyncio.create_task(resolve_image_and_record(http_session, group_id, sender_nick, u_clean))

    # 记录群友艾特或交互到关系网
    m_ats = re.findall(r'\[CQ:at,[^\]]*qq=(\d+)[^\]]*\]', raw_message)
    for target_qq_str in m_ats:
        try:
            t_qq = int(target_qq_str)
            if t_qq != BOT_QQ:
                cross_interaction.record_interaction(group_id, sender_id, sender_nick, t_qq, target_qq_str)
        except Exception:
            pass

    # 解析引用消息与合并转发
    reply_prefix = await extract_reply_context(ws, event, raw_message)
    raw_message = await expand_forward_messages(ws, raw_message)

    # 深度解析主流媒体短链 (抖音/小红书/B站短链)
    if http_session and ("douyin.com" in raw_message or "xhslink.com" in raw_message or "b23.tv" in raw_message):
        try:
            media_info = await media_resolver.parse_media_content(http_session, raw_message)
            if media_info and media_info.get("summary"):
                raw_message = f"{raw_message}\n{media_info['summary']}"
        except Exception:
            pass

    # 清洗掉针对机器人的 @ CQ 码以及其他无关 CQ
    clean_body = re.sub(rf"\[CQ:at,[^\]]*qq={BOT_QQ}[^\]]*\]", "", raw_message)
    clean_body = clean_cq_codes(clean_body).strip()
    # 移除 reply 的 CQ 码残留
    clean_body = re.sub(r"\[CQ:reply,id=-?\d+\]", "", clean_body).strip()

    # 如果用户只 @ 了机器人，没有附带文字，给一个默认问候
    if is_at and not clean_body:
        clean_body = "在吗"
    elif not clean_body:
        return

    # 游戏指令或云崽生态专用指令，交给云崽等扩展插件处理，AI 机器人主动跳过，彻底避免抢答与刷屏
    if is_game_or_yunzai_command(clean_body):
        logger.debug("[群 %d] 识别为游戏指令: %r，主动转交云崽处理，跳过 AI 回复", group_id, clean_body)
        return

    # 4. 安全护栏：拦截提示词注入/越狱指令，以傲娇拟真口吻反讽
    is_injected, snarky_reply = safety_guard.check_prompt_injection(clean_body)
    if is_injected and snarky_reply:
        logger.info("[群 %d] 拦截群友 %s 的提示词注入/越狱请求: %s", group_id, sender_nick, clean_body)
        await send_segmented_group_message(ws, group_id, snarky_reply, at_user_id=sender_id)
        return

    # 规范化指令前缀与小写，兼容全角斜杠与输入法大小写
    norm_cmd = clean_body.replace("／", "/").strip()
    norm_lower = norm_cmd.lower()

    # 公开指令检测: /memory 或 /memary（所有人均可查看好感度与记忆总结）
    if norm_lower.startswith(("/memary", "/memory")):
        await handle_memory_summary_command(ws, group_id=group_id)
        return

    # 1. 轻量文字游戏（免状态：roll、抽签、抛硬币）拦截
    cfg = get_group_config(group_id)
    if cfg.get("enable_games", True):
        quick_game_reply = games.handle_quick_game_commands(clean_body)
        if quick_game_reply:
            await send_group_message(ws, group_id, quick_game_reply, at_user_id=sender_id)
            return

        # 2. 数字炸弹状态机拦截
        handled_bomb, bomb_reply = games.process_number_bomb(group_id, clean_body, sender_id, sender_nick)
        if handled_bomb and bomb_reply:
            await send_group_message(ws, group_id, bomb_reply, at_user_id=sender_id)
            return

    # 3. 两段式搜番/搜动画截图出处拦截
    if re.search(r"(?:什么番|哪部番|谁画的|出处|搜番|搜截图|找番)", clean_body):
        # 寻找群内 90 秒内的有效图片
        now_ts = time.time()
        target_img_url = None
        for img_u, img_t, img_sender in reversed(group_recent_images[group_id]):
            if now_ts - img_t < 90.0:
                target_img_url = img_u
                break
        if target_img_url and http_session:
            anime_info = await connectors.search_anime_by_image(http_session, target_img_url)
            if anime_info and not anime_info.get("uncertain"):
                rep = (
                    f"🎬 帮你看出来啦！这是动画《{anime_info['title']}》\n"
                    f"第 {anime_info['episode']} 集 · 画面大概在 {anime_info['time']}\n"
                    f"（匹配相似度: {anime_info['similarity']}%）"
                )
                await send_group_message(ws, group_id, rep, at_user_id=sender_id)
                return
            elif anime_info and anime_info.get("uncertain"):
                await send_group_message(ws, group_id, f"👀 好像是动画截图，但画面不够典型，我不太确信是不是《{anime_info.get('data', {}).get('anilist', {}).get('title', {}).get('native', '某部番')}》", at_user_id=sender_id)
                return

    # 管理员快捷指令检测（忽略大小写，兼容全角斜杠与常用刹车词）
    if is_admin and (
        norm_lower.startswith(("/new", "/stop", "/root", "/user"))
        or norm_lower in ("stop", "new", "闭嘴", "停", "暂停")
    ):
        target_cmd = norm_cmd
        if norm_lower in ("stop", "闭嘴", "停", "暂停"):
            target_cmd = "/stop"
        elif norm_lower == "new":
            target_cmd = "/new"
        handled = await handle_admin_command(ws, target_cmd, is_private=False, group_id=group_id)
        if handled:
            return

    # 管理员在群里直接下达管理命令拦截 (无需切 root 模式，支持随时修改/清除/重置指定群友的 memory 与好感度)
    if is_admin:
        # 1. 清除/重置某人 memory
        # 兼容: 重置3650199442memary / 清除 3650199442 memory / 删掉群友 3650199442 记忆 / 清空3650199442
        m_clear = re.search(r"(?:清除|删掉|清空|重置)\s*(?:用户|群友)?\s*(\d{5,11})\s*(?:的)?\s*(?:memory|memary|记忆|档案)?", clean_body, re.IGNORECASE)
        # 或者反过来匹配: memory 清除 3650199442 / 重置记忆 3650199442
        if not m_clear:
            m_clear = re.search(r"(?:memory|memary|记忆|档案)?\s*(?:清除|删掉|清空|重置)\s*(?:用户|群友)?\s*(\d{5,11})", clean_body, re.IGNORECASE)
        if m_clear:
            target_qq = int(m_clear.group(1))
            cleared = clear_single_user_memory(target_qq, group_id=group_id)
            if cleared:
                report = f"已清除本群群友 (QQ {target_qq}) 的记忆档案"
            else:
                report = f"记忆库中暂无 (QQ {target_qq}) 的记录"
            await send_group_message(ws, group_id, report, is_command_report=True)
            return

        # 2. 增减某人好感度: 给 123456 加 10 点好感度 / 扣除 123456 20 好感度 / 把 123456 好感度 +5 / 扣 123456 好感 10
        m_delta = re.search(r"(?:把|给|针对|对)?\s*(?:用户|群友)?\s*(\b\d{5,11}\b)\s*(?:的)?\s*(?:好感度|好感|favor)?\s*(加|增加|提升|涨|上升|\+|减|扣除|扣|降低|跌|下降|\-)\s*(\d+)", clean_body, re.IGNORECASE)
        if not m_delta:
            m_delta = re.search(r"(加|增加|提升|涨|上升|\+|减|扣除|扣|降低|跌|下降|\-)\s*(?:用户|群友)?\s*(\b\d{5,11}\b)\s*(?:的)?\s*(?:好感度|好感|favor)?\s*(\d+)", clean_body, re.IGNORECASE)
            if m_delta:
                op_sign = m_delta.group(1)
                target_qq = int(m_delta.group(2))
                delta_val = int(m_delta.group(3))
            else:
                m_delta = re.search(r"(?:好感度|好感|favor)?\s*(加|增加|提升|涨|上升|\+|减|扣除|扣|降低|跌|下降|\-)\s*(\d+)\s*(?:给|针对|对)?\s*(?:用户|群友)?\s*(\b\d{5,11}\b)", clean_body, re.IGNORECASE)
                if m_delta:
                    op_sign = m_delta.group(1)
                    delta_val = int(m_delta.group(2))
                    target_qq = int(m_delta.group(3))
        else:
            target_qq = int(m_delta.group(1))
            op_sign = m_delta.group(2)
            delta_val = int(m_delta.group(3))

        if m_delta:
            is_sub = any(s in op_sign for s in ["减", "扣", "降低", "跌", "下降", "-"])
            actual_delta = -delta_val if is_sub else delta_val
            mem = get_or_create_user_memory(target_qq, group_id=group_id)
            old_f = mem.get("favor", 50)
            new_f = max(0, min(100, old_f + actual_delta))
            mem["favor"] = new_f
            mem["updated_at"] = time.time()
            save_user_memories()
            action_desc = f"扣除 {delta_val}" if is_sub else f"增加 {delta_val}"
            report = f"已为 (QQ {target_qq}) {action_desc} 点好感度，当前好感度: {old_f} -> {new_f}/100"
            await send_group_message(ws, group_id, report, is_command_report=True)
            return

        # 3. 绝对值修改某人好感度: 改 123456 的好感度为 80 / 把 123456 好感度调至 0 / 设为 100 / 3650199442 好感度 80 / 调成 0 / 改为 0
        # 兼容正序与倒序: "把298152965好感度调至0" 或 "把好感度调至0 298152965"
        m_favor = re.search(r"(?:把|将|改|设置)?\s*(?:用户|群友)?\s*(\b\d{5,11}\b)\s*(?:的)?\s*(?:好感度|favor)?\s*(?:设为|改为|变更为|调至|调成|调为|调节为|设置为|为|\=|\s+)\s*(\d+)", clean_body, re.IGNORECASE)
        if not m_favor:
            m_favor = re.search(r"(?:好感度|favor)\s*(?:设为|改为|变更为|调至|调成|调为|调节为|设置为|为|\=|\s+)\s*(\d+)\s*(?:给|针对|对)?\s*(?:用户|群友)?\s*(\b\d{5,11}\b)", clean_body, re.IGNORECASE)
            if m_favor:
                # 倒序形式: group(1)是数值, group(2)是QQ
                new_favor = max(0, min(100, int(m_favor.group(1))))
                target_qq = int(m_favor.group(2))
                mem = get_or_create_user_memory(target_qq, group_id=group_id)
                mem["favor"] = new_favor
                mem["updated_at"] = time.time()
                save_user_memories()
                report = f"已将 (QQ {target_qq}) 的好感度设为 {new_favor}/100"
                await send_group_message(ws, group_id, report, is_command_report=True)
                return
        if m_favor:
            target_qq = int(m_favor.group(1))
            new_favor = max(0, min(100, int(m_favor.group(2))))
            mem = get_or_create_user_memory(target_qq, group_id=group_id)
            mem["favor"] = new_favor
            mem["updated_at"] = time.time()
            save_user_memories()
            report = f"已将 (QQ {target_qq}) 的好感度设为 {new_favor}/100"
            await send_group_message(ws, group_id, report, is_command_report=True)
            return

    # 拟真作息睡眠系统 (01:00 ~ 07:00 CST 为入睡期)
    if is_bot_sleeping():
        if is_admin:
            # 主人专属唤醒特权
            wake_words = ["醒醒", "起床", "别睡了", "起来了", "起来", "快醒醒", "别睡啦", "上工", "开工", "干活"]
            is_wake = any(w in clean_body for w in wake_words) or clean_body.startswith(("/root", "/user"))
            if is_wake:
                sleep_override_until = now + 1800.0  # 临时唤醒 30 分钟
                logger.info("[作息系统] 主人 %s 触发专属叫醒词，大肥鱼临时唤醒 30 分钟接活！", sender_nick)
                wake_reply = random.choice([
                    "(迷迷糊糊揉眼睛，从被窝钻出来) 主人我醒啦，大半夜怎么啦～",
                    "(打了个长长的哈欠) 呼...主人叫我呀，我醒了！有啥急事快吩咐～",
                    "(被子一掀瞬间精神) 到！主人我在呢，大半夜需要我做什么呀～"
                ])
                await send_group_message(ws, group_id, wake_reply, at_user_id=sender_id)
                return
            # 主人正常交谈自动唤醒接活
            sleep_override_until = now + 1800.0
        else:
            # 普通群友在睡眠期（选项 B：睡意梦话提示并继续睡）
            if is_at or bool(re.search(r"\[CQ:reply,id=-?\d+\]", raw_message)):
                key = (group_id, sender_id)
                last_sleep_reply = user_sleep_reply_cd.get(key, 0.0)
                if now - last_sleep_reply > 180.0:
                    user_sleep_reply_cd[key] = now
                    sleepy_quips = [
                        "呼呼...别吵我睡觉（拉起被子翻身继续睡）",
                        "唔...好困，明天睡醒再说啦 (打哈欠潜水)",
                        "呼噜噜...zzz (闭着眼睛吐了个泡泡)",
                        "大半夜不睡觉抓鱼是吧...困死了不理你 (把头缩进被窝)",
                        "呼...困得眼睛睁不开，明天白天再找我玩嘛 (翻身)"
                    ]
                    await send_group_message(ws, group_id, random.choice(sleepy_quips), at_user_id=sender_id)
                return
            else:
                # 旁观消息静默入睡，绝不主动插话
                return

    # 拼装包含引用的文本
    if reply_prefix:
        full_content = f"{reply_prefix} {clean_body}"
    else:
        full_content = clean_body
        
    # 超时重置时间线 (5 分钟未活动)
    if now - group_last_activity[group_id] > INACTIVITY_TIMEOUT:
        group_timelines[group_id].clear()
    group_last_activity[group_id] = now
    
    prefix = "[管理员] " if is_admin else ""
    record_item = {
        "sender": f"{prefix}{sender_display}",
        "content": full_content,
        "time": now
    }
    group_timelines[group_id].append(record_item)
    group_history_pool[group_id].append(record_item)
    # 异步非阻塞推入跨群公共话题（仅内存+脏标记）
    cross_memory.cross_group_mgr.record_topic(group_id, sender_display, full_content)
    logger.info("[群 %d][%s] %s%s: %s", group_id, "被@" if is_at else "群聊", prefix, sender_display, full_content)
    
    # 检查是否直接呼叫机器人：显式 @ 机器人，或者回复引用了机器人之前的发言
    is_reply_to_bot = False
    if "reply" in event and isinstance(event["reply"], dict):
        is_reply_to_bot = (event["reply"].get("sender", {}).get("user_id") == BOT_QQ)
    
    # 检查是否明确 @ 了群里的其他人（非机器人）
    at_other_match = re.findall(r"\[CQ:at,[^\]]*qq=(\d+)[^\]]*\]", raw_message)
    is_at_other = any(int(q) != BOT_QQ for q in at_other_match)
    
    # 检查是否引用回复了其他人（非机器人）
    is_reply_to_other = False
    if "reply" in event and isinstance(event["reply"], dict):
        reply_sender = event["reply"].get("sender", {}).get("user_id")
        if reply_sender and reply_sender != BOT_QQ:
            is_reply_to_other = True

    # 检查是否处于连续对话追问窗口（5分钟内免 @ 回答 5 次，有新的 @ 则重置）
    user_conv_key = (group_id, sender_id)
    follow_up_info = user_follow_up_state.get(user_conv_key)
    is_follow_up = False

    if is_at_other or is_reply_to_other:
        # 该用户正在明确与群里的其他人对话，主动释放追问态，绝不插话打扰
        if user_conv_key in user_follow_up_state:
            user_follow_up_state.pop(user_conv_key, None)
            logger.info("[群 %d] 用户 %s 明确与他人互动，主动退出追问窗口", group_id, sender_nick)
    elif is_at or is_reply_to_bot:
        # 有新的显式 @ 或回复机器人：直接重置为 5 分钟 + 5 次追问配额！
        user_follow_up_state[user_conv_key] = {
            "expire": now + FOLLOW_UP_WINDOW,
            "remain": FOLLOW_UP_MAX_COUNT
        }
        logger.info("[群 %d] 用户 %s 触发新 @，追问窗口重置为 %.0f 秒 / %d 次免 @ 配额", group_id, sender_nick, FOLLOW_UP_WINDOW, FOLLOW_UP_MAX_COUNT)
    elif follow_up_info and now < follow_up_info.get("expire", 0.0):
        # 处于 5 分钟窗口内，检查剩余次数配额
        remain_cnt = follow_up_info.get("remain", 0)
        if remain_cnt > 0:
            is_follow_up = True
            follow_up_info["remain"] = remain_cnt - 1
            left_secs = follow_up_info["expire"] - now
            logger.info("[群 %d] 用户 %s 命中免 @ 追问（剩余可用配额 %d 次，窗口剩余 %.1fs）", group_id, sender_nick, remain_cnt - 1, left_secs)
        else:
            logger.info("[群 %d] 用户 %s 5 分钟内 5 次免 @ 追问已耗尽，需重新 @ 激活", group_id, sender_nick)
    
    # 如果明确 @ 了别人，即便带了其他触发也不立即回复
    if is_at_other:
        should_reply_immediately = False
    else:
        should_reply_immediately = is_at or is_reply_to_bot or is_follow_up

    # 1. 直接呼叫模式（@、引用机器人 或 专属追问中）：立即触发响应
    if should_reply_immediately:
        if not is_admin:
            # 疲劳度衰减检查：防止同一群友短时间内连续疯狂 @ 调戏
            is_fatigued, is_first_trigger = check_user_fatigue(group_id, sender_id)
            if is_fatigued:
                if is_first_trigger:
                    tired_quips = [
                        "哎呀别连环艾特啦，本鱼头好大要歇会儿 (翻身潜水)",
                        "歇会儿歇会儿，你手不酸我嘴都酸了！(闭麦60秒)",
                        "吵死啦！本大肥鱼也是需要休息的，等会再理你 (溜走)",
                        "连着喊我不累嘛，本鲸宝要去喝口水冷静一下"
                    ]
                    await send_group_message(ws, group_id, random.choice(tired_quips), at_user_id=sender_id)
                logger.info("[群 %d] 用户 %s 触发疲劳防调戏限制，本次略过", group_id, sender_nick)
                return

            key = (group_id, sender_id)
            last_at = user_last_at_time.get(key, 0.0)
            # 追问状态下防抖设为 2.0 秒，非追问常规被@为 2.5 秒基础冷却
            cd = 2.0 if is_follow_up else DIRECT_AT_CD
            if now - last_at < cd:
                logger.info("[群 %d] 用户 %s 处于冷却中（%.1f秒），略过。", group_id, sender_nick, cd)
                return
            user_last_at_time[key] = now
        
        task = group_debounce_tasks.get(group_id)
        if task and not task.done():
            task.cancel()

        asyncio.create_task(process_group_trigger(ws, group_id, is_at=True, sender_nick=sender_display, is_admin=is_admin, target_uid=sender_id, user_text=clean_body))
        return
        
    # 2. 旁观模式（未直接找机器人）：严格门控过滤，绝不乱插嘴
    # 过滤短语无意义废话
    if is_low_info_message(clean_body):
        group_last_msg_time[group_id] = now
        return
        
    # 检查管理员 /stop 设置的群主动插话静默保护期
    silence_exp = group_silence_until.get(group_id, 0.0)
    if now < silence_exp:
        logger.debug("[群 %d] 处于管理员 /stop 静默保护期（剩余 %.0fs），保持静默不插话", group_id, silence_exp - now)
        return

    # 意图门控：结合冷热场动态权重、代称、讨论疑问词综合判定插话
    should_trigger, is_mention_bot = should_active_interrupt(clean_body, group_id=group_id, msg_count_since=group_msg_count_since_bot[group_id])
    group_last_msg_time[group_id] = now
    if not should_trigger:
        return
        
    # 冷却时间门控：若未直接提及机器人且处于 CD 中，保持静默（若直接提及机器人或代称，CD 缩短为 3 秒；普通插话 10 秒）
    min_cd = 3.0 if is_mention_bot else 10.0
    if now - group_last_active_reply[group_id] < min_cd:
        logger.info("[群 %d] 主动插话处于冷却期 (%.0fs)，保持静默。", group_id, min_cd)
        return
        
    async def debounced_step(target_gid: int, nick: str, mention_flag: bool, uid: int, text_body: str):
        try:
            # 缩短至 1.0 秒轻微防抖，接话更迅速不被冲走
            await asyncio.sleep(1.0)
            await process_group_trigger(ws, target_gid, is_at=False, sender_nick=nick, is_admin=False, is_mention=mention_flag, target_uid=uid, user_text=text_body)
        except asyncio.CancelledError:
            pass

    old_task = group_debounce_tasks.get(group_id)
    if old_task and not old_task.done():
        old_task.cancel()
    group_debounce_tasks[group_id] = asyncio.create_task(debounced_step(group_id, sender_display, is_mention_bot, sender_id, clean_body))

async def call_deepseek_poke(sender_nick: str, is_admin: bool, group_id: Optional[int] = None) -> Optional[str]:
    """戳一戳的回应。

    为了让一句「戳我干嘛」不背着两千多字的 System Prompt 去烧一次 API，
    普通群友直接走本地语料；只有 root 模式才真的问模型（那里要判断有没有活干）。
    """
    channel_mode = get_channel_mode(group_id)
    is_root_work = bool(is_admin and channel_mode == "root")

    if not is_root_work:
        corpus = _POKE_ADMIN_QUIPS if is_admin else _POKE_QUIPS
        quip = random.choice(corpus)
        logger.info("戳一戳本地回应（不走模型）: %s", quip)
        return quip

    headers = {
        "Authorization": f"Bearer {SILICONFLOW_API_KEY}",
        "Content-Type": "application/json"
    }
    user_prompt = (
        "主人在群里戳了你一下。当前是 root 工作模式，简明回应主人有什么任务要安排（1 句，10~20 字，严禁演戏）："
    )
    sys_content = ROOT_SYSTEM_PROMPT
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": user_prompt}
        ],
        "max_tokens": 60,
        "temperature": 0.7
    }
    
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession()

    t0 = time.time()
    try:
        async with http_session.post(SILICONFLOW_API_URL, headers=headers, json=payload, timeout=15) as resp:
            elapsed = time.time() - t0
            if resp.status != 200:
                logger.error("戳一戳 API 调用失败 (%d, 耗时 %.2fs)", resp.status, elapsed)
                return None
            data = await resp.json()
            if "usage" in data:
                token_tracker.record_usage(data["usage"], MODEL_NAME, group_id=group_id)
            reply = data["choices"][0]["message"]["content"].strip()
            logger.info("戳一戳动态响应完成 (耗时 %.2fs): %s", elapsed, reply[:30])
            return reply
    except Exception as e:
        logger.error("戳一戳调用大模型异常: %s", e)
        return None

# ==================== 群通知与状态事件处理 ====================
async def handle_notice_event(ws: aiohttp.ClientWebSocketResponse, event: dict):
    """处理戳一戳、群禁言、退群解散等系统通知事件"""
    notice_type = event.get("notice_type") or event.get("post_type")
    sub_type = event.get("sub_type")
    group_id = event.get("group_id")
    
    # 1. 监测群禁言事件 (group_ban) -> 杜绝禁言期间调用模型浪费 Token
    if notice_type == "group_ban":
        user_id = event.get("user_id", 0)
        duration = event.get("duration", 0)  # 禁言秒数，0 表示解禁
        operator_id = event.get("operator_id")
        
        # 1.1 全员禁言事件 (user_id 为 0)
        if user_id == 0:
            if sub_type == "ban" or duration > 0:
                group_all_banned[group_id] = True
                logger.warning("[群 %d] 管理员 %s 开启了全员禁言，机器人进入静默节流状态！", group_id, operator_id)
            else:
                group_all_banned[group_id] = False
                logger.info("[群 %d] 管理员 %s 解除了全员禁言，恢复响应。", group_id, operator_id)
            return

        # 1.2 针对机器人自身的禁言
        if user_id == BOT_QQ:
            if sub_type == "ban" or duration > 0:
                expire_time = time.time() + duration
                group_bot_ban_expire[group_id] = expire_time
                logger.warning("[群 %d] 机器人被管理员 %s 禁言 %d 秒！禁言期内将静默拦截所有大模型调用。", group_id, operator_id, duration)
            elif sub_type == "lift_ban" or duration == 0:
                group_bot_ban_expire.pop(group_id, None)
                logger.info("[群 %d] 机器人被管理员 %s 提前解禁！恢复正常响应。", group_id, operator_id)
            return

    # 2. 监测机器人被踢或退群事件 (group_decrease)
    if notice_type == "group_decrease" and event.get("user_id") == BOT_QQ:
        logger.warning("[群 %d] 机器人已退出或被移出群聊！清理该群状态...", group_id)
        group_bot_ban_expire.pop(group_id, None)
        group_all_banned.pop(group_id, None)
        return

    # 3. 监测戳一戳事件 (poke)
    if sub_type != "poke":
        return
        
    target_id = event.get("target_id")
    user_id = event.get("user_id")
    
    # 只响应：白名单群中，戳的目标是机器人自身 (BOT_QQ)
    if not group_id or group_id not in ALLOWED_GROUPS:
        return
    if target_id != BOT_QQ:
        return
        
    # 如果机器人此时处于禁言状态，戳一戳也不回复，绝不浪费 Token
    if await check_bot_banned_realtime(ws, group_id):
        logger.debug("[群 %d] 机器人处于禁言中，忽略戳一戳", group_id)
        return
        
    now = time.time()
    is_admin = (user_id == ADMIN_QQ)
    
    # 戳一戳防刷冷却（普通群友 5 秒 CD，管理员 0 秒）
    if not is_admin:
        last_poke = user_last_poke_time.get((group_id, user_id), 0.0)
        if now - last_poke < 5.0:
            logger.info("[群 %d] 用户 %s 戳一戳处于冷却中，忽略。", group_id, user_id)
            return
        user_last_poke_time[(group_id, user_id)] = now

    cfg = get_group_config(group_id)
    if not cfg.get("enable_poke", True):
        logger.info("[群 %d] 戳一戳互动已禁用，略过", group_id)
        return

    # 拟真睡眠作息检查：若正在睡觉被戳
    if is_bot_sleeping(group_id):
        if is_admin:
            sleep_override_until = now + 1800.0  # 主人戳一戳直接唤醒 30 分钟
            logger.info("[作息系统] 主人戳一戳叫醒大肥鱼！")
        else:
            logger.info("[群 %d] 处于睡眠期，被群友 %s 戳一戳，触发翻身梦话反应", group_id, user_id)
            await asyncio.sleep(0.5)
            sleep_poke = random.choice([
                "(闭着眼睛把尾巴缩进被窝，翻身继续睡)",
                "(被戳得翻了个身，砸吧砸吧嘴没醒)",
                "呼噜...别戳啦，在睡觉呢 (扯被子盖住头)"
            ])
            await send_group_message(ws, group_id, sleep_poke, at_user_id=user_id)
            return

    logger.info("[群 %d] 机器人被 %s 戳一戳！触发动态傲娇回复...", group_id, f"管理员 {user_id}" if is_admin else f"群友 {user_id}")
    
    # 异步调用模型生成傲娇动态回复
    nick = "管理员" if is_admin else "群友"
    reply = await call_deepseek_poke(nick, is_admin=is_admin, group_id=group_id)
    if reply:
        await asyncio.sleep(0.5)
        # 被戳一戳时，文字回复前置 @ 对方
        await send_group_message(ws, group_id, reply, at_user_id=user_id)
        # 戳一戳互动后，开启针对该用户的 5 分钟 / 5 次免 @ 追问配额
        user_follow_up_state[(group_id, user_id)] = {
            "expire": time.time() + FOLLOW_UP_WINDOW,
            "remain": FOLLOW_UP_MAX_COUNT
        }
        # 戳一戳也算亲密互动，异步微调好感度
        asyncio.create_task(update_user_memory_async(user_id, nick, "戳了戳小鲸鱼", group_id=group_id))

    # 戳回去！调用 OneBot 11 group_poke 动作回戳对方
    try:
        await asyncio.sleep(0.3)
        poke_res = await call_action(ws, "group_poke", {"group_id": group_id, "user_id": user_id}, timeout=3.0)
        logger.info("[群 %d] 傲娇回戳用户 %s 完成: %s", group_id, user_id, poke_res)
    except Exception as e:
        logger.warning("[群 %d] 回戳用户 %s 失败: %s", group_id, user_id, e)

# ==================== 管理员私聊处理 ====================
async def handle_private_message(ws: aiohttp.ClientWebSocketResponse, event: dict):
    """管理员私聊流水线"""
    sender_id = event.get("user_id")
    raw_message = event.get("raw_message", "")
    
    if sender_id != ADMIN_QQ:
        return
        
    reply_prefix = await extract_reply_context(ws, event, raw_message)
    raw_message = await expand_forward_messages(ws, raw_message)
    
    clean_body = clean_cq_codes(raw_message).strip()
    clean_body = re.sub(r"\[CQ:reply,id=-?\d+\]", "", clean_body).strip()
    
    if not clean_body:
        return
        
    # 游戏指令或云崽专属指令私聊拦截
    if is_game_or_yunzai_command(clean_body):
        logger.debug("[私聊] 识别为游戏/Cookie指令: %r，交由云崽处理，跳过 AI 回复", clean_body)
        return

    # 规范化指令前缀与小写，兼容全角斜杠与输入法大小写
    norm_cmd = clean_body.replace("／", "/").strip()
    norm_lower = norm_cmd.lower()

    # 公开指令检测: /memory 或 /memary（私聊支持查看）
    if norm_lower.startswith(("/memary", "/memory")):
        await handle_memory_summary_command(ws, user_id=ADMIN_QQ)
        return

    # 管理员私聊直接自动唤醒睡眠状态 30 分钟
    global sleep_override_until
    if is_bot_sleeping():
        sleep_override_until = time.time() + 1800.0
        logger.info("[作息系统] 主人发起私聊，大肥鱼自动解除睡眠状态 30 分钟！")

    # 管理员指令拦截
    if (
        norm_lower.startswith(("/new", "/stop", "/root", "/user"))
        or norm_lower in ("stop", "new", "闭嘴", "停", "暂停")
    ):
        target_cmd = norm_cmd
        if norm_lower in ("stop", "闭嘴", "停", "暂停"):
            target_cmd = "/stop"
        elif norm_lower == "new":
            target_cmd = "/new"
        handled = await handle_admin_command(ws, target_cmd, is_private=True)
        if handled:
            return

    # 管理员私聊直接下达生产命令拦截 (无需切 root 模式，随时可管理任意群友好感度与记忆)
    if True:
        # 1. 清除某人记忆
        m_clear = re.search(r"(?:清除|删掉|清空|重置)\s*(?:用户|群友)?\s*(\d{5,11})\s*(?:的)?\s*(?:memory|memary|记忆|档案)?", clean_body, re.IGNORECASE)
        if not m_clear:
            m_clear = re.search(r"(?:memory|memary|记忆|档案)?\s*(?:清除|删掉|清空|重置)\s*(?:用户|群友)?\s*(\d{5,11})", clean_body, re.IGNORECASE)
        if m_clear:
            target_qq = int(m_clear.group(1))
            cleared = clear_single_user_memory(target_qq, group_id=None)
            if cleared:
                report = f"已清除群友 (QQ {target_qq}) 的全部记忆档案"
            else:
                report = f"记忆库中暂无 (QQ {target_qq}) 的记录"
            await send_private_message(ws, ADMIN_QQ, report, is_command_report=True)
            return

        # 2. 增减某人好感度: 给 123456 加 10 点好感度 / 扣除 123456 20 好感度 / 把 123456 好感度 +5 / 扣 123456 好感 10
        m_delta = re.search(r"(?:把|给|针对|对)?\s*(?:用户|群友)?\s*(\b\d{5,11}\b)\s*(?:的)?\s*(?:好感度|好感|favor)?\s*(加|增加|提升|涨|上升|\+|减|扣除|扣|降低|跌|下降|\-)\s*(\d+)", clean_body, re.IGNORECASE)
        if not m_delta:
            m_delta = re.search(r"(加|增加|提升|涨|上升|\+|减|扣除|扣|降低|跌|下降|\-)\s*(?:用户|群友)?\s*(\b\d{5,11}\b)\s*(?:的)?\s*(?:好感度|好感|favor)?\s*(\d+)", clean_body, re.IGNORECASE)
            if m_delta:
                op_sign = m_delta.group(1)
                target_qq = int(m_delta.group(2))
                delta_val = int(m_delta.group(3))
            else:
                m_delta = re.search(r"(?:好感度|好感|favor)?\s*(加|增加|提升|涨|上升|\+|减|扣除|扣|降低|跌|下降|\-)\s*(\d+)\s*(?:给|针对|对)?\s*(?:用户|群友)?\s*(\b\d{5,11}\b)", clean_body, re.IGNORECASE)
                if m_delta:
                    op_sign = m_delta.group(1)
                    delta_val = int(m_delta.group(2))
                    target_qq = int(m_delta.group(3))
        else:
            target_qq = int(m_delta.group(1))
            op_sign = m_delta.group(2)
            delta_val = int(m_delta.group(3))

        if m_delta:
            is_sub = any(s in op_sign for s in ["减", "扣", "降低", "跌", "下降", "-"])
            actual_delta = -delta_val if is_sub else delta_val
            mem = get_or_create_user_memory(target_qq, group_id=None)
            old_f = mem.get("favor", 50)
            new_f = max(0, min(100, old_f + actual_delta))
            mem["favor"] = new_f
            mem["updated_at"] = time.time()
            save_user_memories()
            action_desc = f"扣除 {delta_val}" if is_sub else f"增加 {delta_val}"
            report = f"已为 (QQ {target_qq}) {action_desc} 点好感度，当前好感度: {old_f} -> {new_f}/100"
            await send_private_message(ws, ADMIN_QQ, report, is_command_report=True)
            return

        # 3. 绝对值修改某人好感度
        m_favor = re.search(r"(?:把|将|改|设置)?\s*(?:用户|群友)?\s*(\b\d{5,11}\b)\s*(?:的)?\s*(?:好感度|favor)?\s*(?:设为|改为|变更为|调至|调成|调为|调节为|设置为|为|\=|\s+)\s*(\d+)", clean_body, re.IGNORECASE)
        if not m_favor:
            m_favor = re.search(r"(?:好感度|favor)\s*(?:设为|改为|变更为|调至|调成|调为|调节为|设置为|为|\=|\s+)\s*(\d+)\s*(?:给|针对|对)?\s*(?:用户|群友)?\s*(\b\d{5,11}\b)", clean_body, re.IGNORECASE)
            if m_favor:
                new_favor = max(0, min(100, int(m_favor.group(1))))
                target_qq = int(m_favor.group(2))
                mem = get_or_create_user_memory(target_qq, group_id=None)
                mem["favor"] = new_favor
                mem["updated_at"] = time.time()
                save_user_memories()
                report = f"已将 (QQ {target_qq}) 的好感度设为 {new_favor}/100"
                await send_private_message(ws, ADMIN_QQ, report, is_command_report=True)
                return
        if m_favor:
            target_qq = int(m_favor.group(1))
            new_favor = max(0, min(100, int(m_favor.group(2))))
            mem = get_or_create_user_memory(target_qq, group_id=None)
            mem["favor"] = new_favor
            mem["updated_at"] = time.time()
            save_user_memories()
            report = f"已将 (QQ {target_qq}) 的好感度设为 {new_favor}/100"
            await send_private_message(ws, ADMIN_QQ, report, is_command_report=True)
            return

    full_content = f"{reply_prefix} {clean_body}" if reply_prefix else clean_body
    logger.info("[私聊] 主人 life 来信: %s", full_content)
    admin_private_timeline.append(f"life: {full_content}")
    admin_private_history.append(f"life: {full_content}")
    
    timeline_str = "\n".join(admin_private_timeline)
    current_task = asyncio.current_task()
    if current_task:
        running_tasks.add(current_task)
    try:
        reply = await call_deepseek(timeline_str, is_at=True, is_admin=True, is_private=True)
    finally:
        if current_task:
            running_tasks.discard(current_task)
            
    if not reply:
        return
        
    # 私聊只发正文，内部标记剥掉，免得主人收到一句 [MEME:卖萌]
    reply, _ = meme_catalog.extract_tag(reply)
    if not reply:
        return
    admin_private_timeline.append(f"小鲸鱼: {reply}")
    admin_private_history.append(f"小鲸鱼: {reply}")
    await asyncio.sleep(0.8)
    await send_private_message(ws, ADMIN_QQ, reply, is_command_report=(get_channel_mode(None) == "root"))

# ==================== Web 仪表盘状态导出 ====================
def get_bot_dashboard_status() -> Dict[str, Any]:
    """生成供给轻量 Web 仪表盘的实时快照数据 (按群独立配置)"""
    now = time.time()
    cst_tz = datetime.timezone(datetime.timedelta(hours=8))
    start_dt = datetime.datetime.fromtimestamp(BOT_START_TIME, tz=cst_tz)

    groups_data = []
    for gid in sorted(ALLOWED_GROUPS):
        cfg = get_group_config(gid)
        last_msg = group_last_msg_time.get(gid, 0.0)
        last_reply = group_last_bot_reply_time.get(gid, 0.0)
        msg_count = group_msg_count_since_bot.get(gid, 0)
        base_p = float(cfg.get("interject_prob", 0.35))
        prob, why = calculate_interject_probability(gid, base_p)
        g_name = group_names_cache.get(gid, f"群聊 {gid}")
        groups_data.append({
            "group_id": gid,
            "group_name": g_name,
            "config": cfg,
            "last_msg_ago": (now - last_msg) if last_msg > 0 else -1,
            "last_bot_reply_ago": (now - last_reply) if last_reply > 0 else -1,
            "msg_count_since": msg_count,
            "interject_prob": prob,
            "interject_why": why
        })

    return {
        "bot": {
            "qq": BOT_QQ,
            "admin_qq": ADMIN_QQ,
            "model": MODEL_NAME,
            "admin_mode": get_channel_mode(None),
            "is_sleeping": is_bot_sleeping(),
            "sleep_override": (now < sleep_override_until),
            "uptime": (now - BOT_START_TIME),
            "start_time_str": start_dt.strftime("%Y-%m-%d %H:%M:%S")
        },
        "token_usage": token_tracker.get_status_summary(),
        "groups": groups_data,
        "configs": group_configs,
        "memories": user_memories
    }

# ==================== 主连接循环 ====================
async def main():
    logger.info("启动 QQ DeepSeek 群聊 & 私聊机器人...")
    logger.info("白名单群: %s | 机器人 QQ: %d | 模型: %s | 管理员: %d", list(ALLOWED_GROUPS), BOT_QQ, MODEL_NAME, ADMIN_QQ)

    # 启动轻量现代浅色 Web 仪表盘 (监听 0.0.0.0:8088)
    try:
        await web_dashboard.start_web_server(get_bot_dashboard_status, update_config_func=update_group_config, port=8088)
        cross_memory.cross_group_mgr.start_background_flusher()
        logger.info("🌐 Web 仪表盘已成功启动并监听: http://0.0.0.0:8088")
    except Exception as e:
        logger.error("Web 仪表盘启动异常: %s", e)
    
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                logger.info("正在连接 NapCat OneBot 11 (%s)...", NAPCAT_WS_URL)
                async with session.ws_connect(NAPCAT_WS_URL) as ws:
                    global current_ws
                    current_ws = ws
                    logger.info("✅ 成功连接到 NapCat WebSocket！开始监听消息事件。")
                    # 异步刷新各群名称
                    async def refresh_group_names():
                        try:
                            res = await call_action(ws, "get_group_list", {}, timeout=3.0)
                            if res and res.get("status") == "ok" and isinstance(res.get("data"), list):
                                for item in res["data"]:
                                    gid = item.get("group_id")
                                    gname = item.get("group_name")
                                    if gid and gname:
                                        group_names_cache[gid] = gname
                                logger.info("成功同步 %d 个群的群名称缓存: %s", len(group_names_cache), group_names_cache)
                        except Exception as ge:
                            logger.debug("获取群列表名称异常: %s", ge)
                    asyncio.create_task(refresh_group_names())

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                event = json.loads(msg.data)
                                # 检查是否是 API 回调
                                echo_id = event.get("echo")
                                if echo_id and echo_id in pending_requests:
                                    fut = pending_requests[echo_id]
                                    if not fut.done():
                                        fut.set_result(event)
                                    continue
                                    
                                post_type = event.get("post_type")
                                if post_type == "message":
                                    msg_type = event.get("message_type")
                                    if msg_type == "group":
                                        asyncio.create_task(handle_group_message(ws, event))
                                    elif msg_type == "private":
                                        asyncio.create_task(handle_private_message(ws, event))
                                elif post_type in ("notice", "notify"):
                                    # 监听群戳一戳通知
                                    logger.info("收到系统/群通知事件: %s", event)
                                    asyncio.create_task(handle_notice_event(ws, event))
                            except Exception as e:
                                logger.error("解析事件异常: %s", e)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.warning("WebSocket 连接关闭或异常，准备重连...")
                            break
        except Exception as e:
            logger.error("连接失败或发生错误: %s，5秒后自动重试...", e)
            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("服务已手动退出。")
