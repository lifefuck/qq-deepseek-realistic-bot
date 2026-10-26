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
import sys
import time
import datetime
import uuid
from typing import Any, Deque, Dict, Optional, Set, Tuple
import aiohttp
import web_dashboard
import games
import connectors
import cross_memory
import fun_features
import group_stats
import media_resolver
from meme_manager import meme_manager
from prompts import (SYSTEM_PROMPT, ROOT_SYSTEM_PROMPT, GAMES_KB, needs_games_kb,
                     repo_ask_reply, _POKE_QUIPS, _POKE_ADMIN_QUIPS)
import meme_catalog
import safety_guard
import cross_interaction
from services import trace
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

# 把当前模块交给服务层。python bot.py 跑起来时 __main__ 和 bot 是两个不同的模块对象，
# 服务层自己 import bot 会读到另一份全局状态，所以只认这里注入的实例。
for _mod in (_svc_messaging, _svc_llm, _h_admin, _h_private, _h_group, fun_features):
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

# 拟真作息睡眠参数 (23:00 ~ 07:00 CST 为入睡期)
SLEEP_START_HOUR = 23
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
# 每个群各自记一份最近发言，跨群混在一起会让 A 群看到 B 群说过的话
recent_bot_replies: Dict[int, Deque[str]] = collections.defaultdict(lambda: collections.deque(maxlen=8))

# 自称「本大肥鱼/本鲸宝」这类，属于人设词空转，盯的是它
_SELF_REF_RE = re.compile(r"本(?:大)?(?:肥)?(?:鱼|鲸宝|鲸鱼|鲸)|我(?:大)?(?:肥鱼|鲸宝)|大肥鱼本(?:鱼|人)")

BOT_KEYWORDS = {
    "大肥鱼", "肥鱼", "小鲸鱼", "鲸鱼", "鲸宝", "deepseek", "v3", "r1", 
    "bot", "机器人", "ai", "人工智障", "人工智能", "什么生物", "回消息", "理人", "说话",
    "笨蛋", "傻子", "傻鱼", "小鱼", "死鱼", "咸鱼"
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
    "muted": False,             # /off 一键静音的总闸：群友说什么都不回，管理员指令照旧
    "enable_interject": True,   # 是否允许主动插话
    "interject_prob": 0.35,     # 主动插话基准概率 (0.0 ~ 1.0)
    "enable_sleep": True,       # 是否启用 23:00~07:00 拟真作息
    "enable_poke": True,        # 是否开启戳一戳互动与回戳
    "enable_games": True,       # 是否开启数字炸弹/掷骰子等文字游戏
    "enable_search": True,      # 是否开启联网热点检索 (鸭鸭搜/B站)
    "enable_bilibili_push": False, # 是否开启定时热门视频推送 (默认关)
    "enable_yunzai": True,      # 是否开启云崽游戏生态 (原神/星铁/米游社)
    "enable_fun": True          # 年鉴/关系网/图灵等群玩法，AI 自主决定何时发起
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


def de_template(reply: str, group_id: int = 0) -> str:
    """只干一件事：同一句话原样再发一次就挡掉。说话风格交给模型自己判断。"""
    text = reply.strip()
    if not text:
        return reply
    skel = _skeleton(text)
    if skel and any(_skeleton(r) == skel for r in recent_bot_replies[group_id]):
        segs = [s for s in re.split(r"[，,。！？]", text) if s.strip()]
        if len(segs) > 1:
            # 第一段太短（"吵死啦""好家伙"）就带上第二段，别削成俩字
            keep = segs[:2] if len(segs[0]) < 6 else segs[:1]
            text = "，".join(keep)
            logger.info("[反模板闸] 整句复读，削短: %r", text)
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
_OBFUSCATE_SKIP_HOSTS = ("github.com", "githubusercontent.com", "gitee.com", "gitcode.com")
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
        # 代码托管站是白名单：QQ 不吞，插字反而让地址没法直接点
        if any(host == h or host.endswith("." + h) for h in _OBFUSCATE_SKIP_HOSTS):
            return m.group(0)
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

    # 先戳回去再说话：对方戳完的即时反馈就是回戳，别等模型生成完再补一刀
    await poke_back(ws, group_id, user_id)

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
async def _stats_keeper():
    """统计落盘 + 每周一凌晨结算。只挂一次，扔在重连循环外面。"""
    import datetime as _dt
    last_mark = ""
    while True:
        try:
            await asyncio.sleep(300)
            group_stats.maybe_flush()
            now = _dt.datetime.now()
            mark = now.strftime("%Y-%W")
            if now.weekday() == 0 and now.hour == 0 and mark != last_mark:
                last_mark = mark
                ws = current_ws
                usable = ws if (ws is not None and not ws.closed) else None
                await fun_features.weekly_settle_all(usable)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("[统计守护] 异常: %s", e)


async def main():
    logger.info("启动 QQ DeepSeek 群聊 & 私聊机器人...")
    logger.info("白名单群: %s | 机器人 QQ: %d | 模型: %s | 管理员: %d", list(ALLOWED_GROUPS), BOT_QQ, MODEL_NAME, ADMIN_QQ)

    group_stats.load()
    asyncio.create_task(_stats_keeper())

    # 启动轻量现代浅色 Web 仪表盘（只绑 127.0.0.1，外网走 Cloudflare 隧道）
    try:
        await web_dashboard.start_web_server(get_bot_dashboard_status, update_config_func=update_group_config, port=8088)
        cross_memory.cross_group_mgr.start_background_flusher()
        logger.info("🌐 Web 仪表盘已启动，本机监听: http://127.0.0.1:8088")
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
