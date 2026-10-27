import os
import time
import base64
import logging
import aiohttp
from typing import Optional, Dict, Tuple

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

async def download_image_as_base64(session: aiohttp.ClientSession, image_url: str) -> Optional[tuple[str, str]]:
    """下载图片并转换为 base64 及对应 mime_type（若 URL 需要翻墙则附带代理）"""
    try:
        # QQ 图片直链一般无需代理，如遇到 google 或海外图片链接则可选代理
        proxy = PROXY_URL if "google" in image_url or "github" in image_url else None
        async with session.get(image_url, proxy=proxy, timeout=aiohttp.ClientTimeout(total=10.0)) as resp:
            if resp.status != 200:
                logger.warning("[Vision] 下载图片失败 HTTP %d: %s", resp.status, image_url)
                return None
            data = await resp.read()
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
    user_prompt: str = (
        "你是一个精通动漫、二次元、游戏与中国互联网流行文化的资深老网民。"
        "请精准提炼这张图片的核心信息（80~120字左右）：\n"
        "1. 核心角色与出处：如果是动漫/游戏/二次元/影视角色或知名人物，必须明确指出人物名字和所属作品（如果不确定可指出最接近的特征或风格）；\n"
        "2. 关键文字与表情/动作：提取画面中出现的全部关键配字、人物的神态动作与心理潜台词；\n"
        "3. 梗与情绪槽点：说明这张梗图/表情包的核心笑点、槽点或在传达什么情绪（如嘲讽、破防、摆烂、吃瓜、搞怪等）。"
    )
) -> Optional[str]:
    """使用 Google Gemini 极速视觉模型识别图片内容（带内存缓存）"""
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
    models_to_try = [GEMINI_MODEL] + [m for m in FALLBACK_MODELS if m != GEMINI_MODEL]

    for model_name in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        try:
            async with session.post(
                url,
                json=payload,
                proxy=PROXY_URL,
                timeout=aiohttp.ClientTimeout(total=10.0)
            ) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    logger.warning("[Vision] Gemini (%s) 识图 API 错误 HTTP %d: %s", model_name, resp.status, err_text[:80])
                    continue
                res_json = await resp.json()
                candidates = res_json.get("candidates", [])
                if not candidates:
                    continue
                parts = candidates[0].get("content", {}).get("parts", [])
                if not parts:
                    continue
                text = parts[0].get("text", "").strip()
                logger.info("[Vision] Gemini (%s) 识图成功 (耗时 %.2fs): %s", model_name, time.time() - t0, text[:60])
                if text:
                    _vision_cache[url_hash] = (text, now)
                    if len(_vision_cache) > 200:
                        oldest_key = min(_vision_cache, key=lambda k: _vision_cache[k][1])
                        _vision_cache.pop(oldest_key, None)
                return text
        except Exception as e:
            logger.warning("[Vision] 请求 Gemini (%s) 视觉接口失败: %s", model_name, e)
            continue

    return None
