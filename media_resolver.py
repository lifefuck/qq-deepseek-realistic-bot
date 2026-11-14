"""
短链展开与多媒体内容语义解析模块

支持：B站（BV/av/番剧 ep·ss/短链 b23.tv）、GitHub（仓库/issue/PR/gist）、
抖音、小红书、快手、网易云音乐、QQ音乐、通用网页。

群友可以直接往群里贴链接，所以这些地址都是用户可控的。
所有请求先过公网校验：内网、回环、链路本地一律拒绝；重定向一跳一跳重新校验，
否则公网域名 302 到 10.x 就能绕过第一道检查。

设计铁律（都是踩坑换来的）：
1. 只发「有信息量」的卡片。抖音/小红书落地页是纯 JS 墙，静态抓到的往往只是站名，
   以前这种空卡片会直接刷屏（「[抖音作品] 文案: 」），所以抓不到就返回 None 静默。
2. 所有截断走 _clip_desc，按句末标点收口，绝不切半个词。
3. 响应体一律走 _read_body 分块读满，`resp.content.read(n)` 会半截返回。
"""
import aiohttp
import asyncio
import html as html_lib
import ipaddress
import json
import logging
import os
import re
import socket
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

logger = logging.getLogger("DeepSeekBot")

# 匿名 API 每 IP 只有 60 次/小时，群里贴几个链接就到顶；有 token 是 5000 次
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"

MAX_REDIRECTS = 3

# 简介里常混零宽字符，直接切片会把词切成半个（如「收录于他的专辑《Whe」），观感像抓取失败
_ZERO_WIDTH_RE = re.compile("[\u200b-\u200f\u2060\ufeff]")
_DESC_LIMIT = 120

# 这些标题是站点外壳/反爬占位，不是内容，命中就当没抓到
_PLACEHOLDER_TITLES = (
    "小红书 - 你的生活指南",
    "小红书",
    "Sina Visitor System",
    "抖音-记录美好生活",
    "抖音",
    "快手",
    "哔哩哔哩 (゜-゜)つロ 干杯~-bilibili",
    "知乎 - 有问题，就会有答案",
    "微博",
    "登录",
    "页面不存在",
    "404",
    "验证码中间页",
    "Build software better",
    "Just a moment",
)

# 站点 slogan 冒充简介：小红书「在小红书，分享照片和视频…」、抖音「记录美好生活」，
# 抓到的落地页是 JS 墙时只有这段营销话，当正文发出去等于发废话
_NOISE_DESC_KEYS = (
    "记录美好生活", "分享照片和视频", "最会生活的人做朋友", "全球领先的中文社区",
    "快手，拥抱每一种生活", "拥抱每一种生活",
)

# 正文段落里出现这些就别当摘要（导航、版权、脚本残留）
_BODY_NOISE_KEYS = (
    "ICP", "版权所有", "Copyright", "cookie", "Cookie", "javascript",
    "NaN-NaN", "打开 App", "打开App", "关注", "登录后", "扫码", "广告拦截器",
    "广告屏蔽", "加入白名单", "无需申请",
)


def _clip_desc(text: str, limit: int = _DESC_LIMIT) -> str:
    """截简介：优先在句末标点处收口，找不到再退到逗号，宁短一句不切半个词"""
    cleaned = _ZERO_WIDTH_RE.sub("", text or "")
    cleaned = re.sub(r"[\r\n\t]+", " ", cleaned).strip()
    if not cleaned or len(cleaned) <= limit:
        return cleaned
    head = cleaned[:limit]
    for puncts in ("。！？…", "，、,.;；"):
        idx = max(head.rfind(p) for p in puncts)
        if idx >= limit // 2:
            return cleaned[: idx + 1]
    return head + "…"


def _clean_text(text: str, limit: int = 0) -> str:
    """HTML 实体反转义 + 去标签 + 压空白，网页标题里 `&quot;` `&#39;` 全靠这一步"""
    if not text:
        return ""
    out = html_lib.unescape(str(text))
    out = re.sub(r"<[^>]+>", "", out)
    out = _ZERO_WIDTH_RE.sub("", out)
    out = re.sub(r"[\r\n\t\s]+", " ", out).strip()
    return _clip_desc(out, limit) if limit else out


def _strip_markdown(text: str) -> str:
    """去掉链接/加粗/标题井号，Release 说明里全是这种，裁一半会露出半条 URL"""
    out = re.sub(r"\[([^\]]*)\]\(([^)]*)\)", r"\1", text or "")
    out = re.sub(r"^\s*#{1,6}\s*", "", out, flags=re.MULTILINE)
    out = out.replace("**", "").replace("`", "")
    return _clean_text(out)


def _fmt_num(n: Any) -> str:
    """459244 → 45.9万，3118343 → 311.8万，过亿用亿"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "0"
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


def _fmt_duration(seconds: Any) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _is_placeholder_title(title: str) -> bool:
    t = (title or "").strip()
    if len(t) < 2:
        return True
    return any(p in t for p in _PLACEHOLDER_TITLES) and len(t) <= 24


def _is_weak_title(title: str) -> bool:
    """「 - 哔哩哔哩」「微博」这种只有站点名的标题，等于没有标题"""
    if _is_placeholder_title(title):
        return True
    core = re.split(r"[|｜\-–—_:：]", title)[0].strip()
    return len(core) < 2


def _is_noise_desc(desc: str) -> bool:
    return any(k in (desc or "") for k in _NOISE_DESC_KEYS)


def _extract_meta(page: str) -> Dict[str, str]:
    """按 og → twitter → 普通 meta → <title> 的顺序取标题和描述

    很多站点的 <title> 是「站点名」或反爬占位，og:title 才是正文标题。
    """
    def meta(keys) -> str:
        for key in keys:
            m = re.search(
                rf'<meta[^>]+(?:property|name)=["\']{re.escape(key)}["\'][^>]*content=["\']([^"\']*)["\']',
                page, re.IGNORECASE,
            )
            if not m:
                # content 写在 property 前面的写法
                m = re.search(
                    rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{re.escape(key)}["\']',
                    page, re.IGNORECASE,
                )
            if m and m.group(1).strip():
                return _clean_text(m.group(1))
        return ""

    title = meta(("og:title", "twitter:title"))
    if not title:
        m_t = re.search(r"<title[^>]*>([^<]*)</title>", page, re.IGNORECASE)
        title = _clean_text(m_t.group(1)) if m_t else ""
    desc = meta(("og:description", "twitter:description", "description"))
    return {"title": title, "desc": desc, "site": meta(("og:site_name",))}


def _first_body_paragraph(page: str, min_len: int = 20) -> str:
    """抓首个像样的正文段落，过滤导航/版权/脚本残留"""
    for p_raw in re.findall(r"<p[^>]*>([\s\S]*?)</p>", page, re.IGNORECASE):
        clean = _clean_text(p_raw)
        if len(clean) >= min_len and not any(k in clean for k in _BODY_NOISE_KEYS):
            return _clip_desc(clean, 150)
    return ""


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


def _declared_charset(resp: Any, raw: bytes) -> str:
    """字符集判定：响应头 → meta charset → 猜 utf-8/gb18030

    老中文站（政府/新闻/论坛）大量还是 GBK，一律按 utf-8 解会得到一屏乱码标题。
    """
    ctype = (resp.headers.get("Content-Type") or "").lower()
    m = re.search(r"charset=([\w-]+)", ctype)
    if m:
        return m.group(1)
    head = raw[:2048].decode("ascii", errors="ignore").lower()
    m = re.search(r'charset=["\']?([\w-]+)', head)
    if m:
        return m.group(1)
    return "utf-8"


def _decode_body(resp: Any, raw: bytes) -> str:
    charset = _declared_charset(resp, raw)
    for candidate in (charset, "utf-8", "gb18030"):
        try:
            return raw.decode(candidate, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="ignore")


async def safe_fetch(
    session: aiohttp.ClientSession,
    url: str,
    *,
    timeout: float = 6.0,
    max_hops: int = MAX_REDIRECTS,
    extra_headers: Optional[Dict[str, str]] = None,
    want_json: bool = False,
) -> Optional[Tuple[str, str]]:
    """抓公网页面，返回 (最终地址, 正文)。不安全或抓不到都返回 None。

    want_json=True 时接受非 200 但正文是合法 JSON 的响应（B站接口各种 code 错误体），
    由调用方自己判断业务码；这样日志里能看清到底是限速还是参数错。
    """
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
                if resp.status != 200 and not want_json:
                    logger.debug("抓取返回 %s: %s", resp.status, current)
                    return None
                # 上限 2MB：群友贴个大文件链接就能把内存和事件循环一起拖死
                raw = await _read_body(resp)
                return str(resp.url), _decode_body(resp, raw)
        except Exception as e:
            logger.debug("抓取失败 %s: %s", current, e)
            return None
    return None


async def _fetch_json(
    session: aiohttp.ClientSession, url: str, *, timeout: float = 5.0,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Optional[Any]:
    fetched = await safe_fetch(
        session, url, timeout=timeout, extra_headers=extra_headers, want_json=True
    )
    if not fetched:
        return None
    try:
        return json.loads(fetched[1])
    except (json.JSONDecodeError, AttributeError):
        logger.warning("接口返回 JSON 不完整 len=%d tail=%r", len(fetched[1]), fetched[1][-80:])
        return None


async def _fetch_page(
    session: aiohttp.ClientSession, url: str, *, timeout: float = 5.0
) -> Optional[Tuple[str, str]]:
    return await safe_fetch(session, url, timeout=timeout)


# ---------------------------------------------------------------- GitHub

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
        data = await _fetch_json(session, url, extra_headers=headers)
        if data is None:
            logger.warning("GitHub 仓库抓取失败(第%d次) %s/%s", attempt + 1, owner, repo)
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


def _github_headers() -> Dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _repo_card(repo_data: Dict[str, Any]) -> Dict[str, Any]:
    full_name = repo_data.get("full_name") or "未知仓库"
    desc = _clean_text(repo_data.get("description") or "") or "暂无项目描述"
    lang = repo_data.get("language") or "未知"
    stars = _fmt_num(repo_data.get("stargazers_count", 0))
    forks = _fmt_num(repo_data.get("forks_count", 0))
    topics = [t for t in (repo_data.get("topics") or []) if t][:4]
    tail = f"\n标签: {' '.join('#' + t for t in topics)}" if topics else ""
    return {
        "type": "github",
        "title": full_name,
        "author": (repo_data.get("owner") or {}).get("login", ""),
        "summary": (
            f"[GitHub开源项目] {full_name} ({lang} | ★{stars} | ⑂{forks})\n"
            f"简介: {_clip_desc(desc, 100)}{tail}"
        ),
    }


async def _parse_github(
    session: aiohttp.ClientSession, owner: str, repo: str, rest: str
) -> Optional[Dict[str, Any]]:
    """仓库 / issue / PR / gist 分开做卡片，只贴 issue 链接时别只讲仓库简介"""
    repo = repo.removesuffix(".git")

    # issue / PR：编号、标题、状态、评论数才是群友想知道的
    m_num = re.match(r"(issues|pull)/(\d+)", rest)
    if m_num:
        kind, number = m_num.group(1), m_num.group(2)
        endpoint = "issues" if kind == "issues" else "pulls"
        data = await _fetch_json(
            session, f"https://api.github.com/repos/{owner}/{repo}/{endpoint}/{number}",
            extra_headers=_github_headers(),
        )
        if isinstance(data, dict) and data.get("title"):
            title = _clean_text(data["title"])
            state = data.get("state", "")
            if kind == "pull" and data.get("merged_at"):
                state = "已合并"
            elif state == "open":
                state = "open"
            body = _clip_desc(_clean_text(data.get("body") or ""), 100)
            comments = data.get("comments", 0)
            label = "PR" if kind == "pull" else "Issue"
            line = f"[GitHub {label}] {owner}/{repo} #{number} {title}（{state}"
            if comments:
                line += f" | {comments} 评论"
            line += "）"
            if body:
                line += f"\n摘要: {body}"
            return {"type": "github", "title": title, "author": owner, "summary": line}

    repo_data = await _fetch_github_repo(session, owner, repo)
    if repo_data:
        card = _repo_card(repo_data)
        # 贴的是文件/目录/Release 时补一句路径，别让人以为在说整个仓库
        if rest.startswith(("blob/", "tree/")):
            kind = "文件" if rest.startswith("blob/") else "目录"
            card["summary"] = (
                f"[GitHub {kind}] {owner}/{repo} · {_clip_desc(rest, 70)}\n"
                f"仓库: {_clip_desc(_clean_text(repo_data.get('description') or ''), 80) or '无简介'}"
                f"（{repo_data.get('language') or '未知'} | ★{_fmt_num(repo_data.get('stargazers_count', 0))}）"
            )
        elif rest.startswith("releases"):
            latest = await _fetch_json(
                session, f"https://api.github.com/repos/{owner}/{repo}/releases/latest",
                extra_headers=_github_headers(),
            )
            if isinstance(latest, dict) and latest.get("tag_name"):
                tag = latest["tag_name"]
                name = _clean_text(latest.get("name") or "")
                if name and tag and tag not in name:
                    name = f"{name}（{tag}）"
                body = _strip_markdown(latest.get("body") or "")
                card["summary"] = (
                    f"[GitHub Release] {owner}/{repo} {name or tag}\n"
                    f"简介: {_clip_desc(body, 100) or '无说明'}"
                )
        return card
    return None


# ---------------------------------------------------------------- B 站

_BILI_API_HEADERS = {"Referer": "https://www.bilibili.com/"}


def _bili_video_card(vdata: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    title = _clean_text(vdata.get("title") or "")
    if not title:
        return None
    up = (vdata.get("owner") or {}).get("name", "")
    stat = vdata.get("stat") or {}
    meta_bits = []
    dur = _fmt_duration(vdata.get("duration"))
    if dur:
        meta_bits.append(dur)
    if stat.get("view"):
        meta_bits.append(f"{_fmt_num(stat['view'])}播放")
    if stat.get("danmaku"):
        meta_bits.append(f"{_fmt_num(stat['danmaku'])}弹幕")
    if stat.get("like"):
        meta_bits.append(f"{_fmt_num(stat['like'])}赞")
    head = f"[B站视频] 《{title}》- UP主: {up or '未知'}"
    if meta_bits:
        head += " | " + " · ".join(meta_bits)
    desc = _clip_desc(_clean_text(vdata.get("desc") or ""))
    return {
        "type": "bilibili",
        "title": title,
        "author": up,
        "summary": f"{head}\n简介: {desc or '无简介'}",
    }


async def _parse_bilibili_bangumi(session: aiohttp.ClientSession, ep_or_ss: str) -> Optional[Dict[str, Any]]:
    """番剧/影视：ep 号也能查 season 接口，返回的是整季信息 + 各话标题"""
    key = "ep_id" if ep_or_ss.lower().startswith("ep") else "season_id"
    ident = re.sub(r"\D", "", ep_or_ss)
    if not ident:
        return None
    data = await _fetch_json(
        session, f"https://api.bilibili.com/pgc/view/web/season?{key}={ident}",
        extra_headers=_BILI_API_HEADERS,
    )
    result = (data or {}).get("result") or {}
    title = _clean_text(result.get("title") or "")
    if not title:
        return None
    bits = []
    index_show = (result.get("new_ep") or {}).get("index_show")
    if index_show:
        bits.append(_clean_text(index_show))
    styles = [s for s in (result.get("styles") or []) if s][:3]
    if styles:
        bits.append("/".join(styles))
    if result.get("rating", {}).get("score"):
        bits.append(f"{result['rating']['score']}分")
    head = f"[B站番剧] 《{title}》"
    if bits:
        head += " | " + " · ".join(bits)
    desc = _clip_desc(_clean_text(result.get("evaluate") or ""))
    return {
        "type": "bilibili",
        "title": title,
        "author": "",
        "summary": f"{head}\n简介: {desc or '无简介'}",
    }


async def _parse_bilibili_live(session: aiohttp.ClientSession, room_id: str) -> Optional[Dict[str, Any]]:
    """直播间卡片：标题、主播、开播状态、人气。直播页是纯前端渲染，只能走接口"""
    info = await _fetch_json(
        session, f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room_id}",
        extra_headers=_BILI_API_HEADERS,
    )
    room = (info or {}).get("data") or {}
    title = _clean_text(room.get("title") or "")
    if not title:
        return None
    anchor = await _fetch_json(
        session,
        f"https://api.live.bilibili.com/live_user/v1/UserInfo/get_anchor_in_room?roomid={room_id}",
        extra_headers=_BILI_API_HEADERS,
    )
    uname = ((anchor or {}).get("data") or {}).get("info", {}).get("uname", "")
    bits = ["直播中" if room.get("live_status") == 1 else "未开播"]
    if room.get("online"):
        bits.append(f"人气 {_fmt_num(room['online'])}")
    if room.get("area_name"):
        bits.append(room["area_name"])
    return {
        "type": "bilibili",
        "title": title,
        "author": uname,
        "summary": f"[B站直播] 《{title}》- {uname or '未知主播'} | {' · '.join(bits)}",
    }


async def _parse_bilibili(session: aiohttp.ClientSession, text: str) -> Optional[Dict[str, Any]]:
    """BV / av / 番剧 ep·ss / b23 短链，四种入口都收敛到这里"""
    normalized = text.replace(r"\/", "/")
    target = ""

    m_short = re.search(r"https?://(?:b23\.tv|bili2233\.cn)/([a-zA-Z0-9]+)", normalized)
    if m_short:
        fetched = await safe_fetch(session, f"https://b23.tv/{m_short.group(1)}")
        if fetched:
            target = fetched[0]
            # 短链落到番剧页时页面本身没有 BV 号，只能看 URL
            m_ep = re.search(r"/(ep|ss)(\d+)", target)
            if m_ep:
                return await _parse_bilibili_bangumi(session, m_ep.group(0).lstrip("/"))

    haystack = f"{target} {normalized}"

    m_live = re.search(r"live\.bilibili\.com/(\d+)", haystack)
    if m_live:
        card = await _parse_bilibili_live(session, m_live.group(1))
        if card:
            return card

    m_ep = re.search(r"bilibili\.com/bangumi/(?:play|media)/(ep|ss)(\d+)", haystack, re.IGNORECASE)
    if m_ep:
        card = await _parse_bilibili_bangumi(session, f"{m_ep.group(1)}{m_ep.group(2)}")
        if card:
            return card

    m_bv = re.search(r"\b(BV[a-zA-Z0-9]{10})\b", haystack, re.IGNORECASE)
    m_av = re.search(r"\bav(\d+)\b", haystack, re.IGNORECASE)
    if not m_bv and not m_av:
        return None
    query = f"bvid={m_bv.group(1)}" if m_bv else f"aid={m_av.group(1)}"
    data = await _fetch_json(
        session, f"https://api.bilibili.com/x/web-interface/view?{query}",
        extra_headers=_BILI_API_HEADERS,
    )
    vdata = (data or {}).get("data") or {}
    # 合集/番剧的 view 接口返回 pages，正片信息还是同一份
    return _bili_video_card(vdata)


# ---------------------------------------------------------------- 音乐

async def _parse_netease_music(session: aiohttp.ClientSession, url: str) -> Optional[Dict[str, Any]]:
    parsed = urlparse(url)
    fragment = parsed.fragment or ""
    qs = parse_qs(parsed.query or fragment.split("?", 1)[-1])
    song_id = (qs.get("id") or [""])[0]

    m_pl = re.search(r"(?:playlist|discover/toplist)", url)
    if m_pl and song_id:
        data = await _fetch_json(
            session, f"https://music.163.com/api/v6/playlist/detail?id={song_id}"
        )
        pl = (data or {}).get("playlist") or {}
        if pl.get("name"):
            return {
                "type": "music",
                "title": pl["name"],
                "summary": (
                    f"[网易云歌单] 《{_clean_text(pl['name'])}》"
                    f"（{pl.get('trackCount', 0)} 首 | 播放 {_fmt_num(pl.get('playCount', 0))}）\n"
                    f"简介: {_clip_desc(_clean_text(pl.get('description') or '')) or '无简介'}"
                ),
            }
        return None

    if not song_id.isdigit():
        return None
    data = await _fetch_json(
        session, f"https://music.163.com/api/song/detail?ids=%5B{song_id}%5D"
    )
    songs = (data or {}).get("songs") or []
    if not songs:
        return None
    song = songs[0]
    artists = "/".join(a.get("name", "") for a in song.get("artists") or []) or "未知"
    album = (song.get("album") or {}).get("name") or ""
    dur = _fmt_duration(int((song.get("duration") or 0) / 1000))
    head = f"[网易云音乐] 《{_clean_text(song.get('name') or '')}》- {artists}"
    if album:
        head += f" | 专辑《{_clean_text(album)}》"
    if dur:
        head += f" | {dur}"
    return {"type": "music", "title": song.get("name", ""), "author": artists, "summary": head}


async def _parse_qq_music(session: aiohttp.ClientSession, url: str) -> Optional[Dict[str, Any]]:
    m_mid = re.search(r"songmid=([A-Za-z0-9]+)", url) or re.search(r"/songDetail/([A-Za-z0-9]+)", url)
    if not m_mid:
        return None
    mid = m_mid.group(1)
    data = await _fetch_json(
        session,
        f"https://c.y.qq.com/v8/fcg-bin/fcg_play_single_song.fcg?songmid={mid}&format=json",
        extra_headers={"Referer": "https://y.qq.com/"},
    )
    items = (data or {}).get("data") or []
    if not items:
        return None
    song = items[0]
    singers = "/".join(s.get("name", "") for s in song.get("singer") or []) or "未知"
    album = (song.get("album") or {}).get("name") or ""
    dur = _fmt_duration(song.get("interval") or 0)
    head = f"[QQ音乐] 《{_clean_text(song.get('name') or '')}》- {singers}"
    if album:
        head += f" | 专辑《{_clean_text(album)}》"
    if dur:
        head += f" | {dur}"
    return {"type": "music", "title": song.get("name", ""), "author": singers, "summary": head}


# ---------------------------------------------------------------- 短视频 / 图文

def _salvage_share_text(text: str, kind: str) -> Tuple[str, str]:
    """从群友贴的分享口令里捞标题/作者。

    QQ 里分享抖音、小红书基本都是「复制口令 + 短链」的纯文本，
    平台那套 JS 墙我们进不去，但这几段文字本身就把标题写着了，白捡的信息别浪费。
    返回 (标题, 作者)，捞不到就是两个空串。
    """
    title = author = ""
    if kind == "douyin":
        m_auth = re.search(r"【(.+?)的作品】", text)
        if m_auth:
            author = m_auth.group(1).strip()
        # 作者名后面到链接之间那一段通常就是作品文案
        m_title = re.search(r"】([^【】]{2,60}?)\s*https?://", text)
        if m_title:
            title = m_title.group(1).strip()
    elif kind == "xiaohongshu":
        m_title = re.search(r"\d+\s*([^\s].{1,60}?)\s*发布了一篇小红书笔记", text)
        if m_title:
            title = m_title.group(1).strip()
        m_auth = re.match(r"\s*(\S{1,20}?)\s*发布了一篇小红书笔记", text)
        if m_auth:
            author = m_auth.group(1).strip()
    elif kind == "kuaishou":
        m_title = re.search(r"【(.+?)】", text)
        if m_title:
            title = m_title.group(1).strip()
    return _clean_text(title)[:60], _clean_text(author)[:20]


async def _parse_short_video(
    session: aiohttp.ClientSession, url: str, kind: str, share_text: str = ""
) -> Optional[Dict[str, Any]]:
    """抖音/小红书/快手的落地页都是 JS 墙。

    静态抓取能拿到的通常只有站点名，以前会发出「[抖音作品] 文案: 」这种空卡片。
    这里只有真的抓到标题或描述才出声，抓不到就返回 None 静默，别刷屏。
    """
    fetched = await _fetch_page(session, url, timeout=6.0)
    if not fetched:
        logger.info("%s 落地页抓取失败（多半是 JS 墙或需登录）: %s", kind, url[:80])
        return _share_text_card(kind, share_text)
    final_url, page = fetched
    meta = _extract_meta(page)
    title = meta["title"]
    desc = meta["desc"]
    if _is_weak_title(title):
        title = ""
    if _is_noise_desc(desc):
        desc = ""
    if not title and not desc:
        logger.info("%s 页面没有可读元信息，改用分享口令兜底: %s", kind, final_url[:80])
        return _share_text_card(kind, share_text)
    label = {"douyin": "抖音作品", "xiaohongshu": "小红书笔记", "kuaishou": "快手作品"}[kind]
    body = _clip_desc(desc) if desc else ""
    text = f"[{label}] {title or '（无标题）'}"
    if body and body != title:
        text += f"\n文案: {body}"
    return {"type": kind, "title": title, "summary": text}


def _share_text_card(kind: str, share_text: str) -> Optional[Dict[str, Any]]:
    """只用分享口令里的标题/作者拼卡片；一个字都没有就静默（宁可不发也不发空卡片）"""
    title, author = _salvage_share_text(share_text, kind)
    if not title and not author:
        return None
    label = {"douyin": "抖音作品", "xiaohongshu": "小红书笔记", "kuaishou": "快手作品"}[kind]
    head = f"[{label}] {title or '（分享口令里没有标题）'}"
    if author:
        head += f" - {author}"
    head += "\n（正文在 App 里，网页端抓不到）"
    return {"type": kind, "title": title, "author": author, "summary": head}


# ---------------------------------------------------------------- 通用网页

def _find_first_url(text: str) -> str:
    m = re.search(r"https?://[^\s<>\"'，。！？（）\[\]]+", text)
    if not m:
        return ""
    url = html_lib.unescape(m.group(0)).rstrip(".,;:!?，。！？")
    # 结尾常黏上右括号（Markdown 写法），配对不上就削掉
    while url.endswith((")", "）", "]")) and url.count("(") < url.count(")"):
        url = url[:-1]
    return url


async def _parse_webpage(session: aiohttp.ClientSession, url: str) -> Optional[Dict[str, Any]]:
    fetched = await _fetch_page(session, url, timeout=5.0)
    if not fetched:
        return None
    final_url, page = fetched
    host = (urlparse(final_url).hostname or "").lower()
    if host in ("github.com", "www.github.com"):
        # 仓库/issue/gist 已经在上游处理过，走到这里说明是 topics、trending 这类聚合页，
        # 它们的 og:title 全是「Build software better, together」，发出去纯噪音
        logger.info("GitHub 聚合页不做省流卡片: %s", final_url[:80])
        return None
    meta = _extract_meta(page)
    title, desc = meta["title"], meta["desc"]
    if _is_noise_desc(desc):
        desc = ""
    body = "" if desc else _first_body_paragraph(page)

    if _is_weak_title(title):
        logger.info("网页只是站点外壳/反爬页，跳过卡片: %s", final_url[:80])
        return None
    if not title and not desc and not body:
        return None

    parts = []
    if title:
        parts.append(f"标题: {_clip_desc(title, 100)}")
    if desc:
        parts.append(f"简介: {_clip_desc(desc, 120)}")
    elif body:
        parts.append(f"摘要: {body}")
    return {"type": "webpage", "title": title, "summary": f"[分享网页省流] {' | '.join(parts)}"}


# ---------------------------------------------------------------- 入口

async def parse_media_content(session: aiohttp.ClientSession, text: str) -> Optional[Dict[str, Any]]:
    """检测并解析文本中的媒体/仓库/网页链接，返回带 summary 的卡片数据"""
    normalized = text.replace(r"\/", "/")

    # 1. B 站：BV/av/番剧/短链
    if re.search(r"b23\.tv|bili2233\.cn|bilibili\.com|\bBV[a-zA-Z0-9]{10}\b|\bav\d+", normalized, re.IGNORECASE):
        card = await _parse_bilibili(session, normalized)
        if card:
            return card

    # 2. GitHub：仓库/issue/PR/gist
    m_gist = re.search(r"https?://gist\.github\.com/([a-zA-Z0-9_.-]+)/([a-f0-9]+)", text)
    if m_gist:
        data = await _fetch_json(
            session, f"https://api.github.com/gists/{m_gist.group(2)}",
            extra_headers=_github_headers(),
        )
        if isinstance(data, dict) and data.get("files"):
            files = list(data["files"].keys())[:3]
            return {
                "type": "github",
                "title": _clean_text(data.get("description") or "Gist"),
                "summary": (
                    f"[GitHub Gist] {_clean_text(data.get('description') or '无描述')}"
                    f"（{len(data['files'])} 个文件: {', '.join(files)}）"
                ),
            }

    m_gh = re.search(r"https?://github\.com/([a-zA-Z0-9_.-]+)(?:/([a-zA-Z0-9_.-]+))?(/.*)?", text)
    if m_gh:
        owner, repo, rest = m_gh.group(1), m_gh.group(2) or "", (m_gh.group(3) or "").lstrip("/")
        if repo and owner.lower() not in (
            "topics", "features", "collections", "events", "sponsors", "settings",
            "explore", "orgs", "users", "apps", "marketplace", "trending", "issues", "pulls",
        ):
            card = await _parse_github(session, owner, repo, rest)
            if card:
                return card
            logger.info("GitHub 元数据没拿到，降级走网页解析: %s/%s", owner, repo)

    # 3. 音乐
    if "music.163.com" in text:
        card = await _parse_netease_music(session, _find_first_url(text))
        if card:
            return card
    if "y.qq.com" in text or "i.y.qq.com" in text:
        card = await _parse_qq_music(session, _find_first_url(text))
        if card:
            return card

    # 4. 短视频/图文：抖音、小红书、快手（抓不到内容就静默）
    m_dy = re.search(r"https?://(?:v\.douyin\.com/[A-Za-z0-9_-]+|(?:www\.)?(?:ies)?douyin\.com/(?:video|note|share/video)/\d+)", text)
    if m_dy:
        card = await _parse_short_video(session, m_dy.group(0).rstrip("/") + "/", "douyin", text)
        if card:
            return card
    m_xhs = re.search(
        r"https?://(?:xhslink\.com/[A-Za-z0-9/_?=&.-]+|(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/[A-Za-z0-9]+[^\s'\"]*)",
        text,
    )
    if m_xhs:
        card = await _parse_short_video(session, m_xhs.group(0), "xiaohongshu", text)
        if card:
            return card
    m_ks = re.search(r"https?://(?:v\.kuaishou\.com/[A-Za-z0-9_-]+|(?:www\.)?kuaishou\.com/short-video/[A-Za-z0-9]+)", text)
    if m_ks:
        card = await _parse_short_video(session, m_ks.group(0), "kuaishou", text)
        if card:
            return card

    # 5. 通用网页兜底
    url = _find_first_url(text)
    if url:
        return await _parse_webpage(session, url)

    return None
