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
import os
import re
import socket
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse

logger = logging.getLogger("DeepSeekBot")

# 匿名 API 每 IP 只有 60 次/小时，群里贴几个链接就到顶；有 token 是 5000 次
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

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


def connected_peer_is_public(resp: Any) -> bool:
    """复核这条连接真正连到了哪个 IP。

    `_is_public_url` 是先解析一次再过闸，建连时 aiohttp 又自己解析一次；
    群友用自己控 DNS 的域名（TTL=0）就能让第一次返回公网、第二次返回 127.0.0.1，
    校验形同虚设。这里直接看 socket 的对端地址，rebinding 也躲不过。
    拿不到对端信息时按原校验结果放行，不让它误伤正常抓取。
    """
    try:
        conn = getattr(resp, "connection", None)
        transport = getattr(conn, "transport", None)
        if transport is None:
            return True
        peer = transport.get_extra_info("peername")
        if not peer:
            return True
        ip = peer[0] if isinstance(peer, (tuple, list)) else str(peer)
        return _is_public_ip(ip)
    except Exception:
        return True


async def _read_body(resp: Any, limit: int = 2 * 1024 * 1024) -> bytes:
    """读完整个响应体（最多 limit 字节）。

    不能用 `resp.content.read(limit)` 一把梭：它的语义是「最多给这么多、手头有多少先返多少」，
    网络慢的时候会在半截就返回，JSON 解析出来就是残的（现场约 15% 概率，且不抛异常）。
    这里按块循环读到 EOF，既拿全又守住内存上限。
    """
    declared = resp.content_length
    if declared and declared > limit:
        # 超大文件只取前 limit，够提取 title/description 了
        return await resp.content.read(limit)
    chunks = []
    total = 0
    while total < limit:
        chunk = await resp.content.read(min(65536, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


async def safe_fetch(
    session: aiohttp.ClientSession,
    url: str,
    *,
    timeout: float = 6.0,
    max_hops: int = MAX_REDIRECTS,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Optional[Tuple[str, str]]:
    """抓公网页面，返回 (最终地址, 正文)。不安全或抓不到都返回 None。"""
    current = url
    headers = {"User-Agent": USER_AGENT}
    if extra_headers:
        headers.update(extra_headers)
    for _ in range(max_hops + 1):
        if not await _is_public_url(current):
            logger.warning("拒绝请求非公网地址: %s", current)
            return None
        try:
            async with session.get(
                current, headers=headers,
                timeout=timeout, allow_redirects=False
            ) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if not location:
                        return None
                    current = urljoin(current, location)
                    continue
                if not connected_peer_is_public(resp):
                    logger.warning("拒绝请求：实际连到的不是公网地址（疑似 DNS rebinding），放弃 %s", current)
                    return None
                if resp.status != 200:
                    return None
                # 上限 2MB：群友贴个大文件链接就能把内存和事件循环一起拖死
                raw = await _read_body(resp)
                return str(resp.url), raw.decode("utf-8", errors="ignore")
        except Exception as e:
            logger.debug("抓取失败 %s: %s", current, e)
            return None
    return None


async def _fetch_github_repo(
    session: aiohttp.ClientSession, owner: str, repo: str, *, attempts: int = 2
) -> Optional[Dict[str, Any]]:
    """取仓库元数据，拿不到就返回 None，让调用方降级成本就不发卡片。

    匿名 API 有两种常态失败：
    1. 响应体在传输中途被半截读走，JSON 解析失败（真正的根因是 content.read(n) 的语义，
       已由 _read_body 修掉，这里仍要校验，防止别的异常形状漏过去）；
    2. 每 IP 每小时 60 次限速，返回 {"message": ...}，里面没有仓库字段。
    两种情况都重试一次，还是不行就返回 None，绝不拿空数据拼「未知 | ★0」的卡片。
    """
    url = f"https://api.github.com/repos/{owner}/{repo}"
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    for attempt in range(attempts):
        fetched = await safe_fetch(session, url, timeout=5.0, extra_headers=headers)
        if not fetched:
            logger.warning("GitHub 仓库抓取失败(第%d次) %s/%s", attempt + 1, owner, repo)
            continue
        body = fetched[1]
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, AttributeError):
            # 截断的 JSON 到这里，长度和尾部足够定位问题
            logger.warning(
                "GitHub 返回 JSON 不完整(第%d次) %s/%s len=%d tail=%r",
                attempt + 1, owner, repo, len(body), body[-80:],
            )
            continue
        if isinstance(data, dict) and data.get("full_name"):
            return data
        # 限速/404 都是这种形状，把 message 带进日志才知道是哪种
        message = data.get("message") if isinstance(data, dict) else data
        logger.warning(
            "GitHub 未返回仓库数据(第%d次) %s/%s: %s",
            attempt + 1, owner, repo, str(message)[:120],
        )
    return None


async def resolve_short_url(session: aiohttp.ClientSession, short_url: str) -> str:
    """跟随 HTTP 重定向展开真实落地页 URL"""
    fetched = await safe_fetch(session, short_url)
    return fetched[0] if fetched else short_url


async def parse_media_content(session: aiohttp.ClientSession, text: str) -> Optional[Dict[str, Any]]:
    """检测并解析文本中的主流短视频/图文分享卡片"""
    # 1. 检测 B 站短链 b23.tv 或 bili2233.cn 或直接包含的 BV 号
    # 兼容 JSON 卡片中的反斜杠转义 (例如 https:\/\/b23.tv\/...)
    text_normalized = text.replace(r"\/", "/")
    bvid = None
    m_bili = re.search(r"https?://(?:b23\.tv|bili2233\.cn)/([a-zA-Z0-9]+)", text_normalized)
    if m_bili:
        real_url = await resolve_short_url(session, f"https://b23.tv/{m_bili.group(1)}")
        m_bv = re.search(r"(BV[a-zA-Z0-9]{10})", real_url, re.IGNORECASE)
        if m_bv:
            bvid = m_bv.group(1)
    else:
        # 兼容直接发 bilibili.com/video/BV... 或群友纯发 BV 号
        m_bv = re.search(r"\b(BV[a-zA-Z0-9]{10})\b", text_normalized, re.IGNORECASE)
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
    gh_failed = ""      # 记下仓库名，后面降级或兜底时用来告诉群友到底哪一步没抓到
    m_gh = re.search(r"https?://github\.com/([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+)", text)
    if m_gh:
        owner = m_gh.group(1)
        repo = m_gh.group(2).rstrip("/").removesuffix(".git")
        # 排除保留路径
        if owner.lower() not in ("topics", "features", "collections", "events", "sponsors", "settings", "explore"):
            gh_data = await _fetch_github_repo(session, owner, repo)
            if gh_data:
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
            # 拿不到元数据就往下走网页解析，别把空数据渲染成「未知 | ★0 | ⑂0」的假卡片
            gh_failed = f"{owner}/{repo}"

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
                # 仓库接口没抓到才走到这里，得让群友知道是抓取失败而不是项目本来没简介
                if gh_failed:
                    head = f"[GitHub] {gh_failed} 接口没抓到（限速或超时），先给个网页省流"
                else:
                    head = "[分享网页省流]"
                return {
                    "type": "github" if gh_failed else "webpage",
                    "title": clean_title,
                    "summary": f"{head} {' | '.join(summary_parts)}"
                }

    # GitHub 链接连网页都没打开：明确说是抓取失败，别让群友以为仓库是空的
    if gh_failed:
        return {
            "type": "github",
            "title": gh_failed,
            "summary": f"[GitHub] {gh_failed} 这次没抓到，接口限速或网络超时，可以过会儿再发一次"
        }

    return None
