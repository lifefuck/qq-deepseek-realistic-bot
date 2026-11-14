"""媒体解析体检脚本：把代表性命中各种解析分支的输入丢进去，看真实输出。

用法： .venv/bin/python tests/probe_media.py
不走 pytest，就是给人看的体检报告——每条打印命中类型与最终下发文案。
"""
import asyncio
import logging
import sys

import aiohttp

import media_resolver

logging.basicConfig(level=logging.WARNING)

CASES = [
    ("纯BV号", "BV1UT42167xb"),
    ("B站长链", "https://www.bilibili.com/video/BV1UT42167xb?p=1&vd_source=abc"),
    ("B站番剧ep", "https://www.bilibili.com/bangumi/play/ep323085"),
    ("B站番剧ss", "https://www.bilibili.com/bangumi/play/ss33415"),
    ("B站直播", "https://live.bilibili.com/6"),
    ("微信公众号", "https://mp.weixin.qq.com/s/9L7FBQg60PmiotLEV91BPg"),
    ("B站专栏cv", "https://www.bilibili.com/read/cv28374066"),
    ("B站无效ep", "https://www.bilibili.com/bangumi/play/ep477142"),
    ("GitHub仓库", "https://github.com/morluto/rea"),
    ("GitHub issue", "https://github.com/Eyre-S/Coeur-Morny-Cono/issues/50"),
    ("GitHub topics", "https://github.com/topics/python"),
    ("XHS短链1", "http://xhslink.com/h04FrT"),
    ("XHS短链a型", "http://xhslink.com/a/Qb6N47BGvhBY"),
    ("XHS长链", "https://www.xiaohongshu.com/explore/65f0f0f0000000000d00abcd"),
    ("抖音短链", "https://v.douyin.com/iRNBho6u/"),
    ("抖音口令", "7.42 复制打开抖音，看看【摄影师阿凯的作品】川西秋天的第一场雪 https://v.douyin.com/iRNBho6u/ 复制此链接，打开Dou音搜索"),
    ("小红书口令", "59 一个人的川西 发布了一篇小红书笔记，快来看吧！ 😆 ABC123 http://xhslink.com/h04FrT，复制本条信息，打开【小红书】App查看精彩内容！"),
    ("抖音长链", "https://www.douyin.com/video/7358947904833322249"),
    ("网页-少数派", "https://sspai.com/post/73145"),
    ("网页-阮一峰", "https://www.ruanyifeng.com/blog/2024/01/weekly-issue-286.html"),
    ("网页-GBK老站", "http://www.people.com.cn/"),
    ("网页-带实体URL", "https://www.bing.com/search?q=python&amp;form=QBLH"),
    ("网页-知乎", "https://zhuanlan.zhihu.com/p/670000000"),
    ("网页-微博", "https://weibo.com/1234567890/Nabcdefg"),
    ("网易云音乐", "https://music.163.com/#/song?id=1824045033"),
    ("网易云歌单", "https://music.163.com/playlist?id=2884035"),
    ("QQ音乐", "https://y.qq.com/n/ryqq/songDetail/0039MnYb0qxYhV"),
    ("GitHub gist", "https://gist.github.com/octocat/6cad326836d38bd3a7ae"),
    ("GitHub release", "https://github.com/morluto/rea/releases"),
    ("GitHub blob", "https://github.com/morluto/rea/blob/main/README.md"),
    ("快手", "https://v.kuaishou.com/2r2ZtZ"),
    ("YouTube", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
]


async def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    async with aiohttp.ClientSession() as session:
        for name, payload in CASES:
            if only and only not in name and only not in payload:
                continue
            try:
                info = await asyncio.wait_for(
                    media_resolver.parse_media_content(session, payload), timeout=20
                )
            except Exception as e:  # 解析流程本身抛异常也算体检验出来的病灶
                print(f"[{name}] EXCEPTION {type(e).__name__}: {e}")
                continue
            if not info:
                print(f"[{name}] -> None（不下发卡片）")
                continue
            print(f"[{name}] -> type={info.get('type')} | {info.get('summary')!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
