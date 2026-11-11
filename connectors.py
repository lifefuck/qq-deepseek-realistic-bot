"""
联网扩展能力：Trace.moe 动画截图搜番 + 免 Key 轻量网页检索 + B 站热门新番推荐
"""
import aiohttp
import logging

from media_resolver import _is_public_url, _read_body
import random
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("DeepSeekBot")

# ==================== Trace.moe 搜番 ====================
# trace.moe 的 url= 参数是让它自己去抓图，QQ/微信的图片 CDN 基本不让它抓，
# 群友发的截图十有八九返回 400/404。所以这里先自己把图扒下来，再以 multipart 直传。
TRACE_MOE_API = "https://api.trace.moe/search?cutBorders"
MAX_IMAGE_BYTES = 8 * 1024 * 1024


async def _download_image(session: aiohttp.ClientSession, image_url: str) -> Optional[bytes]:
    """把群友发的图下载成字节，超过 8MB 直接放弃，别把内存喂爆。"""
    try:
        # 这个 URL 来自群消息，不加校验就能被拿去打内网，还会把内网响应转手传给 trace.moe
        if not await _is_public_url(image_url):
            logger.warning("搜番拒绝非公网图片地址: %s", image_url[:80])
            return None
        async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=10.0)) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if resp.status != 200 or not ctype.startswith("image/"):
                logger.warning("搜番图片类型不支持: HTTP %d %s", resp.status, ctype or "?")
                return None
                logger.warning("搜番下载图片失败: HTTP %d", resp.status)
                return None
            declared = int(resp.headers.get("Content-Length") or 0)
            if declared > MAX_IMAGE_BYTES:
                logger.warning("搜番图片过大: %.1fMB", declared / 1024 / 1024)
                return None
            data = await _read_body(resp, MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                return None
            return data or None
    except Exception as e:
        logger.warning("搜番下载图片异常: %s", e)
        return None


async def search_anime_by_image(session: aiohttp.ClientSession, image_url: str) -> Optional[Dict[str, Any]]:
    """检索动画截图出处。失败时返回 {"error": ...}，调用方据此给群友一句交代。"""
    image_bytes = await _download_image(session, image_url)
    if not image_bytes:
        return {"error": "download"}

    form = aiohttp.FormData()
    form.add_field("image", image_bytes, filename="anime.jpg", content_type="image/jpeg")
    try:
        async with session.post(TRACE_MOE_API, data=form, timeout=aiohttp.ClientTimeout(total=25.0)) as resp:
            if resp.status == 402:
                logger.warning("Trace.moe 免费额度已用完")
                return {"error": "quota"}
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if resp.status != 200 or not ctype.startswith("image/"):
                logger.warning("搜番图片类型不支持: HTTP %d %s", resp.status, ctype or "?")
                return None
                logger.warning("Trace.moe 请求失败: HTTP %d", resp.status)
                return {"error": "http", "status": resp.status}
            data = await resp.json()
            quota, used = data.get("quota"), data.get("quotaUsed")
            if isinstance(quota, int) and isinstance(used, int) and used >= quota:
                logger.warning("Trace.moe 额度告警: %d/%d", used, quota)

            results = data.get("result", [])
            if not results:
                return {"error": "noresult"}

            # 取置信度最高的首个结果
            best = results[0]
            similarity = best.get("similarity", 0)
            if similarity < 0.80:  # 低于 80% 相似度视为可能不匹配
                return {"uncertain": True, "similarity": similarity, "title": "未找到高确信度动画", "data": best}

            anilist = best.get("anilist") or {}
            title_native = anilist.get("title", {}).get("native") or anilist.get("title", {}).get("romaji") or "未知动画"
            title_chinese = anilist.get("title", {}).get("chinese") or title_native
            episode = best.get("episode") or "?"
            from_sec = int(best.get("from", 0))
            m, s = divmod(from_sec, 60)
            time_str = f"{m:02d}:{s:02d}"

            return {
                "title": title_chinese,
                "native_title": title_native,
                "episode": episode,
                "time": time_str,
                "similarity": round(similarity * 100, 1),
                "is_adult": anilist.get("isAdult", False)
            }
    except Exception as e:
        logger.warning("Trace.moe 搜番异常: %s", e)
        return {"error": "exception"}

# ==================== 免 Key 网页轻量检索 ====================
async def search_web_brief(session: aiohttp.ClientSession, query: str, limit: int = 3) -> List[Dict[str, str]]:
    """免 Key 网页检索：cn.bing.com 优先，拿不到退回搜狗。

    原先用 DuckDuckGo HTML，在国内网络下直接超时，搜索是静默失败的——
    表现就是「问它点什么它都答不上来」，其实是网络层没通。
    """
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    results: List[Dict[str, str]] = []

    def _clean(raw: str) -> str:
        txt = re.sub(r"<[^>]+>", "", raw or "")
        txt = txt.replace("&ensp;", " ").replace("&#0183;", " ").replace("&nbsp;", " ")
        txt = re.sub(r"&[a-z]+;", " ", txt)
        return re.sub(r"\s+", " ", txt).strip()

    try:
        async with session.get("https://cn.bing.com/search", params={"q": query},
                               headers={"User-Agent": ua}, timeout=10.0) as resp:
            if resp.status == 200:
                html = await resp.text()
                for block in re.findall(r'<li class="b_algo".*?</li>', html, re.S):
                    tm = re.search(r"<h2[^>]*>.*?<a[^>]*>(.*?)</a>", block, re.S)
                    if not tm:
                        continue
                    sm = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
                    title = _clean(tm.group(1))
                    if title:
                        results.append({"title": title, "snippet": _clean(sm.group(1)) if sm else ""})
                    if len(results) >= limit:
                        break
    except Exception as e:
        logger.debug("bing 检索异常: %s", e)

    # 必应被限流时换搜狗，两家同时挂才认命
    if not results:
        try:
            async with session.get("https://www.sogou.com/web", params={"query": query},
                                   headers={"User-Agent": ua}, timeout=10.0) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    heads = re.findall(r"<h3[^>]*>\s*<a[^>]*>(.*?)</a>", html, re.S)
                    snips = re.findall(r'<div class="fz-mid space-txt">(.*?)</div>', html, re.S)
                    for i, h in enumerate(heads):
                        title = _clean(h)
                        if title:
                            results.append({"title": title,
                                            "snippet": _clean(snips[i]) if i < len(snips) else ""})
                        if len(results) >= limit:
                            break
        except Exception as e:
            logger.debug("搜狗检索异常: %s", e)

    return results

# ==================== B 站热门/新番动态免登录抓取 ====================
async def fetch_bilibili_hot_videos(session: aiohttp.ClientSession, keyword: str = "", limit: int = 5) -> List[Dict[str, Any]]:
    """抓取 B 站免登录官方热门排行榜"""
    url = f"https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/"
    }
    filtered = []
    # 负面过滤词，排除水视频
    negative_words = ["直播回放", "录播", "拼多多", "抽奖", "领券", "教程", "带货"]
    try:
        async with session.get(url, headers=headers, timeout=8.0) as resp:
            if resp.status == 200:
                data = await resp.json()
                video_list = data.get("data", {}).get("list", [])
                for v in video_list:
                    title = v.get("title", "")
                    desc = v.get("desc", "")
                    bvid = v.get("bvid", "")
                    tname = v.get("tname", "") # 分区名，如动画、游戏
                    owner = v.get("owner", {}).get("name", "")

                    if any(nw in title for nw in negative_words):
                        continue
                    if keyword and (keyword.lower() not in title.lower() and keyword.lower() not in tname.lower()):
                        continue

                    filtered.append({
                        "bvid": bvid,
                        "title": title,
                        "author": owner,
                        "tag": tname,
                        "desc": desc[:80]
                    })
                    if len(filtered) >= limit:
                        break
    except Exception as e:
        logger.debug("抓取 B 站热门异常: %s", e)
    return filtered
