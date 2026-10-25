"""
联网扩展能力：Trace.moe 动画截图搜番 + 免 Key 轻量网页检索 + B 站热门新番推荐
"""
import aiohttp
import logging
import random
import re
import urllib.parse
from typing import Any, Dict, List, Optional

logger = logging.getLogger("DeepSeekBot")

# ==================== Trace.moe 搜番 ====================
async def search_anime_by_image(session: aiohttp.ClientSession, image_url: str) -> Optional[Dict[str, Any]]:
    """使用 Trace.moe 官方免费 API 检索动画截图出处与集数"""
    api_url = f"https://api.trace.moe/search?cutBorders&url={urllib.parse.quote(image_url)}"
    try:
        async with session.get(api_url, timeout=12.0) as resp:
            if resp.status != 200:
                logger.warning("Trace.moe 请求失败: HTTP %d", resp.status)
                return None
            data = await resp.json()
            results = data.get("result", [])
            if not results:
                return None
            
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
        return None

# ==================== 免 Key 网页轻量检索 ====================
async def search_web_brief(session: aiohttp.ClientSession, query: str, limit: int = 3) -> List[Dict[str, str]]:
    """通过 DuckDuckGo HTML 引擎免 Key 抓取前 3 条网页摘要作为大模型补充上下文"""
    search_url = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://html.duckduckgo.com/"
    }
    data = {"q": query, "b": ""}
    results = []
    try:
        async with session.post(search_url, data=data, headers=headers, timeout=8.0) as resp:
            if resp.status == 200:
                html = await resp.text()
                # 正则提取标题与摘要
                snippets = re.findall(r'<a class="result__snippet[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>', html)
                titles = re.findall(r'<a class="result__url[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>', html)
                
                # 备用抽取
                items = re.findall(r'<div class="result__body">[\s\S]*?<a class="result__snippet[^>]*>([\s\S]*?)</a>', html)
                title_matches = re.findall(r'<a class="result__a"[^>]*>([\s\S]*?)</a>', html)

                for i in range(min(limit, len(title_matches))):
                    t = re.sub(r'<[^>]+>', '', title_matches[i]).strip()
                    s = re.sub(r'<[^>]+>', '', items[i]).strip() if i < len(items) else ""
                    if t and s:
                        results.append({"title": t, "snippet": s})
    except Exception as e:
        logger.debug("网页检索异常: %s", e)
    return results

# ==================== B 站热门/新番动态免登录抓取 ====================
async def fetch_bilibili_hot_videos(session: aiohttp.ClientSession, keyword: str = "", limit: int = 5) -> List[Dict[str, Any]]:
    """抓取 B 站免登录官方热门排行榜"""
    url = f"https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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
