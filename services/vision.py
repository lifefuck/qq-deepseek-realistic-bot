import os
import re
import time
import base64
import logging
import aiohttp
from typing import Optional, Dict, Tuple

from media_resolver import _is_public_url, _read_body

logger = logging.getLogger(__name__)

# Google Gemini 配置
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
PROXY_URL = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or "http://127.0.0.1:7890"

# 模型降级备用列表（按优先级尝试，默认优先 lite 避开配额拥堵与 429 降级等待）
FALLBACK_MODELS = [
    GEMINI_MODEL,
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.8-flash",
]

# 整条识图链路的总预算（秒）。识图是在同群锁内同步等待的，
# 单个模型给 10s、五个模型连着试，最坏能把后面前来排队的消息压住 40s+，
# 群里看到的就是"机器人延迟很高"。所以改成整条链路一个总预算，超了就放弃。
VISION_TOTAL_BUDGET = float(os.environ.get("VISION_TOTAL_BUDGET", "12"))
# 单次请求上限，必须小于总预算，失败才来得及降级
VISION_ATTEMPT_TIMEOUT = float(os.environ.get("VISION_ATTEMPT_TIMEOUT", "8"))


def _clean_desc(text: str) -> str:
    """去掉首尾引号/代码块残留。模型截断时常在开头留半个引号"""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"```$", "", t)
    return t.strip().strip('"\'“”').strip()


def _looks_like_desc(text: str, finish_reason: Optional[str] = None) -> bool:
    """判断一段识图返回是不是能用的描述。

    之前只判 `if text:`，于是被截断的残句（真实案例：`" * Status box`）也算成功，
    下游拿着这句空气当"看见了图"，只能接着十几分钟前的老话题硬聊，
    表现为答非所问、看起来像延迟。宁可不识图，也别把残句喂给主模型。
    """
    t = _clean_desc(text)
    if not t:
        return False
    # finishReason 不是 STOP 基本等于被截断/被打断
    if finish_reason and finish_reason not in ("STOP", "MAX_TOKENS"):
        return False
    visible = re.sub(r"[\s\W_]+", "", t)
    if len(visible) < 6:
        return False
    # 有效字符占比太低说明是符号碎片（引号、列表符、换行）
    if len(visible) / max(len(t), 1) < 0.4:
        return False
    # 中文是主语言：没有中文时要求足够长，否则半句英文 fragment 比如 "Status box" 会漏过去
    if not re.search(r"[\u4e00-\u9fff]", t) and len(visible) < 25:
        return False
    return True

async def download_image_as_base64(session: aiohttp.ClientSession, image_url: str) -> Optional[tuple[str, str]]:
    """下载图片并转换为 base64 及对应 mime_type（若 URL 需要翻墙则附带代理）"""
    try:
        # 图片地址来自群消息，任何人可伪造 CQ 码指向内网，先过公网校验
        if not await _is_public_url(image_url):
            logger.warning("[Vision] 拒绝非公网图片地址: %s", image_url[:80])
            return None
        # QQ 图片直链一般无需代理，如遇到 google 或海外图片链接则可选代理
        proxy = PROXY_URL if "google" in image_url or "github" in image_url else None
        async with session.get(image_url, proxy=proxy, timeout=aiohttp.ClientTimeout(total=10.0)) as resp:
            if resp.status != 200:
                logger.warning("[Vision] 下载图片失败 HTTP %d: %s", resp.status, image_url)
                return None
            # 8MB 上限：base64 还要再涨 1/3，不能让它把内存吃掉
            data = await _read_body(resp, 8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                logger.warning("[Vision] 图片超过 8MB，放弃: %s", image_url[:80])
                return None
            if not data:
                return None
            
            # 简单判断 mime 类型
            mime_type = "image/jpeg"
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                mime_type = "image/png"
            elif data.startswith(b"GIF8"):
                mime_type = "image/gif"
            elif data.startswith(b"RIFF") and b"WEBP" in data[:16]:
                mime_type = "image/webp"

            b64_str = base64.b64encode(data).decode("utf-8")
            return b64_str, mime_type
    except Exception as e:
        logger.warning("[Vision] 下载或转换图片异常: %s", e)
        return None

# 全局图片描述内存缓存 (url_md5 -> (desc, timestamp))，防止短时间内重复识图浪费 token
_vision_cache: Dict[str, Tuple[str, float]] = {}

async def describe_image(
    session: aiohttp.ClientSession,
    image_url: str,
    # 只要白描：说清看得见的东西就够。以前那句「这张图想表达什么」会逼它猜意图，
    # 下游模型再顺着猜出来的意图接话，就变成莫名其妙的发挥。
    # 也不给任何情绪/类别暗示（搞笑、梗图、擦边…），免得先把下游的思路带歪。
    user_prompt: str = (
        "用两三句话白描这张图，60 字以内：谁/什么在做什么，关键的物件和场景，"
        "图上的字原样抄下来。只写看得见的，不解释含义、不评价好坏、不猜出处和来历。"
        "认不出是谁就直说认不出。"
    ),
    deadline: Optional[float] = None,
) -> Optional[str]:
    """使用 Google Gemini 极速视觉模型识别图片内容（带内存缓存）

    deadline: 绝对时间戳（time.time() 口径）。同一条消息的多张图共用同一个 deadline，
    避免一张图超时后第二张再重新给满预算。
    """
    import hashlib
    url_hash = hashlib.md5(image_url.encode("utf-8")).hexdigest()
    now = time.time()
    if url_hash in _vision_cache:
        desc, ts = _vision_cache[url_hash]
        if now - ts < 1800.0:  # 30 分钟缓存
            return desc
    api_key = os.environ.get("GEMINI_API_KEY") or GEMINI_API_KEY
    if not api_key:
        logger.warning("[Vision] 未配置 GEMINI_API_KEY，跳过识图")
        return None

    img_res = await download_image_as_base64(session, image_url)
    if not img_res:
        return None
    b64_data, mime_type = img_res

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": user_prompt},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": b64_data
                        }
                    }
                ]
            }
        ],
        "generationConfig": {
            "maxOutputTokens": 300,
            "temperature": 0.3
        }
    }

    t0 = time.time()
    if deadline is None:
        deadline = t0 + VISION_TOTAL_BUDGET
    models_to_try = [GEMINI_MODEL] + [m for m in FALLBACK_MODELS if m != GEMINI_MODEL]

    for model_name in models_to_try:
        remaining = deadline - time.time()
        # 剩余时间不够一次像样的请求就别开新模型了，宁可没有描述也别继续占着同群锁
        if remaining < 1.5:
            logger.warning("[Vision] 总预算 %.1fs 已用尽，放弃识图（已试 %s）", VISION_TOTAL_BUDGET, model_name)
            break
        attempt_timeout = max(1.5, min(VISION_ATTEMPT_TIMEOUT, remaining))
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        try:
            async with session.post(
                url,
                json=payload,
                proxy=PROXY_URL,
                timeout=aiohttp.ClientTimeout(total=attempt_timeout)
            ) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    logger.warning("[Vision] Gemini (%s) 识图 API 错误 HTTP %d: %s", model_name, resp.status, err_text[:80])
                    continue
                res_json = await resp.json()
                if res_json.get("promptFeedback", {}).get("blockReason"):
                    logger.warning("[Vision] Gemini (%s) 被安全策略拦截: %s", model_name, res_json["promptFeedback"]["blockReason"])
                    continue
                candidates = res_json.get("candidates", [])
                if not candidates:
                    continue
                finish_reason = candidates[0].get("finishReason")
                parts = candidates[0].get("content", {}).get("parts", [])
                if not parts:
                    logger.warning("[Vision] Gemini (%s) 返回空 parts (finishReason=%s)", model_name, finish_reason)
                    continue
                text = _clean_desc(parts[0].get("text", ""))
                if not _looks_like_desc(text, finish_reason):
                    # 残句不能当成功：继续降级，让下一个模型再试
                    logger.warning(
                        "[Vision] Gemini (%s) 返回残句，当失败处理 (finishReason=%s, %.2fs): %r",
                        model_name, finish_reason, time.time() - t0, text[:60],
                    )
                    continue
                logger.info("[Vision] Gemini (%s) 识图成功 (耗时 %.2fs): %s", model_name, time.time() - t0, text[:60])
                _vision_cache[url_hash] = (text, now)
                if len(_vision_cache) > 200:
                    oldest_key = min(_vision_cache, key=lambda k: _vision_cache[k][1])
                    _vision_cache.pop(oldest_key, None)
                return text
        except Exception as e:
            logger.warning("[Vision] 请求 Gemini (%s) 视觉接口失败 (%.2fs): %r", model_name, time.time() - t0, e)
            continue

    return None
