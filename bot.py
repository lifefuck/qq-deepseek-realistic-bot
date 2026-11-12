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
import signal
import base64
import re
import sys
import time
import datetime
import uuid
import glob
import shutil
from typing import Any, Deque, Dict, Optional, Set, Tuple
import aiohttp
import web_dashboard
import games
import connectors
import cross_memory
import fun_features
import media_resolver
from meme_manager import meme_manager
from prompts import (SYSTEM_PROMPT, GAMES_KB, needs_games_kb,
                     repo_ask_reply, _POKE_QUIPS, _POKE_ADMIN_QUIPS,
                     _POKE_FAVOR_HIGH, _POKE_FAVOR_MID, _POKE_FAVOR_LOW)
import meme_catalog
import safety_guard
import cross_interaction
import memory_atoms
from services import trace
from services import affect
from services import anti_template
from services import interject_planner
from services.messaging import (send_group_message, send_segmented_group_message,
                              send_private_message, send_meme_message, thin_emoji,
                              send_forward_message, build_forward_nodes)
from services.llm import (call_deepseek, call_deepseek_poke, evaluate_favor_with_ai,
                          _is_favor_worthy, update_user_memory_async)
from handlers.admin import (handle_admin_command, handle_memory_summary_command,
                            generate_agent_command_reply)
from handlers.private import handle_private_message
from handlers.group import handle_group_message, process_group_trigger
import services.messaging as _svc_messaging
import services.llm as _svc_llm
import handlers.admin as _h_admin
import handlers.private as _h_private
import handlers.group as _h_group
import handlers.actions as _h_actions

# 把当前模块交给服务层。python bot.py 跑起来时 __main__ 和 bot 是两个不同的模块对象，
# 服务层自己 import bot 会读到另一份全局状态，所以只认这里注入的实例。
for _mod in (_svc_messaging, _svc_llm, _h_admin, _h_private, _h_group, _h_actions, fun_features):
    _mod.bind(sys.modules[__name__])

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
DISABLE_THINKING = os.environ.get("DISABLE_THINKING", "0").lower() in ("1", "true", "yes")
# 日常群聊的默认档位：low 已经够它读懂上下文，不比关思考慢多少
DEFAULT_EFFORT = os.environ.get("DEEPSEEK_EFFORT", "low")

def think_effort(is_at: bool = False, is_admin: bool = False, is_mention: bool = False,
                 is_follow_up: bool = False, text: str = "") -> str:
    """按场景给思考档位：闲聊 low 保速度，真在问事/被点名才 high。

    关掉思考的 flash 只会复述模板句（实测 reasoning_tokens=0），
    但全开 max 会让一句「笑死」也磨半天，群里节奏就废了。
    """
    if text:
        q = text.strip()
        # 问事、算法、要答案的，才值得多花那几百毫秒
        if any(k in q for k in ("怎么", "为什么", "为啥", "是什么", "多少", "哪个", "?", "？", "帮我", "查一下", "写个", "算")):
            return "high"
    if is_at or is_mention or is_follow_up or is_admin:
        return "high"
    return os.environ.get("DEEPSEEK_EFFORT", "low")


def get_base_chat_payload(model: str, messages: list, max_tokens: int, temperature: float = 0.8,
                          effort: Optional[str] = None) -> dict:
    """构建统一请求载荷：默认带思考档位，关思考只留作应急开关"""
    # 思考/推理模型判定
    is_reasoning_model = any(k in model.lower() for k in ("r1", "reasoner", "flash", "pro", "cot"))

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature
    }

    if DISABLE_THINKING:
        # 应急专用：显式设 DISABLE_THINKING=1 才走这里，省 token 但说话会变模板
        payload["thinking"] = {"type": "disabled"}
        payload["max_tokens"] = max_tokens
    else:
        payload["reasoning_effort"] = effort or DEFAULT_EFFORT
        # 带思考时 content 前面可能先烧 reasoning token，留够量免得正文被截空
        safe_tokens = max(max_tokens, 240) if is_reasoning_model else max_tokens
        payload["max_tokens"] = safe_tokens

    return payload

# 行为与节奏参数
TIMELINE_MAX_LEN = 30           # 群时间线保留最近消息条数（8 条只够看半场，接到的话常常对不上前情）
PRIVATE_TIMELINE_MAX_LEN = 14   # 管理员私聊时间线保留条数
INACTIVITY_TIMEOUT = 900.0      # 超过 15 分钟未说话才重置时间线（5 分钟会把半场对话洗掉）
DEBOUNCE_DELAY = 2.0            # 主动插话防抖 2.0 秒
ACTIVE_INTERRUPT_CD = 45.0      # 主动插话冷却 45 秒
DIRECT_AT_CD = 2.5              # 被 @ 时的用户级基础冷却（秒）
FOLLOW_UP_WINDOW = 300.0        # 持续对话追问窗口为 5 分钟 (300 秒)
FOLLOW_UP_MAX_COUNT = 5         # 追问窗口内最多免 @ 回答 5 次，有新的 @ 则重置配额
MAX_OUTPUT_TOKENS = 200         # 限制单次生成长度，防长篇大论

# 拟真作息睡眠参数 (01:00 ~ 07:00 CST 为入睡期)
SLEEP_START_HOUR = 1
SLEEP_END_HOUR = 7
sleep_override_until: float = 0.0          # 唤醒生效截止时间戳（保留扩展）
user_sleep_reply_cd: Dict[tuple, float] = collections.defaultdict(float)  # 限制半夜对单个群友每 3 分钟最多回一句梦话

# 疲劳度衰减参数 (防群友连续狂 @ 调戏)
user_mention_burst: Dict[tuple, Deque[float]] = collections.defaultdict(lambda: collections.deque(maxlen=10))
user_fatigue_until: Dict[tuple, float] = collections.defaultdict(float)

# 群聊冷热场时间戳追踪 (活跃期提权 + 冷场降权)
group_last_msg_time: Dict[int, float] = collections.defaultdict(float)        # 群内上一条消息时间
group_last_bot_reply_time: Dict[int, float] = collections.defaultdict(float)  # 机器人在该群上次说话时间

# 群成员昵称缓存：群名片 -> QQ。时间线只有最近 8 条，光靠它认不出几小时前
# 说过话的人，模型就只能对着 QQ 号念数字（「3803303988 是哪个」）
group_member_nicks: Dict[int, Dict[int, str]] = {}
# 冷场抛话记录：同一句梗四小时后再抛就成了复读机
group_last_nudge: Dict[int, str] = collections.defaultdict(str)
# 冷场冒泡配额：每群每天最多两次，且上次冒泡后没人接话就不再开口
group_nudge_day: Dict[int, str] = {}
group_nudge_count: Dict[int, int] = {}
group_last_nudge_ts: Dict[int, float] = collections.defaultdict(float)
# 主动插话时间戳：一小时最多开口几次，防止每个话题都凑一句
group_interject_times: Dict[int, Deque[float]] = collections.defaultdict(lambda: collections.deque(maxlen=40))
# 带时间戳的已发回复：10 分钟内同句复读直接丢弃（「啊？」「什么鬼」这种最像机器人）
group_recent_replies: Dict[int, Deque[Tuple[float, str]]] = collections.defaultdict(lambda: collections.deque(maxlen=20))
# 本群实际发言时间戳：连着刷三四条会被群管理盯上，用来做发言密度冷却
group_bot_reply_times: Dict[int, Deque[float]] = collections.defaultdict(lambda: collections.deque(maxlen=20))
group_bot_reply_seq: Dict[int, int] = collections.defaultdict(int)  # 只增不减，用来判断「这次真的发出去了」
_pre_off_snapshot: Dict[int, dict] = {}  # /off 关掉的那几项原值，/on 时照原样还原

# 反模板套路削平队列 (记录最近发出的 6 条回复)
# 每个群各自记一份最近发言，跨群混在一起会让 A 群看到 B 群说过的话
recent_bot_replies: Dict[int, Deque[str]] = collections.defaultdict(lambda: collections.deque(maxlen=8))

# 自称「本大肥鱼/本鲸宝」这类，属于人设词空转，盯的是它
_SELF_REF_RE = re.compile(r"本(?:大)?(?:肥)?(?:鱼|鲸宝|鲸鱼|鲸)|我(?:大)?(?:肥鱼|鲸宝)|大肥鱼本(?:鱼|人)")

BOT_KEYWORDS = {
    "大肥鱼", "肥鱼", "小鲸鱼", "鲸鱼", "鲸宝", "deepseek", "v3", "r1", 
    "bot", "机器人", "ai", "人工智障", "人工智能", "什么生物", "回消息", "理人",
    "笨蛋", "傻子",
    "爸爸", "主人", "开发者", "老父亲", "创造者"
}

# 日志格式：rid 放在最前面，一眼能把一条消息的全程串起来
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(rid)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("DeepSeekBot")
trace.install()
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
    "deepseek-v4.1-flash-1017": {
        "name": "DeepSeek-V4.1-Flash (TaoToken)",
        "provider": "TaoToken / 中转",
        "offpeak": {"input": 0.27, "output": 1.35},
        "peak": {"input": 0.27, "output": 1.35}
    },
    "deepseek-v4.1-flash": {
        "name": "DeepSeek-V4.1-Flash (api-top)",
        "provider": "api-top / 国模",
        "offpeak": {"input": 2.06, "output": 8.23},
        "peak": {"input": 2.06, "output": 8.23}
    },
    "gemini-3.8-flash": {
        "name": "Gemini 3.8 Flash (api-top)",
        "provider": "api-top / 中转",
        "offpeak": {"input": 0.27, "output": 1.35},
        "peak": {"input": 0.27, "output": 1.35}
    },
    "gemini-3.1-flash-lite": {
        "name": "Gemini 3.1 Flash Lite (api-top)",
        "provider": "api-top / 中转",
        "offpeak": {"input": 0.27, "output": 1.35},
        "peak": {"input": 0.27, "output": 1.35}
    },
    "deepseek-flash": {
        "name": "DeepSeek-V4.1-Flash",
        "provider": "DeepSeek 官方",
        "offpeak": {"input": 1.0, "output": 4.0},
        "peak": {"input": 2.0, "output": 8.0}
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
        self._last_save = 0.0
        self._unknown_models: Set[str] = set()

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
            "by_group": {},
            "history": {}
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
            atomic_write_json(self.file_path, self.data)
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

        pricing = DEEPSEEK_MODELS_PRICING.get(model_name)
        if pricing is None:
            # 没登记的模型按 V3 估价，但必须留下告警，否则账目会悄悄失真
            if model_name not in self._unknown_models:
                self._unknown_models.add(model_name)
                logger.warning("模型 %s 未配置定价，暂按 DeepSeek-V3 单价估算，请补 PRICING 表", model_name)
            pricing = DEEPSEEK_MODELS_PRICING["deepseek-ai/DeepSeek-V3"]
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
            # 跨天前先把昨天整块归档，不然“今日”一重置，历史账单就永远查不到了
            if today_data.get("date"):
                self.data.setdefault("history", {})[today_data["date"]] = dict(today_data)
                for old_day in sorted(self.data["history"])[:-30]:
                    self.data["history"].pop(old_day, None)
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

        # 每次调用都重写账本纯属浪费，攒 5 秒批量落盘
        now_ts = time.time()
        if now_ts - self._last_save >= 5.0:
            self.save()
            self._last_save = now_ts

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

def atomic_write_json(path: str, data: Any, *, pretty: bool = True) -> None:
    """唯一临时名 + fsync + 原子替换。

    固定 .tmp 名字在两次写入重叠时会互相踩；断电时也可能把半截文件 replace 上去。
    临时名带 pid 和随机串，写完强制刷盘再替换，坏文件的概率能压到最低。
    """
    tmp_path = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            if pretty:
                json.dump(data, f, ensure_ascii=False, indent=2)
            else:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def backup_json(path: str, keep: int = 3, min_interval: float = 600.0) -> None:
    """覆盖前留快照，同一条文件 10 分钟内只留一份，防止刷出一堆备份"""
    if not os.path.exists(path):
        return
    try:
        olds = sorted(glob.glob(f"{path}.bak-*"))
        if olds and time.time() - os.path.getmtime(olds[-1]) < min_interval:
            return
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, f"{path}.bak-{stamp}")
        for old in sorted(glob.glob(f"{path}.bak-*"))[:-keep]:
            os.remove(old)
    except Exception as e:
        logger.warning("备份 %s 失败: %s", os.path.basename(path), e)


def load_json_with_recovery(path: str) -> Optional[dict]:
    """读 JSON，主文件坏了就回退最近的备份。

    读不出来必须返回 None，绝不能返回空字典：调用方拿空字典一保存，
    磁盘上的整库档案就被抹掉了。
    """
    for candidate in [path] + sorted(glob.glob(f"{path}.bak-*"), reverse=True):
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                logger.error("%s 结构异常（不是对象），跳过", os.path.basename(candidate))
                continue
            if candidate != path:
                logger.warning("主文件 %s 损坏，已从备份 %s 恢复", os.path.basename(path), os.path.basename(candidate))
                shutil.copy2(candidate, path)
            return data
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.error("读取 %s 失败: %s", os.path.basename(candidate), e)
    return None


user_memories: Dict[str, Dict[str, dict]] = {}
_memory_load_failed = False       # 读到坏档时置位，本次运行禁止覆盖磁盘
_mem_dirty = False
_mem_last_save = 0.0
MEMORY_SAVE_INTERVAL = 10.0       # 高频事件合并落盘窗口，别每次好感判定都重写整库

def load_user_memories():
    """从磁盘加载按群隔离的群友记忆库，主档坏了自动吃备份"""
    global user_memories, _memory_load_failed
    if not os.path.exists(MEMORY_FILE_PATH):
        user_memories = {}
        return
    raw_data = load_json_with_recovery(MEMORY_FILE_PATH)
    if raw_data is None:
        _memory_load_failed = True
        user_memories = {}
        logger.error("群友记忆库读不出来，已进入只读保护；本次运行不会覆盖磁盘档案")
        return
    # 兼容旧版本平铺结构，如果是平铺则自动迁移或初始化
    if raw_data and not any(isinstance(v, dict) and any(isinstance(sub_v, dict) for sub_v in v.values()) for v in raw_data.values() if isinstance(v, dict)):
        user_memories = {"default": raw_data}
    else:
        user_memories = raw_data
    total_users = sum(len(users) for users in user_memories.values() if isinstance(users, dict))
    # 老档案的 notes 是字符串列表，进内存前统一拆成原子（拆串/去重/补等级），
    # 不然第一轮注入就会因为类型不对炸掉
    cleaned = 0
    for scope in user_memories.values():
        if not isinstance(scope, dict):
            continue
        for mem in scope.values():
            if not isinstance(mem, dict) or "notes" not in mem:
                continue
            before = len(mem["notes"]) if isinstance(mem["notes"], list) else 0
            mem["notes"] = memory_atoms.migrate(mem.get("notes"))
            cleaned += max(0, before - len(mem["notes"]))
    if cleaned:
        logger.info("记忆原子化完成，合并/拆分了 %d 条重复印象", cleaned)
    logger.info("已成功加载按群隔离记忆库，共 %d 个分区，%d 位群友档案！", len(user_memories), total_users)

def save_user_memories(force: bool = False):
    """记忆库落盘：默认攒 10 秒合并写一次，force=True 立即写。"""
    global _mem_dirty, _mem_last_save
    if _memory_load_failed:
        logger.error("记忆库处于只读保护，跳过保存，避免覆盖磁盘原始档案")
        return
    now = time.time()
    if not force and now - _mem_last_save < MEMORY_SAVE_INTERVAL:
        _mem_dirty = True
        return
    try:
        backup_json(MEMORY_FILE_PATH)
        atomic_write_json(MEMORY_FILE_PATH, user_memories, pretty=False)
        _mem_dirty = False
        _mem_last_save = now
    except Exception as e:
        _mem_dirty = True
        logger.error("保存群友记忆库失败: %s", e)

def flush_user_memories():
    """把攒下来的改动真正写盘，退出和后台循环用"""
    if _mem_dirty:
        save_user_memories(force=True)

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
            "notes": memory_atoms.migrate(["专属主人兼创造者 life"]) if is_admin_user else [],
            "updated_at": time.time()
        }
    # 只在首次建档时给主人写默认身份，之后和普通群友一样按真实互动演进；
    # 以前每次调用都强刷 notes/traits，等于把主人的印象永久锁死在一句话上。
    # 只认真正的昵称：戳一戳传的「群友/管理员」、显示名里的「【头衔: x】」「[机器人]」都不是名字，
    # 以前这些会被原样写进记忆，把人改名成「群友」
    clean_nick = re.sub(r"[【\[][^】\]]{0,20}[】\]]", "", nickname or "").strip()
    if clean_nick in ("群友", "管理员", "未知"):
        clean_nick = ""
    if not is_admin_user and clean_nick and group_scope[uid_str].get("nickname") != clean_nick:
        group_scope[uid_str]["nickname"] = clean_nick
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




# 模块加载时直接读取记忆库
load_user_memories()

memory_write_lock = asyncio.Lock()


# ==================== 状态存储（按群隔离） ====================
# 群聊近期图片缓存池 (用于两段式搜番与以图搜图): group_id -> deque of (image_url, timestamp, sender_id, sender_nick)
group_recent_images: Dict[int, Deque[Tuple[str, float, int, str]]] = collections.defaultdict(lambda: collections.deque(maxlen=10))

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
# 已处理消息 id：NapCat 重连补历史、QQ 侧重推都会把同一条消息再投一次。
# 不挡住的话模型会再答一遍，而模型是非确定性的，群里就出现两条不同的回答。
_seen_msg_ids: "collections.OrderedDict[int, float]" = collections.OrderedDict()
MSG_DEDUPE_TTL = 300.0
MSG_DEDUPE_MAX = 3000


def is_duplicate_message(msg_id, scope=None) -> bool:
    """这条消息是不是刚处理过？顺手把过期记录清出去，字典不会无限长。

    key 带上会话作用域：不同群的 message_id 各自编号，撞号时不能互相当成重复。
    """
    if not msg_id:
        return False
    try:
        key = (str(scope), int(msg_id))
    except (TypeError, ValueError):
        return False
    now = time.time()
    first_seen = _seen_msg_ids.get(key)
    if first_seen is not None and now - first_seen < MSG_DEDUPE_TTL:
        return True
    _seen_msg_ids[key] = now
    _seen_msg_ids.move_to_end(key)
    while _seen_msg_ids:
        oldest_id, ts = next(iter(_seen_msg_ids.items()))
        if now - ts < MSG_DEDUPE_TTL and len(_seen_msg_ids) <= MSG_DEDUPE_MAX:
            break
        _seen_msg_ids.popitem(last=False)
    return False
# 同群生成互斥：连点两次 @ 不能同时跑两个模型请求，否则会各读一份时间线、重复回复
group_gen_locks: Dict[int, asyncio.Lock] = collections.defaultdict(asyncio.Lock)
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
ALL_BANNED_TTL = 6 * 3600   # 全员禁言最多记 6 小时，超时自动失效并要求重新核实
group_all_banned: Dict[int, float] = collections.defaultdict(float)     # group_id -> 全员禁言的过期时间戳
last_active_ban_check: Dict[int, float] = {}   # group_id -> 上次主动通过 API 校验的时间戳

# ==================== 运行时保护状态持久化 ====================
# /stop 的静默期、禁言熔断、追问窗口都带过期时间，重启清零等于把刚生效的保护也清了，
# 所以存一份 runtime_state.json，启动时按时间戳过滤恢复。
RUNTIME_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime_state.json")

def save_runtime_state() -> None:
    try:
        atomic_write_json(RUNTIME_STATE_FILE, {
            "silence_until": {str(k): v for k, v in group_silence_until.items()},
            "follow_up": {f"{k[0]}:{k[1]}": v for k, v in user_follow_up_state.items()},
            "ban_expire": {str(k): v for k, v in group_bot_ban_expire.items()},
            "all_banned": {str(k): v for k, v in group_all_banned.items() if v},
        }, pretty=False)
    except Exception as e:
        logger.warning("保存运行时状态失败: %s", e)

def load_runtime_state() -> None:
    data = load_json_with_recovery(RUNTIME_STATE_FILE)
    if not data:
        return
    now = time.time()
    # 这个函数的设计意图是"文件坏了也要能起来"：状态可以丢，进程不能崩。
    # 所以逐条兜异常——键名不是数字、值不是时间戳、结构被手改过都不能抛到 main()

    def _as_dict(value):
        """本该是字典的字段被写成 list/str 时统一降级成空字典，否则 .items() 直接崩"""
        return value if isinstance(value, dict) else {}

    for gid, exp in _as_dict(data.get("silence_until")).items():
        try:
            if isinstance(exp, (int, float)) and exp > now:
                group_silence_until[int(gid)] = float(exp)
        except (TypeError, ValueError):
            logger.warning("运行时状态 silence_until 条目损坏，已跳过: %r", gid)
    for key, state in _as_dict(data.get("follow_up")).items():
        try:
            if not isinstance(state, dict) or state.get("expire", 0) <= now:
                continue
            gid_str, uid_str = str(key).split(":", 1)
            user_follow_up_state[(int(gid_str), int(uid_str))] = state
        except (AttributeError, TypeError, ValueError):
            logger.warning("运行时状态 follow_up 条目损坏，已跳过: %r", key)
    for gid, exp in _as_dict(data.get("ban_expire")).items():
        try:
            if isinstance(exp, (int, float)) and exp > now:
                group_bot_ban_expire[int(gid)] = float(exp)
        except (TypeError, ValueError):
            logger.warning("运行时状态 ban_expire 条目损坏，已跳过: %r", gid)
    # 全员禁言必须带过期时间：离线期间管理员解禁了，这条事件机器人收不到，
    # 老实现无条件恢复 True，群里就永久变哑巴，只能手改文件才救得回来
    for gid, exp in _as_dict(data.get("all_banned")).items():
        try:
            exp_ts = float(exp)
        except (TypeError, ValueError):
            # 旧版本存的是裸 True，给个短命宽限，尽快重新核一次
            exp_ts = now + 1800.0
        if exp_ts > now:
            group_all_banned[int(gid)] = exp_ts
    if group_silence_until or group_bot_ban_expire or group_all_banned:
        logger.info("已恢复运行时保护状态: 静默 %d 群 / 禁言 %d 群", len(group_silence_until), len(group_bot_ban_expire))

async def check_bot_banned_realtime(ws: aiohttp.ClientWebSocketResponse, group_id: int) -> bool:
    """双重保障检查：优先查内存事件；若处于被动保护期，主动调 OneBot11 API 核实真实禁言状态"""
    now = time.time()
    if now < group_all_banned.get(group_id, 0.0):
        return True
    group_all_banned.pop(group_id, None)   # 过期了自己失效，别永久留一个 True
    expire = group_bot_ban_expire.get(group_id, 0.0)
    if now >= expire:
        return False
        
    # 离解禁还早（>60 秒）就别反复问了：QQ 不会凭空提前解禁，
    # 只在临近截止时间复核，免得每条消息都打一次 API（禁言一小时能问几百次）
    if expire - now > 60:
        return True

    # 临近解禁才核实，30 秒一次足够
    last_check = last_active_ban_check.get(group_id, 0.0)
    if now - last_check >= 30.0 and ws:
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


async def backfill_group_history(ws, group_id: int, count: int = 20) -> None:
    """重连后把最近的群消息补回时间线。

    断线期间 NapCat 不会补推事件，回来时上下文是空的；
    补一次至少能接上前情，代价只有一次只读 API。
    """
    try:
        # message_seq=0 才是「从最新往回取」；不带这个参数 NapCat 会回一个空列表
        res = await call_action(ws, "get_group_msg_history",
                                {"group_id": group_id, "message_seq": 0, "count": count}, timeout=8.0)
        if not res or res.get("status") != "ok":
            logger.warning("[群 %d] 补历史失败: status=%s retcode=%s",
                           group_id, (res or {}).get("status"), (res or {}).get("retcode"))
            return
        msgs = (res.get("data") or {}).get("messages") or []
        # 服务端按新→旧给，时间线要旧的在前
        msgs.sort(key=lambda m: m.get("time") or 0)
        known = {m.get("msg_id") for m in group_timelines.get(group_id, ())}
        added = 0
        for m in msgs:
            mid = m.get("message_id")
            text = (m.get("raw_message") or "").strip()
            if not text or mid in known:
                continue
            sender = m.get("sender") or {}
            uid = sender.get("user_id") or m.get("user_id")
            name = sender.get("card") or sender.get("nickname") or "群友"
            # 自己发的要标出来：否则重连补回的历史里，模型的上一句变成"某个群友说的"，
            # 它会顺着自己的旧话再接一遍，或者把自己当第三方来评论
            is_self = str(uid) == str(BOT_QQ)
            group_timelines[group_id].append({
                "sender": f"小鲸鱼(你自己)(QQ {uid})" if is_self else f"{name}(QQ {uid})",
                "sender_id": uid,
                "content": text[:300],
                "time": float(m.get("time") or time.time()),
                "msg_id": mid,
            })
            added += 1
        if added:
            logger.info("[群 %d] 重连后补回 %d 条历史消息", group_id, added)
    except Exception as e:
        logger.warning("[群 %d] 补历史失败: %s: %s", group_id, type(e).__name__, e)


# 管理员私聊历史记录
admin_private_timeline: Deque[str] = collections.deque(maxlen=PRIVATE_TIMELINE_MAX_LEN)
# 管理员私聊长记录池（保留最近 100 条）
admin_private_history: Deque[str] = collections.deque(maxlen=100)

# WebSocket 请求回调映射与运行中模型任务集合
pending_requests: Dict[str, asyncio.Future] = {}
running_tasks: Set[asyncio.Task] = set()
# 所有后台任务的强引用。create_task 不存引用，任务可能被 GC 提前回收，
# 异常也只能等解释器退出时才吐一句 never retrieved，等于没日志。
background_tasks: Set[asyncio.Task] = set()

def _on_background_done(task: asyncio.Task) -> None:
    background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error("后台任务异常退出: %s", exc, exc_info=exc)

# ==================== 撤回联动 ====================
# message_id -> 由这条消息触发的回复任务。撤回时直接 cancel，模型那边也会跟着断开。
reply_tasks: Dict[int, asyncio.Task] = {}
reply_task_groups = {}   # msg_id -> group_id，/stop 在群里发时只停本群
# 已撤回的消息 id：任务来不及取消时，发送前还有最后一道闸
recalled_msgs: Dict[int, float] = {}
RECALL_KEEP_SECONDS = 600


def track_reply_task(message_id, task, group_id=None) -> None:
    """记住「这条消息触发的回复」，撤回时才有东西可取消。"""
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        return
    reply_tasks[mid] = task
    if group_id is not None:
        reply_task_groups[mid] = group_id   # /stop 在单个群发时只停这个群
    if len(reply_tasks) > 400:
        for k in list(reply_tasks)[:200]:
            reply_tasks.pop(k, None)
            reply_task_groups.pop(k, None)


def mark_recalled(message_id) -> bool:
    """标记撤回。返回 False 表示这条本来就没在等回复，不用管。"""
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        return False
    now = time.time()
    for k, ts in list(recalled_msgs.items()):
        if now - ts > RECALL_KEEP_SECONDS:
            recalled_msgs.pop(k, None)
    recalled_msgs[mid] = now
    return True


def was_recalled(message_id) -> bool:
    if not message_id:
        return False
    try:
        return int(message_id) in recalled_msgs
    except (TypeError, ValueError):
        return False


def spawn(coro, *, track_for_stop: bool = False) -> asyncio.Task:
    """统一的后台任务入口：留引用、接异常、可选纳入 /stop 的取消范围"""
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(_on_background_done)
    if track_for_stop:
        running_tasks.add(task)
        task.add_done_callback(running_tasks.discard)
    return task

# ==================== 按群独立配置管理 ====================
GROUP_CONFIGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "group_configs.json")
# 临时免红温名单：改这个文件立即生效（按 mtime 热加载），不用重启进程
ANGER_OVERRIDE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anger_overrides.json")
_anger_cache = {"mtime": 0.0, "data": {}}


def is_anger_unlimited(group_id) -> bool:
    """该群是否临时把红温阈值拉满（被骂也不真急眼、不禁骂）。"""
    try:
        mt = os.path.getmtime(ANGER_OVERRIDE_FILE)
    except OSError:
        return False
    if mt != _anger_cache["mtime"]:
        try:
            with open(ANGER_OVERRIDE_FILE, "r", encoding="utf-8") as f:
                _anger_cache["data"] = json.load(f)
            _anger_cache["mtime"] = mt
        except Exception as e:
            logger.warning("读取 anger_overrides.json 失败，按正常阈值处理: %s", e)
            return False
    # 这个文件是给人手改的（注释里还写着「改这个文件立即生效」），
    # 一旦写成数组或 {"群号": true}，原来的 .get() 就在每条群消息主流程上抛
    # AttributeError，整个群直接静默。结构不对就当没配。
    data = _anger_cache.get("data")
    cfg = data.get(str(group_id)) if isinstance(data, dict) else None
    if not isinstance(cfg, dict):
        return False
    # 只认真正的 true：写成 "no"/"false" 这类字符串不该当成开启
    return cfg.get("unlimited") is True
DEFAULT_GROUP_CONFIG = {
    "muted": False,             # /off 一键静音的总闸：群友说什么都不回，管理员指令照旧
    "enable_interject": True,   # 是否允许主动插话
    "interject_prob": 0.25,     # 主动接话意愿 (0.0~1.0)：越高门槛越低，25 = 中等质量话题就接
    "enable_sleep": True,       # 是否启用 01:00~07:00 拟真作息
    "enable_poke": True,        # 是否开启戳一戳互动与回戳
    "enable_games": True,       # 是否开启数字炸弹/掷骰子等文字游戏
    "enable_search": True,      # 是否开启联网热点检索 (鸭鸭搜/B站)
    "enable_yunzai": True,      # 是否开启云崽游戏生态 (原神/星铁/米游社)
}
DEFAULT_PRIVATE_CONFIG = {}
group_configs: Dict[str, dict] = {}

def load_group_configs():
    global group_configs
    load_failed = False
    if os.path.exists(GROUP_CONFIGS_FILE):
        loaded = load_json_with_recovery(GROUP_CONFIGS_FILE)
        if not isinstance(loaded, dict):
            # 读坏了就用默认值跑，但绝不立刻覆盖原文件——留给人去捞。
            # 原来这里打完「磁盘文件保持原样」下一行就 save_group_configs()，
            # 静默群全被打开、所有 enable_* 还原，日志说的和做的正好相反。
            logger.error("群独立配置读取失败，本次以默认配置运行，磁盘文件保持原样")
            group_configs = {}
            load_failed = True
        else:
            group_configs = loaded
    for gid in ALLOWED_GROUPS:
        sgid = str(gid)
        entry = group_configs.get(sgid)
        # 条目被写成字符串/null（手改、外部脚本写入）时原来会 TypeError 崩在 import，
        # 进程起不来且每次重启都崩，只能手改文件才能救
        if not isinstance(entry, dict):
            if entry is not None:
                logger.warning("群 %s 的配置不是对象（%s），已重置为默认值", sgid, type(entry).__name__)
            entry = dict(DEFAULT_GROUP_CONFIG)
            group_configs[sgid] = entry
        for k, v in DEFAULT_GROUP_CONFIG.items():
            if k not in entry:
                entry[k] = v
    if not isinstance(group_configs.get("private"), dict):
        group_configs["private"] = dict(DEFAULT_PRIVATE_CONFIG)
    if load_failed:
        # 内存里跑默认值可以，但不能把坏文件写没：等人工修复或下次正常退出再落盘
        logger.warning("配置读取失败，本次跳过写回，避免把原文件覆盖成默认值")
        return
    save_group_configs()

def save_group_configs():
    try:
        backup_json(GROUP_CONFIGS_FILE)
        atomic_write_json(GROUP_CONFIGS_FILE, group_configs)
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


def update_group_config(group_id: Optional[int], key: str, value: Any) -> bool:
    # 只认白名单里的群：get_group_config 会凭空建条目，陌生群号一旦写进 muted=False，
    # 还会把任意 QQ 群加进云崽白名单（越权开功能），配置里也会留一堆永不清理的脏群
    if group_id is not None and group_id not in ALLOWED_GROUPS:
        logger.warning("[配置更新] 拒绝未知群 %s 的配置写入", group_id)
        return False
    cfg = get_group_config(group_id)
    cfg[key] = value
    save_group_configs()
    logger.info("[配置更新] 频道 %s 更新 %s -> %r", "私聊" if group_id is None else f"群 {group_id}", key, value)
    if key == "enable_yunzai" and group_id is not None:
        sync_yunzai_whitelist(group_id, bool(value))
    if key == "muted" and group_id is not None:
        # 静默要真的安静：云崽插件也从白名单摘掉，解除时按 enable_yunzai 恢复
        sync_yunzai_whitelist(group_id, (not bool(value)) and bool(cfg.get("enable_yunzai", True)))
    return True

load_group_configs()
ADMIN_MODE: str = "user"  # 兼容旧引用

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
    
    files = []
    for f in os.listdir(cat_dir):
        if not f.lower().endswith(('.webp', '.jpg', '.jpeg', '.gif')):
            continue
        try:
            # getsize 原来是列表推导里的裸调用：目录里出现断链、被删、无权限的文件就抛异常，
            # 而这句在发文字之前，异常一路掀掉整个发送函数——表情包没了，话也发不出去
            if os.path.getsize(os.path.join(cat_dir, f)) <= 1200 * 1024:  # 限制 1.2MB，防止大图撑爆 WebSocket
                files.append(f)
        except OSError:
            continue
    if not files:
        return None
    chosen = os.path.join(cat_dir, random.choice(files))
    try:
        with open(chosen, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except Exception as e:
        logger.warning("读取表情包失败: %s", e)
        return None


# ==================== 大肥鱼专属表情包与消息清洗系统 ====================
def parse_card_content(text: str) -> str:
    """从分享卡片段（json / xml / share）里抽标题、描述、BV号与链接，避免被一刀切抹掉"""
    def _parse_card_match(m):
        body = m.group(2)
        titles = []
        bvs = []
        
        # 提取 BV 号
        body_norm = body.replace(r"\/", "/")
        bv_match = re.search(r'(BV[0-9A-Za-z]{10})', body_norm)
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

def extract_dialogue_content(timeline_text: str) -> str:
    """从群聊时间线文本中提取发言纯正文，剔除发言者昵称前缀与 [管理员] 标识。

    时间线形如:
      [管理员] life: 哈哈
      疯狂星期四V我50: 今天吃啥
    若直接匹配整行，群友的奇葩昵称（如带有'疯狂星期四'）会污染全局梗匹配与时效搜索，
    导致大模型无论聊什么都死抠昵称里的梗。
    """
    if not timeline_text:
        return ""
    content_lines = []
    for line in timeline_text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # 兼容可能有 [管理员] 或普通昵称后的冒号分割 (支持中文冒号和英文冒号)
        # 如: "[管理员] 张三: 正文" 或 "李四: 正文"
        if ":" in line:
            parts = line.split(":", 1)
            content_lines.append(parts[1].strip())
        elif "：" in line:
            parts = line.split("：", 1)
            content_lines.append(parts[1].strip())
        else:
            content_lines.append(line)
    return "\n".join(content_lines)


def clean_cq_codes(text: str) -> str:
    """去除表情、图片、文件等大段 CQ 码，保留纯文本与卡片语义"""
    # 先做卡片语义提取
    text = parse_card_content(text)

    def _face_placeholder(m):
        # 新版 NapCat 的小黄脸带 raw={"faceText":"/大怨种",...}，老写法只认
        # `[CQ:face,id=123]`，整段 200 多字符的 JSON 会原样进提示词，
        # 模型就照着复读一句「[CQ:face,id=344,raw={...}]」，群里看着像报错。
        ft = re.search(r'"faceText"\s*:\s*"([^"]{1,12})"', m.group(0))
        name = ft.group(1).strip().lstrip("/") if ft else ""
        return f"[表情:{name}]" if name else "[表情]"

    # 带参数的表情码一律吃掉，顺带把表情名捞出来当语义
    text = re.sub(r"\[CQ:face,[^\]]*\]", _face_placeholder, text)
    # 大表情/商城表情 [CQ:bface,id=xxx] 保留提示
    text = re.sub(r"\[CQ:bface,[^\]]*\]", "[大表情]", text)
    # 再剥离纯媒体与文件类 CQ 码（包含 file, image, record, video, poke 等）
    text = re.sub(r"\[CQ:(image|record|video|file|audio)[^\]]*\]", "", text)
    text = re.sub(r"\[CQ:poke[^\]]*\]", "", text)
    # 兜底：协议新增的类型（mface/dice/rps/markdown…）不许漏进提示词，
    # 之前每加一种类型就得改一次正则，漏一次就是一大段 JSON 喂给模型
    text = re.sub(r"\[CQ:[A-Za-z_]+(?:,[^\]]*)?\]", "", text)
    # 存储层会把超长 raw 截到几百字符，截断后没有收尾的 `]`，
    # 上面所有正则全部匹配不上，整段 JSON 照样进提示词。这种半截码只可能落在末尾。
    text = re.sub(r"\[CQ:[A-Za-z_]+(?:,[^\]]*)?$", "", text)
    return text.strip()

# 我们自己造出来的占位符（入站把 CQ 码洗成「[表情:大怨种]」「[图片]」），
# 模型会把它当成一句话原样复读出来，群里看到的就是一串方括号。出站全部剥掉。
_OUTBOUND_PLACEHOLDER_RE = re.compile(
    r"\[(?:表情|大表情|图片|卡片|语音|视频|文件|红包|链接|回复|合并转发聊天记录|位置)[^\]]{0,24}\]"
)


def strip_outgoing_cq(text: str) -> str:
    """剥掉模型自己吐出来的 CQ 码与占位符，只留格式合法的 @。

    模型偶尔会把群友的原始 CQ 码当话复读；NapCat 解析不了 `raw=` 这种参数，
    就把整段当普通文本发进群，群友看到的就是一条「报错」。占位符同理：
    「[表情:大怨种]」进了正文，看着像没渲染成功的乱码。
    """
    if not text:
        return ""
    # 合法的 at 保留：动作层和回复分支真的靠它艾特人
    text = re.sub(r"\[CQ:at,[^\]]*qq=(\d+)[^\]]*\]", r"[CQ:at,qq=\1]", text)
    # 其余一律清掉；保留 CQ 码原文没有意义，群里只会看见乱码
    text = re.sub(r"\[CQ:(?!at,qq=\d+\])[A-Za-z_]+(?:,[^\]]*)?\]", "", text)
    text = _OUTBOUND_PLACEHOLDER_RE.sub("", text)
    # 剥完常留下孤零零的标点或双空格
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"^[\s，、,。.！!~]+", "", text).strip()
    return text.strip()


def strip_markdown(text: str) -> str:
    """去除 Markdown 符号，适应手机 QQ 纯文本阅读"""
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\*(.*?)\*", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"`{3}[a-zA-Z]*\n?([\s\S]*?)`{3}", r"\1", text)
    text = re.sub(r"`(.*?)`", r"\1", text)
    return text.strip()

def sanitize_text(text: str) -> str:
    """清理非法 Unicode 孤立代理项及残损控制字符，防止发给大模型 API 时报 400"""
    if not text:
        return ""
    return text.encode("utf-8", "replace").decode("utf-8", "ignore")

def in_sleep_window(hour: int) -> bool:
    """给定时点（CST 小时）是否落在休眠区间。跨午夜时区间被零点切开，不能直接比大小。"""
    if SLEEP_START_HOUR <= SLEEP_END_HOUR:
        return SLEEP_START_HOUR <= hour < SLEEP_END_HOUR
    return hour >= SLEEP_START_HOUR or hour < SLEEP_END_HOUR


def is_bot_sleeping(group_id: Optional[int] = None) -> bool:
    """判断当前是否为夜间入睡期 (23:00 ~ 07:00 CST)，主人叫醒期除外，按群检查是否开启作息"""
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
    return in_sleep_window(cst_hour)

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

def _skeleton(text: str) -> str:
    """把一句话削到只剩骨架：去标点、去空白，用来判重。"""
    return re.sub(r"[\s，。！？、,.!?~～…—\-\"“”'‘’()（）\[\]【】]", "", text)


def get_anti_repeat_hint(group_id: int = 0) -> str:
    """把本群刚说过的几句摆回模型面前当负样本。

    不写「不许说本鲸宝」「不许用括号」这类词表规则——规则堆得越多，说话越僵。
    它看见自己前几句说过什么，自己就知道该换个说法。
    """
    recent = [r for r in recent_bot_replies.get(group_id, ()) if r][-4:]
    if not recent:
        return ""
    return "【你刚在群里说过这些话，别原样重复，换个说法】:\n" + "\n".join(f"- {r}" for r in recent)


# 书面连接词和客服式收尾，是"AI 味"最明显的来源；群里没人这么说话
# 前一组是固定书面套话，删掉不影响语义；后一组只有后面跟逗号时才是连接词，
# 否则「另外一个人」「首先我得说清楚」里的词是真内容，不能动。
# 群里别的机器人：OneBot 不给可靠标记，只能靠名单 + 昵称特征认。
# 认错的代价很直接——把它的播报当成人话去接，或者跟它对吵刷屏。
_PEER_BOT_NAME_HINTS = ("机器人", "bot", "助手", "管家", "小助手", "bot酱")
PEER_BOT_QQS: set[int] = set()


def load_peer_bots() -> None:
    """从 data/peer_bots.json 读已知机器人 QQ（一个数组，随时手改）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "peer_bots.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            PEER_BOT_QQS.clear()
            PEER_BOT_QQS.update(int(q) for q in json.load(f) if str(q).strip().lstrip("-").isdigit())
    except FileNotFoundError:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("[其他机器人] 名单读取失败: %s", e)


def is_peer_bot(qq, nick: str = "") -> bool:
    try:
        if int(qq) in PEER_BOT_QQS:
            return True
    except (TypeError, ValueError):
        pass
    n = (nick or "").lower()
    return any(h in n for h in _PEER_BOT_NAME_HINTS)


_AI_OPENERS = (
    "总的来说", "总而言之", "综上所述", "需要注意的是", "值得一提的是", "值得注意的是",
    "换句话说", "简而言之", "简单来说",
)
_AI_CONNECTIVES = ("首先", "其次", "另外", "此外", "所以说")
_AI_TAILS = (
    "希望对你有帮助", "希望能帮到你", "希望这能帮到你", "如果还有其他问题",
    "还有其他问题的话", "有什么需要随时说", "随时可以问我",
    # 客服句式：真人聊天不会先给结论再补一句「你可以试试」，更不会问「还需要什么」
    "如有需要", "如需帮助", "有问题随时找我", "还有什么可以帮你", "需要的话可以试试",
    "建议你可以试试", "你可以尝试一下", "供你参考", "以上",
)


def scrub_ai_flavor(text: str) -> str:
    """削掉 AI 腔：段首书面连接词、客服式收尾、破折号硬转折。

    只删词不删意思，剩下的句子还是模型自己的话。
    """
    if not text:
        return text
    t = text
    for w in _AI_OPENERS:
        t = re.sub(rf"(^|[，。！？\n])\s*{w}[，,]?\s*", r"\1", t)
    for w in _AI_CONNECTIVES:
        # 必须带逗号才算连接词：「另外，说个事」删，「另外一个人」留着
        t = re.sub(rf"(^|[，。！？\n])\s*{w}\s*[，,]\s*", r"\1", t)
    for w in _AI_TAILS:
        t = re.sub(rf"[，,]?\s*{w}[。！？]?\s*$", "", t)
    t = re.sub(r"[—–]{2,}", "，", t)
    t = re.sub(r"，{2,}", "，", t)
    return t.strip(" ，\n")


def de_template(reply: str, group_id: int = 0) -> str:
    """挡复读，也削「每条一个模子」的形状。

    以前只判整句骨架相同；但连着几条「不是，……」起手、或者每条都用
    「是吧」收尾，单看都正常，连起来就是复读机。形状判定放 services/anti_template，
    这里只负责调用和日志。
    """
    text = reply.strip()
    if not text:
        return reply
    recent = [r for r in recent_bot_replies.get(group_id, ()) if r]

    skel = _skeleton(text)
    if skel and any(_skeleton(r) == skel for r in recent):
        segs = [s for s in re.split(r"[，,。！？]", text) if s.strip()]
        if len(segs) > 1:
            # 第一段太短（"吵死啦""好家伙"）就带上第二段，别削成俩字
            keep = segs[:2] if len(segs[0]) < 6 else segs[:1]
            text = "，".join(keep)
            logger.info("[反模板闸] 整句复读，削短: %r", text)
        return text

    reason = anti_template.match(text, recent)
    if reason != "none":
        reshaped = anti_template.reshape(text, reason)
        if reshaped and reshaped != text:
            logger.info("[反模板闸] %s 撞车，削形: %r -> %r", reason, text[:30], reshaped[:30])
            text = reshaped

    # 连着几条同一个语气词收尾，听起来就像同一个人在同一条录音里
    trimmed = anti_template.trim_particle(text, recent)
    if trimmed != text:
        logger.info("[反模板闸] 语气尾巴重复，摘掉: %r", text[-12:])
        text = trimmed
    return text


# 接话质量打分用到的词表：只做加权，不再当"命中就闭嘴"的黑白名单。
# 以前的子串黑名单把「有人吗」这类正常提问整条砍掉，误杀比拦截更伤。
INTERJECT_SLANG_WORDS = (
    "笑死", "离谱", "绝了", "卧槽", "绷不住", "破防", "太抽象", "什么鬼",
    "真的服", "逆天", "抽象", "答辩", "急急急",
)
INTERJECT_QUESTION_HINTS = (
    "吗", "呢", "怎么", "为什么", "为啥", "谁", "哪", "是不是", "有没有",
    "如何", "求", "知不知道", "干嘛", "干啥", "咋", "？", "?",
)
INTERJECT_CODE_HINTS = ("报错", "traceback", "npm", "pip", "git", "docker", "报错", "编译", "配置")

def _norm_reply_text(text: str) -> str:
    """只留文字、去掉标点空白，用来判「是不是同一句话」"""
    return re.sub(r"[\s\W_]+", "", text or "")


def is_repeat_reply(group_id: int, text: str, window: float = 600.0) -> bool:
    """同群 10 分钟内说过同样的话就闭嘴：短句复读是群里最容易露馅的机器人特征。"""
    key = _norm_reply_text(text)
    if not key:
        return False
    now = time.time()
    q = group_recent_replies[group_id]
    while q and now - q[0][0] > window:
        q.popleft()
    return any(k == key for _, k in q)


ARCHIVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "live")


def archive_message(group_id: int, user_id, nick: str, content: str, msg_id=None) -> None:
    """每条群消息顺手落一份到 data/live/YYYY-MM.jsonl。

    journal 会轮转，原始语料丢了再也拿不回来；写失败绝不能影响聊天，所以整段吞异常。
    """
    try:
        os.makedirs(ARCHIVE_DIR, exist_ok=True)
        row = {
            "t": int(time.time()),
            "gid": group_id,
            "uid": user_id,
            "nick": nick,
            "msg_id": msg_id,
            "raw": (content or "")[:500],
        }
        path = os.path.join(ARCHIVE_DIR, time.strftime("%Y-%m") + ".jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug("归档消息失败: %s", e)


def note_reply_sent(group_id: int, text: str) -> None:
    """记一笔真实发出的回复，复读闸和密度闸都靠它"""
    now = time.time()
    # 单调计数器：deque 有上限、还会被密度闸 popleft，拿 len() 判断"这次有没有发出去"
    # 三个方向都会错（发了判没发、没发判发了），所以单独记一个只增不减的序号
    group_bot_reply_seq[group_id] += 1
    group_recent_replies[group_id].append((now, _norm_reply_text(text)))
    group_bot_reply_times[group_id].append(now)
    # 自己的话也归档：以后对比「真人怎么接 / 机器怎么接」才看得出来差在哪
    archive_message(group_id, BOT_QQ, "机器人", text)


def reply_density_blocked(group_id: int, is_at: bool, window: float = 120.0, limit: int = 2) -> bool:
    """两分钟内已经主动说过两条就停，被点名才例外。

    连刷五条换来管理员一小时禁言，代价比少接一句大得多。
    """
    if is_at:
        return False
    now = time.time()
    q = group_bot_reply_times[group_id]
    while q and now - q[0] > window:
        q.popleft()
    return len(q) >= limit


def score_message_quality(text: str, group_id: int = 0, has_image: bool = False,
                          has_media: bool = False, recent_msgs: int = 0) -> tuple[float, str]:
    """给一条群消息打 0~1 的"值得接话"分，全部本地规则，零 token。

    取代原来的随机抽样：随机意味着好话题也可能被掷丢，差话题也可能被抽中。
    这里只看内容本身的信号强度，分数越高越值得开口。
    """
    t = (text or "").strip()
    if not t and not has_image and not has_media:
        return 0.0, "空消息"

    # 纯表情/占位符不算内容。净化后只剩 `[表情:大怨种]` 这类占位符时，
    # 长度判断会把它当成有内容的长句（占位符+括号也有十来个字），
    # 于是群友甩个表情就被接一句，莫名其妙。
    _meaningful = re.sub(r"\[(?:表情|大表情|图片|卡片)[^\]]*\]", "", t).strip()
    if not _meaningful and not has_image and not has_media:
        return 0.0, "纯表情"

    score = 0.30
    hits = []

    # 信息量：太短的接不上，中长句有内容
    length = len(_meaningful)
    if length >= 6:
        score += 0.10
    if length >= 15:
        score += 0.10
        hits.append("有内容")
    if length > 60:
        score += 0.05
        hits.append("长文")

    # 提问天然值得回应
    if any(h in t for h in INTERJECT_QUESTION_HINTS):
        score += 0.20
        hits.append("提问")

    # 带上"小鲸鱼/鱼"才算点名；单纯一个「你」在群里十有八九指别人，
    # 以前把它当 +0.15 的信号，等于给一大堆跟自己无关的话开了绿灯
    if any(w in t for w in ("小鲸鱼", "大肥鱼", "肥鱼")):
        score += 0.25
        hits.append("点名")

    # 情绪/玩梗，群里最容易接的一类
    if any(w in t for w in INTERJECT_SLANG_WORDS) or "！" in t or "!" in t:
        score += 0.20
        hits.append("有情绪")

    # 技术求助：接得上就很有价值
    if any(w in t.lower() for w in INTERJECT_CODE_HINTS):
        score += 0.10
        hits.append("技术")

    if has_image:
        score += 0.15
        hits.append("带图")
    if has_media:
        score += 0.20
        hits.append("带卡片")

    # 同一话题聊了三条以上说明群里在正经讨论，值得接一句；
    # 单人刷屏同样会累加，但有「时机 刷屏」的扣分兜着，不会被刷屏带飞
    if recent_msgs >= 3:
        score += 0.06
        hits.append("话题延续")
    if recent_msgs >= 6:
        score += 0.04

    # 纯表情/单字/复读往下压
    cleaned = re.sub(r"[\s\d\W_]+", "", t)
    if len(cleaned) <= 1:
        score -= 0.20
        hits.append("过短")
    if re.fullmatch(r"(哈+|6+|草+|啊+|嗯+|哦+|好+)", cleaned or ""):
        score -= 0.15
        hits.append("纯水")

    # 冷场时同样的话更值得接；刚聊过则适当收敛
    now = time.time()
    last_msg = group_last_msg_time.get(group_id, 0.0)
    last_reply = group_last_bot_reply_time.get(group_id, 0.0)
    if last_msg > 0 and (now - last_msg) >= 600.0:
        score += 0.10
        hits.append("冷场")
    elif last_reply > 0 and (now - last_reply) < 600.0:
        score += 0.05
        hits.append("热场")

    score = max(0.0, min(1.0, score))
    return score, "+".join(hits) or "普通闲聊"


def interject_threshold(desire: float) -> float:
    """接话意愿 → 质量门槛。判定与面板展示共用这一份，避免两边公式各写一套漂移。"""
    return max(0.40, min(0.95, 0.86 - 0.8 * desire))


def should_active_interrupt(text: str, group_id: int = 0, msg_count_since: int = 0,
                            has_image: bool = False, has_media: bool = False) -> bool:
    """旁观接话闸门：只做开关/作息判断，剩下交给质量分。

    interject_prob 的语义从"抽中概率"变成"接话意愿"：越高门槛越低。
    25（默认）对应门槛 0.66 —— 普通闲聊不接，提问、玩梗、带图、指向机器人才接。
    """
    cfg = get_group_config(group_id if group_id else None)
    # 静默群连主动插话都停：静音就该彻底安静，只留 /on 这一条出口
    if cfg.get("muted"):
        return False
    if not cfg.get("enable_interject", True):
        return False

    if is_bot_sleeping(group_id):
        return False

    score, why = score_message_quality(text, group_id=group_id, has_image=has_image,
                                       has_media=has_media, recent_msgs=msg_count_since)
    desire = float(cfg.get("interject_prob", 0.25))
    # 门槛整体抬高 0.06：旧公式在 desire=0.25 时门槛只有 0.60，一句六字带感叹号的
    # 闲聊就能过线，于是群里每个话题它都要凑一句
    threshold = interject_threshold(desire)

    # 每小时开口配额：问它话的人随时能 @ 到，主动凑话不该这么勤
    now_ts = time.time()
    recent_openings = [t for t in group_interject_times.get(group_id, ()) if now_ts - t < 3600]
    if len(recent_openings) >= 6:
        logger.info("[群 %d] 近一小时已主动插话 %d 次，本轮只接 @ 与点名", group_id, len(recent_openings))
        return False

    # 质量分只说明「话题值不值得接」，还不等于「现在该不该开口」：
    # 刚说完话就插嘴是抢话，上一条没人接还追着说是自说自话。
    now = time.time()
    last_reply = group_last_bot_reply_time.get(group_id, 0.0)
    if interject_planner.too_soon(now, last_reply):
        logger.info("[群 %d] 接话时机：%d 秒前刚说过，先不抢", group_id, int(now - last_reply))
        return False
    score, timing = interject_planner.adjust(
        score,
        now=now,
        last_bot_reply=last_reply,
        msgs_since_bot=group_msg_count_since_bot.get(group_id, 0),
        last_reply_answered=affect.is_last_reply_answered(group_id),
    )
    passed = score >= threshold
    logger.info("[群 %d] 接话质量 %.2f vs 门槛 %.2f (%s | 时机 %s) -> %s",
                group_id, score, threshold, why, timing or "无修正", "接" if passed else "略过")
    return passed

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
        # 排除管理员的系统级斜杠管理指令 (/new, /stop 等依然保留给 AI)
        # ! ！ ？ ? ~ ～ 在中文里多半是情绪（「！这也太离谱了」「～好困」），
        # 以前一律当指令，私聊里主人的这类话会被静默吞掉。只有后面紧跟
        # ASCII 命令词（?help / !roll）才认。
        if re.match(r"^[!！?？~～%％$￥]\s*[A-Za-z0-9]", t):
            return True
        if re.match(r"^[!！?？~～%％$￥]\s*(?:帮助|菜单|指令|点歌|音乐|签到|图|来张|来张图|色图|搜图)", t):
            return True
        if t[0] in "#井*＊":
            return True
        # 其余以符号开头的一律当闲聊，交给模型
        
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
    # 带 ^.+ 或结尾 .*$ 的宽松模式（「^.+(?:面板|材料|培养).*$」这类）本来是按
    # 游戏黑话写的，但它们把「帮我看看这个面板」「我想问下培养」「深渊今天开了」
    # 全判成云崽指令，私聊里主人的消息直接被静默吞掉。带口语标记的长句不作数。
    _chatty = len(t) >= 6 and bool(re.search(r"[我你他她它的了吗呢吧啊呀哦嘛]|怎么|什么|为什么|是不是|能不能", t))
    for p in patterns:
        loose = p.startswith("^.+") or p.endswith(".*$")
        if loose and _chatty:
            continue
        if re.search(p, t, re.IGNORECASE):
            return True
    return False

async def extract_reply_meta(ws: aiohttp.ClientWebSocketResponse, event: dict, raw_msg: str) -> Optional[dict]:
    """解析「这条消息引用/回复了谁」。

    返回 {"text": 引用内容, "sender_id": 被引用者 QQ, "is_image": bool, "nick": 昵称}。

    注意：NapCat 这个版本不在 event 里塞 reply 字段（日志里 7 天 0 次），
    只有 raw_message 里的 [CQ:reply,id=xxx]，所以必须回查 get_msg 才拿得到「引用的是谁」。
    以前直接读 event["reply"] 判「是不是在回我」，等于永远判 False。
    """
    reply_obj = event.get("reply") if isinstance(event.get("reply"), dict) else None

    # 1. NapCat 真给了 reply 字段就白嫖，省一次 API
    if reply_obj:
        sender = reply_obj.get("sender") or {}
        nick = sender.get("nickname") or sender.get("card") or "群友"
        orig_raw = reply_obj.get("raw_message") or reply_obj.get("message") or ""
        sender_id = sender.get("user_id")
    else:
        # 2. 否则按 [CQ:reply,id=xxx] 回查原消息
        m = re.search(r"\[CQ:reply,id=(-?\d+)\]", raw_msg or "")
        if not m:
            return None
        res = await call_action(ws, "get_msg", {"message_id": int(m.group(1))}, timeout=1.0)
        if not res or res.get("status") != "ok":
            return None
        data = res.get("data", {})
        sender = data.get("sender") or {}
        nick = sender.get("nickname") or sender.get("card") or "群友"
        orig_raw = data.get("raw_message") or ""
        sender_id = sender.get("user_id")

    if "[CQ:image" in str(orig_raw):
        return {
            "text": f"[引用 {nick} 发送的图片: {orig_raw}]",
            "sender_id": sender_id,
            "is_image": True,
            "nick": nick,
        }
    orig_text = clean_cq_codes(orig_raw)
    if not orig_text:
        return {"text": None, "sender_id": sender_id, "is_image": False, "nick": nick}
    # 引用的是自己说的话，直接说「引用你」，模型才分得清这是冲它来的
    who = "你" if str(sender_id) == str(BOT_QQ) else nick
    return {
        "text": f'[引用{who}: "{orig_text[:50]}"]',
        "sender_id": sender_id,
        "is_image": False,
        "nick": nick,
    }


async def extract_reply_context(ws: aiohttp.ClientWebSocketResponse, event: dict, raw_msg: str) -> Optional[str]:
    """只要引用文本的旧接口，保留给私聊等调用方。"""
    meta = await extract_reply_meta(ws, event, raw_msg)
    return meta.get("text") if meta else None


# ==================== LLM 调用 ====================

# 全局 HTTP 会话（复用连接池减少延迟）
http_session: Optional[aiohttp.ClientSession] = None
current_ws: Optional[aiohttp.ClientWebSocketResponse] = None

# 模型调用的全局并发闸门：消息再多也不能一次性把上游打满
LLM_MAX_CONCURRENCY = int(os.environ.get("LLM_MAX_CONCURRENCY", "4"))
llm_semaphore = asyncio.Semaphore(LLM_MAX_CONCURRENCY)

def get_http_session() -> aiohttp.ClientSession:
    """获取或初始化全局 aiohttp ClientSession（统一连接池与超时）"""
    global http_session
    if http_session is None or http_session.closed:
        connector = aiohttp.TCPConnector(limit=32, ttl_dns_cache=300)
        timeout = aiohttp.ClientTimeout(total=60, connect=8, sock_read=50)
        http_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return http_session

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
_OBFUSCATE_CHAR = "删"
_OBFUSCATE_HOST_RE = re.compile(r"(https?://)([^/\s?#（）【】《》，。；：、！？]+)", re.IGNORECASE)
_CN_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")

def obfuscate_urls_for_qq(text: str) -> str:
    """往域名里插两个「删」字，断开 QQ 外链探测，同时让人一眼知道要去掉哪个字"""
    if not text or "://" not in text:
        return text

    def _replace(m):
        proto = m.group(1)
        host = m.group(2)
        if _CN_CHAR_RE.search(host):
            return m.group(0)  # 已经插过字，幂等跳过
        # 域名太短插两个会碎，短的就插一个
        count = 3 if len(host) >= 8 else 2
        # 别插在点号旁边，不然会变成 ub.删com 这种断法
        slots = [i for i in range(1, len(host)) if host[i] != "." and host[i - 1] != "."]
        if len(slots) >= count:
            picks = {slots[round((k + 1) * len(slots) / (count + 1)) - 1] for k in range(count)}
            for pos in sorted(picks, reverse=True):
                host = host[:pos] + _OBFUSCATE_CHAR + host[pos:]
        return proto + host

    result = _OBFUSCATE_HOST_RE.sub(_replace, text)
    if result != text and "「删」" not in result:
        result += "（链接里的「删」去掉再点）"
    return result

# 同群发送串行化：只保证不会两个协程同时写同一个群，不做速率限制
group_send_locks: Dict[int, asyncio.Lock] = collections.defaultdict(asyncio.Lock)






# ==================== 管理员快捷指令处理 ====================


# ==================== 消息事件处理 ====================



# ==================== 群通知与状态事件处理 ====================
async def poke_back(ws: aiohttp.ClientWebSocketResponse, group_id: int, user_id: int) -> None:
    """回戳发起来源。失败只记 warning，不影响后续回复"""
    try:
        poke_res = await call_action(ws, "group_poke", {"group_id": group_id, "user_id": user_id}, timeout=3.0)
        logger.info("[群 %d] 回戳用户 %s 完成: %s", group_id, user_id, poke_res)
    except Exception as e:
        logger.warning("[群 %d] 回戳用户 %s 失败: %s", group_id, user_id, e)


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
                group_all_banned[group_id] = time.time() + ALL_BANNED_TTL
                logger.warning("[群 %d] 管理员 %s 开启了全员禁言，机器人进入静默节流状态！", group_id, operator_id)
            else:
                group_all_banned[group_id] = 0.0
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

    # 2.5 消息撤回：这条消息触发的回复如果还没发出去，直接掐掉
    # NapCat 的群撤回是 notice_type=group_recall，私聊是 friend_recall
    if notice_type in ("group_recall", "friend_recall"):
        mid = event.get("message_id")
        operator = event.get("operator_id") or event.get("user_id")
        task = reply_tasks.pop(int(mid), None) if str(mid or "").lstrip("-").isdigit() else None
        cancelled = False
        if task and not task.done():
            task.cancel()
            cancelled = True
        mark_recalled(mid)
        removed = False
        if group_id:
            # timeline 只留 30 条，历史池深 200：只清 timeline 的话，撤回的私密内容
            # 还留在池子里，冷场冒泡会把它当素材喂给模型、再当众说出来
            for box in (group_timelines.get(group_id), group_history_pool.get(group_id)):
                for item in list(box or ()):
                    if item.get("msg_id") == mid:
                        box.remove(item)
                        removed = True
        logger.info("[撤回] 群 %s 消息 %s 被 %s 撤回，中断回复=%s，清理时间线=%s",
                    group_id or "私聊", mid, operator, cancelled, removed)
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
    if cfg.get("muted"):
        logger.info("[群 %d] 本群静默中，戳一戳也不回应", group_id)
        return
    if not cfg.get("enable_poke", True):
        logger.info("[群 %d] 戳一戳互动已禁用，略过", group_id)
        return

    # 先戳回去再说话：对方戳完的即时反馈就是回戳，别等模型生成完再补一刀
    await poke_back(ws, group_id, user_id)

    # 拟真睡眠作息检查：若正在睡觉被戳，触发翻身梦话反应（戳一戳保持原样，不叫醒）
    if is_bot_sleeping(group_id):
        logger.info("[群 %d] 处于睡眠期，被 %s 戳一戳，触发翻身梦话反应", group_id, f"管理员 {user_id}" if is_admin else f"群友 {user_id}")
        await asyncio.sleep(0.5)
        sleep_poke = random.choice([
            "呼噜...别戳啦，在睡觉呢",
            "唔...别闹，困死了继续睡",
            "呼呼...大半夜别戳鱼啦"
        ])
        await send_group_message(ws, group_id, sleep_poke, at_user_id=user_id)
        return

    logger.info("[群 %d] 机器人被 %s 戳一戳！触发动态傲娇回复...", group_id, f"管理员 {user_id}" if is_admin else f"群友 {user_id}")
    
    # 异步调用模型生成傲娇动态回复
    nick = "管理员" if is_admin else "群友"
    reply = await call_deepseek_poke(nick, is_admin=is_admin, group_id=group_id, user_id=user_id)
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
        asyncio.create_task(update_user_memory_async(user_id, "", "戳了戳小鲸鱼", group_id=group_id))

# ==================== 管理员私聊处理 ====================

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
        base_p = float(cfg.get("interject_prob", 0.25))
        # 面板与判定读同一个门槛函数，避免两处公式各算各的
        prob = base_p
        why = f"质量门槛 {interject_threshold(base_p):.2f}"
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
            "is_sleeping": is_bot_sleeping(),
            "sleep_override": (now < sleep_override_until),
            "uptime": (now - BOT_START_TIME),
            "start_time_str": start_dt.strftime("%Y-%m-%d %H:%M:%S")
        },
        "token_usage": token_tracker.get_status_summary(),
        "groups": groups_data,
        "configs": group_configs,
        # 前端按 string[] 渲染印象标签，这里把原子摊平成文本，别让面板上蹦出一串 JSON
        "memories": {
            scope_key: {
                uid: {**mem, "notes": [(a.get("text", "") if isinstance(a, dict) else str(a))
                                       for a in (mem.get("notes") or [])]}
                for uid, mem in scope.items() if isinstance(mem, dict)
            }
            for scope_key, scope in user_memories.items() if isinstance(scope, dict)
        }
    }

# ==================== 主连接循环 ====================
def weekly_favor_decay() -> int:
    """一周没露面的群友，好感从 50 以上回落 1 点。返回回落人次。"""
    cutoff = time.time() - 7 * 86400
    n = 0
    for scope in user_memories.values():
        for uid_str, mem in scope.items():
            if not isinstance(mem, dict):
                continue
            if uid_str.isdigit() and int(uid_str) in (ADMIN_QQ, BOT_QQ):
                continue
            favor = int(mem.get("favor", 50))
            if favor > 50 and mem.get("updated_at", 0) < cutoff:
                mem["favor"] = favor - 1
                mem["updated_at"] = time.time()
                n += 1
    if n:
        save_user_memories()
    return n


async def _favor_keeper():
    """每周一凌晨跑一次好感衰减，并定期清理过期的追问与好感打分缓冲状态。"""
    last_mark = ""
    last_cleanup_ts = 0.0
    while True:
        try:
            await asyncio.sleep(300)
            flush_user_memories()
            save_runtime_state()
            now_dt = datetime.datetime.now()
            now_ts = time.time()
            mark = now_dt.strftime("%Y-%W")
            if now_dt.weekday() == 0 and now_dt.hour == 0 and mark != last_mark:
                last_mark = mark
                n = weekly_favor_decay()
                if n:
                    logger.info("[好感] 一周没露面，%d 人回落 1 点", n)

            # 每 30 分钟清理一次已过期的临时会话与节流映射表，防止常驻内存泄漏
            if now_ts - last_cleanup_ts >= 1800.0:
                last_cleanup_ts = now_ts
                # 1. 清理过期追问窗口
                exp_followup = [k for k, v in list(user_follow_up_state.items()) if now_ts >= v.get("expire", 0.0)]
                for k in exp_followup:
                    user_follow_up_state.pop(k, None)

                # 2. 清理超过 1 小时未活动的 favor 打分节流时间戳
                exp_favor = [k for k, t in list(_favor_last_run.items()) if now_ts - t > 3600.0]
                for k in exp_favor:
                    _favor_last_run.pop(k, None)
                    _favor_pending.pop(k, None)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[好感守护] 异常: %s", e)


async def _refresh_group_member_nicks(ws, group_id: int) -> None:
    """把群成员昵称缓存下来，给「@了谁」和裸 QQ 号翻译用。

    缓存只在冷场巡检和首次进群时刷新，不做逐条消息查询——群消息频率下
    每次调 get_group_member_list 会把自己刷限流。
    """
    try:
        res = await call_action(ws, "get_group_member_list", {"group_id": group_id}, timeout=8.0)
    except Exception as e:
        logger.debug("[群 %d] 拉取群成员失败: %s", group_id, e)
        return
    data = (res or {}).get("data") or []
    if not isinstance(data, list):
        return
    mapping: Dict[int, str] = {}
    for m in data:
        try:
            qq = int(m.get("user_id"))
        except (TypeError, ValueError):
            continue
        # 优先群名片，其次昵称：群里认的是名片
        nick = (m.get("card") or m.get("nickname") or "").strip()
        if nick:
            mapping[qq] = nick[:16]
    if mapping:
        group_member_nicks[group_id] = mapping
        logger.info("[群 %d] 群成员昵称缓存已更新：%d 人", group_id, len(mapping))


async def _member_nick_refresher():
    """每 30 分钟刷一次成员昵称，缓存过期也不至于把新人认成数字。"""
    await asyncio.sleep(20)
    while True:
        try:
            if current_ws and not current_ws.closed:
                for gid in list(ALLOWED_GROUPS):
                    await _refresh_group_member_nicks(current_ws, gid)
                    await asyncio.sleep(1.0)   # 别把三条请求连成一串打出去
            await asyncio.sleep(1800)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("[昵称缓存守护] 巡检异常: %s", e)
            await asyncio.sleep(300)


def cold_nudge_gate(gid: int, now_ts: float, now_dt: datetime.datetime) -> Tuple[bool, str, str]:
    """冷场冒泡闸门：返回 (是否开口, 不开口的原因, 可用素材)。

    死群里最丢人的不是不开口，是天天对着空气自言自语，所以卡三道：
    素材必须是近 12 小时内的真人发言、上次冒泡之后得有人接话、每天最多两次。
    """
    timeline = group_timelines.get(gid)
    if not timeline or len(timeline) < 2:
        return False, "时间线还不够", ""
    cutoff = now_ts - 12 * 3600
    pool = list(group_history_pool.get(gid) or ())
    fresh_humans = [
        m for m in pool[-60:]
        if m.get("sender_id") != BOT_QQ
        and float(m.get("time", 0) or 0) >= cutoff
        and (m.get("content") or "").strip()
    ]
    # 全是自己的话或者昨天的旧话题时，接下去就是自说自话（「后天是某某生日」那种）
    if len(fresh_humans) < 2:
        return False, "近 12 小时没有真人发言", ""
    last_nudge_ts = group_last_nudge_ts.get(gid, 0.0)
    if last_nudge_ts and not any(
        float(m.get("time", 0) or 0) > last_nudge_ts for m in fresh_humans
    ):
        return False, "上次冒泡后没人接话", ""
    today_key = now_dt.strftime("%Y-%m-%d")
    if group_nudge_day.get(gid) != today_key:
        group_nudge_day[gid] = today_key
        group_nudge_count[gid] = 0
    if group_nudge_count.get(gid, 0) >= 2:
        return False, "今日已冒泡两次", ""
    material = " / ".join((m.get("content") or "").strip() for m in fresh_humans[-6:])[:200]
    return True, "", material


async def _cold_group_activator():
    """群聊冷场智能唤醒：白天时段（10:00~21:00）若白名单群安静超过 3 小时，且群开启插话，随性唤醒"""
    # 启动等待 5 分钟避免刚连上就触发
    await asyncio.sleep(300)
    while True:
        try:
            await asyncio.sleep(600)  # 每 10 分钟巡检一次
            now_dt = datetime.datetime.now()
            # 仅限白天活跃期：10:00 到 21:00
            if not (10 <= now_dt.hour <= 21):
                continue
            now_ts = time.time()
            for gid in list(ALLOWED_GROUPS):
                cfg = get_group_config(gid)
                if cfg.get("muted") or not cfg.get("enable_interject", True):
                    continue
                # 静默保护
                if now_ts < group_silence_until.get(gid, 0.0):
                    continue
                # 本群静默时连冷场冒泡也不许，静音就是彻底安静
                if get_group_config(gid).get("muted"):
                    continue
                last_msg_t = group_last_msg_time.get(gid, 0.0)
                last_active_t = group_last_active_reply.get(gid, 0.0)
                # 上次有消息超过 3 小时 (10800s)，且上次机器人主动插话超过 4 小时 (14400s)
                if (now_ts - last_msg_t > 10800.0) and (now_ts - last_active_t > 14400.0):
                    ok, why, material = cold_nudge_gate(gid, now_ts, now_dt)
                    if not ok:
                        logger.debug("[群 %d] 冷场但不冒泡：%s", gid, why)
                        continue
                    # 检查历史是否有过真实交流（避免僵尸群）
                    if current_ws and not current_ws.closed:
                        logger.info("[群 %d] 检测到白天冷场超过 3 小时，触发轻量随性抛话唤醒", gid)
                        # 先记下旧值：这次要是没真发出去（上游 5xx / 出站闸吞了），要退回去
                        _prev = {
                            "active": group_last_active_reply.get(gid, 0.0),
                            "msg": group_last_msg_time.get(gid, 0.0),
                            "nudge_ts": group_last_nudge_ts.get(gid, 0.0),
                            "reply_n": group_bot_reply_seq[gid],
                        }
                        group_last_active_reply[gid] = now_ts
                        group_last_msg_time[gid] = now_ts
                        group_nudge_count[gid] = group_nudge_count.get(gid, 0) + 1
                        group_last_nudge_ts[gid] = now_ts
                        extra = (
                            "当前群聊已经冷场三个多小时。你随手冒个泡，就像翻到一句旧话顺口接一句。"
                            "只许一句，2~12 字，不用句号。"
                            "禁止聊「怎么没人说话/好安静/冷清/都没人」这类空话题，也别报时间，"
                            "更别接上句给自己续话。"
                        )
                        if material:
                            extra += f"\n【最近群里真实聊过的内容，挑一个具体的点接】：{material}"
                        prev_nudge = group_last_nudge.get(gid, "")
                        if prev_nudge:
                            extra += f"\n【你上次冷场抛的是「{prev_nudge}」，这次换个话题，别换句话重复它】"
                        # 委托 process_group_trigger 发起一次冷场唤醒
                        from handlers.group import process_group_trigger
                        # 等它真的落地再算数：扣配额/写"刚活跃"都发生在生成之前，
                        # 上游 502 或出站闸静默时这里一条都没发出去，却照样占掉今天的名额，
                        # 还把群伪装成刚活跃——两次抖动就全天放弃这个群，仪表盘也看不出异常
                        try:
                            await process_group_trigger(
                                current_ws, gid,
                                is_at=False,
                                sender_nick="小鲸鱼",
                                is_admin=False,
                                target_uid=None,
                                user_text="（大家怎么突然都不说话了呀，悄悄冒个泡）",
                                extra_instruction=extra,
                                cold_nudge=True,
                            )
                        finally:
                            if group_bot_reply_seq[gid] == _prev["reply_n"]:
                                cold_nudge_rollback(gid, _prev)
        except asyncio.CancelledError:
            raise
        except Exception as ce:
            logger.debug("[冷场唤醒守护] 巡检异常: %s", ce)


async def _affect_keeper():
    """每 5 分钟给各群心境做一次衰减和「没人接」累积。

    心境不靠每条消息算，靠时间自然回落；这条循环只负责让它动起来。
    """
    while True:
        try:
            await asyncio.sleep(300)
            for gid in ALLOWED_GROUPS:
                affect.tick(gid)
            affect.save()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("[心境守护] 巡检异常: %s", e)


async def main():
    logger.info("启动 QQ DeepSeek 群聊 & 私聊机器人...")
    logger.info("白名单群: %s | 机器人 QQ: %d | 模型: %s | 管理员: %d", list(ALLOWED_GROUPS), BOT_QQ, MODEL_NAME, ADMIN_QQ)

    load_runtime_state()
    load_peer_bots()
    # 心境状态单独落盘：和记忆库分开，坏了也不至于把群友档案带崩
    affect.attach(atomic_write_json,
                  os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "affect_state.json"))
    # 走 spawn：裸 create_task 抛一次异常就永久消失，只在 GC 时留一句没有主语的
    # "Task exception was never retrieved"，好感衰减/存档/巡检会静默停摆
    spawn(_favor_keeper())
    spawn(_affect_keeper())
    spawn(_cold_group_activator())
    spawn(_member_nick_refresher())

    # 启动轻量现代浅色 Web 仪表盘（只绑 127.0.0.1，外网走 Cloudflare 隧道）
    try:
        await web_dashboard.start_web_server(get_bot_dashboard_status, update_config_func=update_group_config, port=8088)
        cross_memory.cross_group_mgr.start_background_flusher()
        logger.info("Web 仪表盘已启动，本机监听: http://127.0.0.1:8088")
    except Exception as e:
        logger.error("Web 仪表盘启动异常: %s", e)
    
    reconnect_fails = 0
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                logger.info("正在连接 NapCat OneBot 11 (%s)...", NAPCAT_WS_URL)
                # heartbeat 必不可少：NapCat 侧异常丢弃会话后 TCP 会留下半开连接，
                # 不主动 ping 就永远卡在 async for 里收不到事件（表现为“在线但不理人”）。
                # 30 秒无 pong 即抛异常，走下面的重连分支。
                async with session.ws_connect(NAPCAT_WS_URL, heartbeat=30) as ws:
                    global current_ws
                    current_ws = ws
                    reconnect_fails = 0
                    logger.info("已连接到 NapCat WebSocket，开始监听消息。")
                    # 补历史必须等主循环开始读 ws 之后再发：call_action 靠主循环分发 echo，
                    # 在这里直接 await 会把监听堵死，请求必然超时（实测 status=None）
                    async def _delayed_backfill(sock):
                        await asyncio.sleep(2.0)
                        for _gid in list(ALLOWED_GROUPS):
                            await backfill_group_history(sock, _gid)
                            await asyncio.sleep(0.3)
                    asyncio.create_task(_delayed_backfill(ws))
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

                    # 连接看门狗：半开连接时 async for 会永远静默等待，
                    # 每分钟主动 get_status 探活，连续两次失败就掐断连接逼外层重连。
                    async def _conn_watchdog(_ws):
                        fails = 0
                        while not _ws.closed:
                            await asyncio.sleep(60)
                            if _ws.closed:
                                return
                            res = await call_action(_ws, "get_status", {}, timeout=8.0)
                            if res and res.get("status") in ("ok", "async"):
                                fails = 0
                                continue
                            fails += 1
                            logger.warning("NapCat 连接探活失败 (%d/2)", fails)
                            if fails >= 2:
                                logger.warning("NapCat 连接已失效，主动断开触发重连")
                                await _ws.close()
                                return
                    asyncio.create_task(_conn_watchdog(ws))

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
                                        spawn(handle_group_message(ws, event))
                                    elif msg_type == "private":
                                        _t = spawn(handle_private_message(ws, event))
                                        track_reply_task(event.get("message_id"), _t)
                                elif post_type in ("notice", "notify"):
                                    # 监听群戳一戳通知
                                    logger.info("收到系统/群通知事件: %s", event)
                                    spawn(handle_notice_event(ws, event))
                            except Exception as e:
                                logger.error("解析事件异常: %s", e)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            # 带上关闭码：只写「连接已断开」查不出是 NapCat 踢的还是网络抖的
                            logger.warning("WebSocket 连接关闭或异常（type=%s, close_code=%s, data=%s），准备重连...",
                                           msg.type, getattr(ws, "close_code", None), str(msg.data)[:120])
                            break
        except Exception as e:
            logger.error("连接失败或发生错误: %s", e)
        finally:
            # 不管正常关闭还是异常退出，都要清掉旧连接句柄和挂起的请求；
            # 否则调用方会拿着已关闭的 ws 继续发，future 也只能干等超时。
            current_ws = None
            for fut in list(pending_requests.values()):
                if not fut.done():
                    # 原来用 fut.cancel()：等这个 future 的 call_action 会收到 CancelledError，
                    # 那是 BaseException，`except Exception` 抓不到，整条回复任务直接以 cancelled
                    # 结束，spawn 的 _on_background_done 见到 cancelled 就 return——用户 @ 了机器人
                    # 却什么都没发生，日志里连一行都没有。改成塞异常，走 call_action 的 None 契约。
                    # 用 set_result(None) 而不是 set_exception：call_action 的契约本来就是
                    # 失败返回 None，塞异常只会在 GC 时多甩一句「exception was never retrieved」
                    fut.set_result(None)
            pending_requests.clear()
        # 正常关闭和异常走同一条退避路径，避免服务端一断就变成零间隔重连风暴
        if reconnect_fails:
            wait = min(60.0, 2 ** min(reconnect_fails, 5))
        else:
            wait = 5.0
        reconnect_fails += 1
        logger.warning("连接已断开，%d 秒后重连（第 %d 次）...", wait, reconnect_fails)
        await asyncio.sleep(wait)


def cold_nudge_rollback(gid: int, prev: dict) -> None:
    """冷场冒泡没真发出去时回滚账本。

    扣名额、"刚活跃"时间戳都在生成之前写，上游 5xx 或出站闸静默时一条都没发出去，
    却照样占掉当天的冒泡额度、还把群伪装成刚活跃，两次抖动就能让这个群整天不再开口。
    """
    group_nudge_count[gid] = max(0, group_nudge_count.get(gid, 1) - 1)
    group_last_nudge_ts[gid] = prev.get("nudge_ts", 0.0)
    group_last_active_reply[gid] = prev.get("active", 0.0)
    group_last_msg_time[gid] = prev.get("msg", 0.0)
    logger.info("[群 %d] 冷场冒泡没发出去，名额与活跃时间已回滚", gid)


def _install_signal_handlers() -> None:
    """SIGTERM 走和 Ctrl-C 同一条退出路径。

    systemd stop/restart、OOM killer 发的都是 SIGTERM，Python 默认直接终止，
    `finally` 一行都不跑：最多丢 10s 的对话记忆，更久的是 /stop 静默和禁言保护
    （300s 才存一次）——刚静默完一重启，机器人在群里又是满血复活。
    """
    def _handler(signum, frame):
        raise KeyboardInterrupt

    for sig_name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            # 非主线程 / 平台不支持时忽略，别为了收尾把启动搞崩
            pass


async def _run_service() -> None:
    """包一层只为了在事件循环还活着的时候做异步收尾。"""
    try:
        await main()
    finally:
        # 关了 loop 再 close 不了 session，aiohttp 退出时就会甩一句
        # "Unclosed client session"——每次都像是出了故障，其实是收尾没做完
        try:
            if http_session is not None and not http_session.closed:
                await http_session.close()
        except Exception:
            pass


if __name__ == "__main__":
    _install_signal_handlers()
    try:
        asyncio.run(_run_service())
    except KeyboardInterrupt:
        logger.info("服务已手动退出。")
    finally:
        # 退出前把攒着的记忆、账本、运行时保护状态落盘，别丢最后几分钟的数据
        flush_user_memories()
        affect.save(force=True)
        token_tracker.save()
        try:
            save_runtime_state()
        except Exception as e:
            logger.warning("退出时保存运行时状态失败: %s", e)
