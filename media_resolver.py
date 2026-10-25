"""
短链展开与多媒体内容语义解析模块
支持: 抖音 (v.douyin.com)、小红书 (xhslink.com)、B站短链 (b23.tv)
自动 302 跟随重定向展开、提取视频标题、作者与无水印媒体信息。
"""
import aiohttp
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger("DeepSeekBot")

USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"

async def resolve_short_url(session: aiohttp.ClientSession, short_url: str) -> str:
    """跟随 HTTP 重定向展开真实落地页 URL"""
    try:
        headers = {"User-Agent": USER_AGENT}
        async with session.get(short_url, headers=headers, allow_redirects=True, timeout=6.0) as resp:
            return str(resp.url)
    except Exception as e:
        logger.debug("展开短链失败: %s (%s)", short_url, e)
        return short_url

async def parse_media_content(session: aiohttp.ClientSession, text: str) -> Optional[Dict[str, Any]]:
    """检测并解析文本中的主流短视频/图文分享卡片"""
    # 1. 检测 B 站短链 b23.tv
    m_bili = re.search(r"https?://(?:b23\.tv|bili2233\.cn)/[a-zA-Z0-9]+", text)
    if m_bili:
        real_url = await resolve_short_url(session, m_bili.group(0))
        m_bv = re.search(r"(BV[a-zA-Z0-9]{10})", real_url)
        if m_bv:
            bvid = m_bv.group(1)
            # 请求 B 站官方接口获取视频详细标题与简介
            try:
                api = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
                async with session.get(api, timeout=5.0) as r:
                    if r.status == 200:
                        data = await r.json()
                        vdata = data.get("data", {})
                        title = vdata.get("title", "")
                        desc = vdata.get("desc", "")
                        up = vdata.get("owner", {}).get("name", "")
                        return {
                            "type": "bilibili",
                            "title": title,
                            "author": up,
                            "summary": f"[B站视频] 《{title}》- UP主: {up}\n简介: {desc[:80]}"
                        }
            except Exception:
                pass

    # 2. 检测抖音分享口令 / 短链 v.douyin.com
    m_douyin = re.search(r"https?://v\.douyin\.com/[a-zA-Z0-9_-]+/?", text)
    if m_douyin:
        real_url = await resolve_short_url(session, m_douyin.group(0))
        # 尝试请求 HTML 抽取 og:title
        try:
            async with session.get(real_url, headers={"User-Agent": USER_AGENT}, timeout=6.0) as r:
                if r.status == 200:
                    html = await r.text()
                    m_title = re.search(r'<title>([^<]+)</title>', html)
                    m_desc = re.search(r'<meta\s+name="description"\s+content="([^"]*)"', html)
                    title = m_title.group(1).replace(" - 抖音", "").strip() if m_title else ""
                    desc = m_desc.group(1).strip() if m_desc else ""
                    return {
                        "type": "douyin",
                        "title": title,
                        "summary": f"[抖音作品] 文案: {title or desc[:100]}"
                    }
        except Exception:
            pass

    # 3. 检测小红书短链 xhslink.com
    m_xhs = re.search(r"https?://xhslink\.com/[a-zA-Z0-9/_-]+", text)
    if m_xhs:
        real_url = await resolve_short_url(session, m_xhs.group(0))
        try:
            async with session.get(real_url, headers={"User-Agent": USER_AGENT}, timeout=6.0) as r:
                if r.status == 200:
                    html = await r.text()
                    m_title = re.search(r'<title>([^<]+)</title>', html)
                    title = m_title.group(1).replace(" - 小红书", "").strip() if m_title else ""
                    return {
                        "type": "xiaohongshu",
                        "title": title,
                        "summary": f"[小红书笔记] 标题: {title[:80]}"
                    }
        except Exception:
            pass

    return None
