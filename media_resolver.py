"""
短链展开与多媒体内容语义解析模块
支持: 抖音 (v.douyin.com)、小红书 (xhslink.com)、B站短链 (b23.tv)
自动 302 跟随重定向展开、提取视频标题、作者与无水印媒体信息。

群友可以直接往群里贴链接，所以这些地址都是用户可控的。
所有请求先过公网校验：内网、回环、链路本地一律拒绝；重定向一跳一跳重新校验，
否则公网域名 302 到 10.x 就能绕过第一道检查。
"""
import aiohttp
import asyncio
import ipaddress
import json
import logging
import re
import socket
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse

logger = logging.getLogger("DeepSeekBot")

USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"

MAX_REDIRECTS = 3


def _is_public_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def _scheme_and_host(url: str) -> Tuple[str, str]:
    parsed = urlparse(url)
    return parsed.scheme, (parsed.hostname or "").lower()


async def _is_public_url(url: str) -> bool:
    """主机名要解析成真实 IP 再判断，只看字符串挡不住 DNS 指向内网。"""
    try:
        scheme, host = _scheme_and_host(url)
    except ValueError:
        return False
    if scheme not in ("http", "https") or not host:
        return False
    if host == "localhost":
        return False
    try:
        ipaddress.ip_address(host)
        return _is_public_ip(host)          # 主机名本身就是 IP，不用查 DNS
    except ValueError:
        pass
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
        return bool(infos) and all(_is_public_ip(info[4][0]) for info in infos)
    except Exception:
        return False


async def safe_fetch(
    session: aiohttp.ClientSession,
    url: str,
    *,
    timeout: float = 6.0,
    max_hops: int = MAX_REDIRECTS,
) -> Optional[Tuple[str, str]]:
    """抓公网页面，返回 (最终地址, 正文)。不安全或抓不到都返回 None。"""
    current = url
    for _ in range(max_hops + 1):
        if not await _is_public_url(current):
            logger.warning("拒绝请求非公网地址: %s", current)
            return None
        try:
            async with session.get(
                current, headers={"User-Agent": USER_AGENT},
                timeout=timeout, allow_redirects=False
            ) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if not location:
                        return None
                    current = urljoin(current, location)
                    continue
                if resp.status != 200:
                    return None
                return str(resp.url), await resp.text(errors="ignore")
        except Exception as e:
            logger.debug("抓取失败 %s: %s", current, e)
            return None
    return None


async def resolve_short_url(session: aiohttp.ClientSession, short_url: str) -> str:
    """跟随 HTTP 重定向展开真实落地页 URL"""
    fetched = await safe_fetch(session, short_url)
    return fetched[0] if fetched else short_url


async def parse_media_content(session: aiohttp.ClientSession, text: str) -> Optional[Dict[str, Any]]:
    """检测并解析文本中的主流短视频/图文分享卡片"""
    # 1. 检测 B 站短链 b23.tv 或 bili2233.cn 或直接包含的 BV 号
    bvid = None
    m_bili = re.search(r"https?://(?:b23\.tv|bili2233\.cn)/[a-zA-Z0-9]+", text)
    if m_bili:
        real_url = await resolve_short_url(session, m_bili.group(0))
        m_bv = re.search(r"(BV[a-zA-Z0-9]{10})", real_url, re.IGNORECASE)
        if m_bv:
            bvid = m_bv.group(1)
    else:
        # 兼容直接发 bilibili.com/video/BV... 或群友纯发 BV 号
        m_bv = re.search(r"\b(BV[a-zA-Z0-9]{10})\b", text, re.IGNORECASE)
        if m_bv:
            bvid = m_bv.group(1)

    if bvid:
        # 请求 B 站官方接口获取视频详细标题与简介
        fetched = await safe_fetch(
            session, f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", timeout=5.0
        )
        if fetched:
            try:
                vdata = json.loads(fetched[1]).get("data", {})
            except (json.JSONDecodeError, AttributeError):
                vdata = {}
            title = vdata.get("title", "")
            desc = vdata.get("desc", "")
            up = vdata.get("owner", {}).get("name", "")
            if title:
                return {
                    "type": "bilibili",
                    "title": title,
                    "author": up,
                    "summary": f"[B站视频] 《{title}》- UP主: {up}\n简介: {desc[:80]}"
                }

    # 2. 检测抖音分享口令 / 短链 v.douyin.com
    m_douyin = re.search(r"https?://v\.douyin\.com/[a-zA-Z0-9_-]+/?", text)
    if m_douyin:
        real_url = await resolve_short_url(session, m_douyin.group(0))
        fetched = await safe_fetch(session, real_url)
        if fetched:
            html = fetched[1]
            m_title = re.search(r'<title>([^<]+)</title>', html)
            m_desc = re.search(r'<meta\s+name="description"\s+content="([^"]*)"', html)
            title = m_title.group(1).replace(" - 抖音", "").strip() if m_title else ""
            desc = m_desc.group(1).strip() if m_desc else ""
            return {
                "type": "douyin",
                "title": title,
                "summary": f"[抖音作品] 文案: {title or desc[:100]}"
            }

    # 3. 检测小红书短链 xhslink.com
    m_xhs = re.search(r"https?://xhslink\.com/[a-zA-Z0-9/_-]+", text)
    if m_xhs:
        real_url = await resolve_short_url(session, m_xhs.group(0))
        fetched = await safe_fetch(session, real_url)
        if fetched:
            html = fetched[1]
            m_title = re.search(r'<title>([^<]+)</title>', html)
            title = m_title.group(1).replace(" - 小红书", "").strip() if m_title else ""
            return {
                "type": "xiaohongshu",
                "title": title,
                "summary": f"[小红书笔记] 标题: {title[:80]}"
            }

    # 4. 检测 GitHub 仓库或文件/issue链接 (github.com/owner/repo)
    m_gh = re.search(r"https?://github\.com/([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+)", text)
    if m_gh:
        owner = m_gh.group(1)
        repo = m_gh.group(2).rstrip("/").removesuffix(".git")
        # 排除保留路径
        if owner.lower() not in ("topics", "features", "collections", "events", "sponsors", "settings", "explore"):
            fetched = await safe_fetch(
                session, f"https://api.github.com/repos/{owner}/{repo}", timeout=5.0
            )
            if fetched:
                try:
                    gh_data = json.loads(fetched[1])
                except (json.JSONDecodeError, AttributeError):
                    gh_data = {}
                full_name = gh_data.get("full_name") or f"{owner}/{repo}"
                description = gh_data.get("description") or "暂无项目描述"
                stars = gh_data.get("stargazers_count", 0)
                forks = gh_data.get("forks_count", 0)
                lang = gh_data.get("language") or "未知"
                return {
                    "type": "github",
                    "title": full_name,
                    "author": owner,
                    "summary": f"[GitHub开源项目] {full_name} ({lang} | ★{stars} | ⑂{forks})\n简介: {description[:100]}"
                }

    # 5. 通用网页链接解析 (http/https)，提取 title 与 meta description 参与讨论
    # 同时针对 GitHub、微信公众号、知乎、微博等提供结构化清洗
    m_url = re.search(r"https?://[^\s<>\"'，。！？（）\[\]]+", text)
    if m_url:
        target_url = m_url.group(0).rstrip(".,;:!?，。！？")
        fetched = await safe_fetch(session, target_url, timeout=5.0)
        if fetched:
            final_url, html = fetched
            m_title = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
            title = m_title.group(1).strip() if m_title else ""
            m_desc = re.search(r'<meta\s+[^>]*name=[\"\']description[\"\']\s+content=[\"\']([^\"\']*)[\"\']', html, re.IGNORECASE)
            if not m_desc:
                m_desc = re.search(r'<meta\s+[^>]*property=[\"\']og:description[\"\']\s+content=[\"\']([^\"\']*)[\"\']', html, re.IGNORECASE)
            desc = m_desc.group(1).strip() if m_desc else ""
            
            # 尝试正文简要提炼（提取首个有意义的段落或正文文字，省流给群友）
            body_snippet = ""
            p_matches = re.findall(r'<p[^>]*>([\s\S]*?)</p>', html, re.IGNORECASE)
            for p_raw in p_matches:
                clean_p = re.sub(r'<[^>]+>', '', p_raw).strip()
                clean_p = re.sub(r'[\r\n\t\s]+', ' ', clean_p)
                if len(clean_p) >= 20 and not any(k in clean_p for k in ["ICP", "版权所有", "Copyright", "cookie", "javascript"]):
                    body_snippet = clean_p[:150]
                    break

            if title or desc or body_snippet:
                clean_title = re.sub(r'[\r\n\t]+', ' ', title)[:100]
                summary_parts = []
                if clean_title:
                    summary_parts.append(f"标题: {clean_title}")
                if desc:
                    clean_desc = re.sub(r'[\r\n\t]+', ' ', desc)[:120]
                    summary_parts.append(f"简介: {clean_desc}")
                elif body_snippet:
                    summary_parts.append(f"摘要: {body_snippet}")
                return {
                    "type": "webpage",
                    "title": clean_title,
                    "summary": f"[分享网页省流] {' | '.join(summary_parts)}"
                }

    return None
