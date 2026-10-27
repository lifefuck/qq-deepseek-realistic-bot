import os
import time
import base64
import logging
import aiohttp
from typing import Optional, Dict, Tuple

logger = logging.getLogger(__name__)

# Google Gemini 配置
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
PROXY_URL = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or "http://127.0.0.1:7890"

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
    user_prompt: str = "简要客观地描述这张图片中的关键内容、人物/角色或画面文字（50字以内）："
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

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
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
            "maxOutputTokens": 150,
            "temperature": 0.4
        }
    }

    t0 = time.time()
    try:
        async with session.post(
            url,
            json=payload,
            proxy=PROXY_URL,
            timeout=aiohttp.ClientTimeout(total=15.0)
        ) as resp:
            if resp.status != 200:
                err_text = await resp.text()
                logger.warning("[Vision] Gemini 识图 API 错误 HTTP %d: %s", resp.status, err_text[:100])
                return None
            res_json = await resp.json()
            candidates = res_json.get("candidates", [])
            if not candidates:
                return None
            parts = candidates[0].get("content", {}).get("parts", [])
            if not parts:
                return None
            text = parts[0].get("text", "").strip()
            logger.info("[Vision] Gemini 识图成功 (耗时 %.2fs): %s", time.time() - t0, text[:60])
            if text:
                _vision_cache[url_hash] = (text, now)
                if len(_vision_cache) > 200:
                    oldest_key = min(_vision_cache, key=lambda k: _vision_cache[k][1])
                    _vision_cache.pop(oldest_key, None)
            return text
    except Exception as e:
        logger.warning("[Vision] 请求 Gemini 视觉接口失败: %s", e)
        return None
