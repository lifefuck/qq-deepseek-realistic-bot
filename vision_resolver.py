#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻量视觉转录模块 (Vision Resolver)
遇到群友发图/表情包时，异步调用轻量视觉端点提取画面梗概（10~25 字以内），
以 [用户发送了图片: ...] 形式带入上下文，实现低成本看图接梗。
作者: life
"""

import aiohttp
import asyncio
import logging
from typing import Optional

logger = logging.getLogger("DeepSeekBot")

# 支持使用 SiliconFlow 上的轻量视觉模型（如 Pro/Qwen/Qwen2-VL-7B-Instruct 等）
VISION_API_URL = "https://api.siliconflow.cn/v1/chat/completions"
VISION_MODEL = "Qwen/Qwen2-VL-7B-Instruct"


async def transcribe_image_summary(session: aiohttp.ClientSession, api_key: str, image_url: str, timeout: float = 6.0) -> Optional[str]:
    """
    请求视觉模型对图片进行超短摘要提炼 (10-25字)
    """
    if not image_url or not api_key:
        return None

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    prompt = "简要描述这张图的核心内容或表情包配字（15字以内，纯文本，不加修饰，直接说明画面或梗）："

    payload = {
        "model": VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]
            }
        ],
        "max_tokens": 50,
        "temperature": 0.2
    }

    try:
        async with session.post(VISION_API_URL, headers=headers, json=payload, timeout=timeout) as resp:
            if resp.status == 200:
                data = await resp.json()
                choices = data.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "").strip()
                    # 清理换行与首尾引号
                    summary = content.replace("\n", " ").strip("\"'。 ")
                    if summary:
                        logger.info("视觉转录成功: %s -> %s", image_url[:45], summary)
                        return summary
            else:
                logger.debug("视觉端点响应异常状态码 %d", resp.status)
    except asyncio.TimeoutError:
        logger.debug("视觉转录超时 (%s s)，优雅降级跳过", timeout)
    except Exception as e:
        logger.debug("视觉转录失败: %s", e)

    return None
