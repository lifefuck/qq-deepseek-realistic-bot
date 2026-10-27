#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网络流行语与热梗词库同步脚本
作者: life (lifefuck)

功能:
1. 从 GitHub CHIME 数据集拉取结构化网络热梗（包含梗名与极短含义）
2. 过滤掉 profanity=True (低俗脏话) 和敏感违禁词
3. 从搜狗网络流行新词库提取最新高频流行词
4. 整理聚合生成 data/memes_pool.json，供 QQ Bot 动态轮换注入 Prompt
"""

import json
import logging
import os
import re
import sys
import urllib.request

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sync_hot_words")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
OUTPUT_FILE = os.path.join(DATA_DIR, "memes_pool.json")

# 本地代理配置 (遵循 lmxy 环境事实：7890 端口)
PROXY_HANDLER = urllib.request.ProxyHandler({
    "http": "http://127.0.0.1:7890",
    "https": "http://127.0.0.1:7890",
})
OPENER = urllib.request.build_opener(PROXY_HANDLER)

CHIME_URL = "https://raw.githubusercontent.com/yuboxie/chime/main/data/chime_full.json"
SOGOU_URL = "https://raw.githubusercontent.com/ASC8384/SogouPopularDict/main/data/sogou_network_words_current.txt"

# 核心涉政与严重风控违禁词（坚决拦截，保住 QQ 号防封）
BLOCKED_KEYWORDS = {
    "政治", "国家领导", "领导人", "反华", "涉政", "台独", "港独", "自杀", "献忠", "暴恐", "毒品", "枪支"
}

def fetch_url(url: str, timeout: int = 15) -> str:
    """使用带代理的 opener 拉取远程内容，若失败降级为直连"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with OPENER.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except Exception as e_proxy:
        logger.warning(f"代理拉取 {url} 失败: {e_proxy}，尝试直连...")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")

def is_safe_meme(term: str, meaning: str = "") -> bool:
    """安全性与可用性校验"""
    text = f"{term} {meaning}"
    if any(k in text for k in BLOCKED_KEYWORDS):
        return False
    # 过滤过长词或空词
    if not (2 <= len(term) <= 20):
        return False
    # 过滤纯数字或纯特殊符号
    if re.fullmatch(r"[\d\W]+", term):
        return False
    return True

def sync_memes() -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    memes_list = []
    seen_names = set()

    # 1. 获取并解析 CHIME 结构化网络梗库
    logger.info("正在获取高质量结构化网络梗库 (CHIME)...")
    try:
        raw_chime = fetch_url(CHIME_URL)
        chime_data = json.loads(raw_chime)
        for item in chime_data:
            name = item.get("meme", "").strip()
            meaning = item.get("meaning", "").strip()
            # 提取简短含义（压缩到 40 字以内）
            meaning_short = re.split(r"[。；\n]", meaning)[0].strip()
            if len(meaning_short) > 40:
                meaning_short = meaning_short[:37] + "..."

            if name and name not in seen_names and is_safe_meme(name, meaning):
                seen_names.add(name)
                memes_list.append({
                    "name": name,
                    "desc": meaning_short,
                    "source": "chime",
                    "type": item.get("type_cn", "网络梗")
                })
        logger.info(f"成功载入 CHIME 优质梗词: {len(memes_list)} 条")
    except Exception as e:
        logger.error(f"拉取 CHIME 梗库失败: {e}")

    # 2. 获取并解析搜狗最新网络新词库
    logger.info("正在获取搜狗最新流行新词...")
    sogou_count = 0
    try:
        raw_sogou = fetch_url(SOGOU_URL)
        lines = [line.strip() for line in raw_sogou.splitlines() if line.strip()]
        for line in lines:
            # 搜狗词库格式通常为单行词语
            word = line.split("\t")[0].strip()
            # 过滤过于普通的常规书名、电视剧名、地名或普通词
            if word in seen_names or not is_safe_meme(word):
                continue
            # 严格筛选具有网络社交属性的新词（过滤常规地理、机构、生硬普通名词）
            network_markers = (
                "哥", "姐", "酱", "帝", "王", "神", "批", "门", "症", "学", "文学", "体",
                "吃瓜", "逆天", "离谱", "破防", "硬控", "偷感", "红温", "包的", "那咋了",
                "老铁", "草台", "淡人", "浓人", "整活", "上头", "下头", "绝绝", "塌房",
                "开大", "对线", "拉踩", "画饼", "牛马", "打工人", "emo", "搞子", "串子"
            )
            # 满足具有网络特征或长度特征
            if not any(k in word for k in network_markers):
                continue

            if 2 <= len(word) <= 10:
                seen_names.add(word)
                memes_list.append({
                    "name": word,
                    "desc": "",
                    "source": "sogou_popular",
                    "type": "近期流行词"
                })
                sogou_count += 1
                if sogou_count >= 500:
                    break
        logger.info(f"成功补充搜狗流行新词: {sogou_count} 条")
    except Exception as e:
        logger.error(f"拉取搜狗流行词失败: {e}")

    # 3. 获取并解析社区与游戏热榜（Bilibili、贴吧、抖音聚合）
    logger.info("正在获取社区与亚文化高频热榜 (Bilibili/贴吧/抖音)... ")
    trending_count = 0
    sources = [("bilibili-hot-search", "B站热搜"), ("tieba", "贴吧热议"), ("douyin", "抖音热点")]
    for sid, sname in sources:
        try:
            raw_data = fetch_url(f"https://newsnow.busiyi.world/api/s?id={sid}", timeout=8)
            t_json = json.loads(raw_data)
            for it in t_json.get("items", [])[:15]:
                title = (it.get("title") or "").strip()
                if 2 <= len(title) <= 20 and not any(w in title for w in ("外交部", "气象", "地震", "总书记", "政治")):
                    if title not in seen_names and is_safe_meme(title):
                        seen_names.add(title)
                        memes_list.append({
                            "name": title,
                            "desc": f"近期{sname}高频热点事件/讨论话题",
                            "source": sid,
                            "type": "社区热搜"
                        })
                        trending_count += 1
        except Exception as e:
            logger.warning(f"拉取 {sname} 失败: {e}")

    # 尝试从 GitHub cxyfreedom/website-hot-hub 补充 B 站优质视频热梗
    try:
        hub_text = fetch_url("https://raw.githubusercontent.com/cxyfreedom/website-hot-hub/main/README.md", timeout=10)
        bili_m = re.search(r"## Bilibili.*?(?=## |\Z)", hub_text, re.DOTALL)
        if bili_m:
            for line in bili_m.group(0).splitlines()[:50]:
                m_t = re.search(r"^\d+\.\s*(?:《|【)?([^】》\n(（]{2,18})", line.strip())
                if m_t:
                    name = m_t.group(1).strip()
                    if name not in seen_names and not any(bad in name for bad in ("PV", "http", "宣传片", "自驾", "评测")):
                        if is_safe_meme(name):
                            seen_names.add(name)
                            clean_line = line.strip()[:35]
                            memes_list.append({
                                "name": name,
                                "desc": f"近期B站高频二创热点/爆款内容：{clean_line}",
                                "source": "github_hot_hub",
                                "type": "二创热梗"
                            })
                            trending_count += 1
    except Exception as e:
        logger.warning(f"拉取 website-hot-hub 失败: {e}")
    logger.info(f"成功补充多平台热搜与二创热点: {trending_count} 条")

    # 3.5 对未带详细背景的 Top 新热词尝试补充简短背景摘要
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if deepseek_key:
        unexplained = [m for m in memes_list if m.get("source") in ("bilibili-hot-search", "douyin", "tieba") and not m.get("desc_generated")][:15]
        if unexplained:
            logger.info(f"正在调用轻量 LLM 异步补充最新热词短释义 (待处理 {len(unexplained)} 条)...")
            try:
                names_prompt = "\n".join([f"{i+1}. {m['name']}" for i, m in enumerate(unexplained)])
                req_payload = {
                    "model": "deepseek-ai/DeepSeek-V3",
                    "messages": [
                        {"role": "system", "content": "你是一个懂各种网络热搜、游戏黑话和流行梗的年轻人。请为每个给出的网络热词或热搜标题写一句极其简炼的背景（20字以内，讲清楚这是什么梗、大家在调侃或关注什么）。输出格式：每行一个'序号. 解释'，不要废话。"},
                        {"role": "user", "content": names_prompt}
                    ],
                    "temperature": 0.5,
                    "max_tokens": 500
                }
                api_url = "https://api.siliconflow.cn/v1/chat/completions"
                req_obj = urllib.request.Request(
                    api_url,
                    data=json.dumps(req_payload).encode("utf-8"),
                    headers={"Authorization": f"Bearer {deepseek_key}", "Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req_obj, timeout=12) as resp:
                    if resp.status == 200:
                        res_data = json.loads(resp.read().decode())
                        ans_text = res_data["choices"][0]["message"]["content"]
                        for line in ans_text.splitlines():
                            m_idx = re.match(r"^(\d+)\.\s*(.*)", line.strip())
                            if m_idx:
                                idx = int(m_idx.group(1)) - 1
                                if 0 <= idx < len(unexplained):
                                    brief_desc = m_idx.group(2).strip()
                                    if brief_desc:
                                        unexplained[idx]["desc"] = brief_desc[:40]
                                        unexplained[idx]["desc_generated"] = True
                        logger.info("已成功为最新热搜补充一句话梗背景！")
            except Exception as e:
                logger.warning(f"自动提炼热搜梗摘要跳过或失败: {e}")

    # 4. 补充常青经典二次元/群友高频互动梗（保底库，含别名变体与圈子归类）
    classic_memes = [
        {"name": "打瓦", "desc": "指玩射击游戏《无畏契约》(Valorant)，常伴随瓦学弟/叫妈妈/笨蛋奶妈等抽象逆天二创梗", "source": "classic", "type": "游戏梗", "category": "game", "aliases": ["达瓦", "我要打瓦", "瓦罗兰特", "valorant", "打瓦叫妈", "贤者"]},
        {"name": "瓦学弟", "desc": "《无畏契约》玩家的戏称（与GO学长相对），常被二创调侃在游戏里爱认妈或叫妈妈带飞", "source": "classic", "type": "游戏梗", "category": "game", "aliases": ["瓦批", "无畏契约学弟"]},
        {"name": "三角洲堵桥", "desc": "《三角洲行动》航天基地发射桥玩法，乌鲁鲁/狙击手卡桥头封锁撤离点只杀人不吃包", "source": "classic", "type": "游戏梗", "category": "game", "aliases": ["发射桥", "航天基地堵桥", "三角洲封桥"]},
        {"name": "堵桥", "desc": "战术撤离或吃鸡游戏中守在必经桥头截杀撤离玩家（三角洲航天基地乌鲁鲁堵桥最出名）", "source": "classic", "type": "游戏梗", "category": "game", "aliases": ["守桥", "卡桥头", "桥头战神"]},
        {"name": "乌鲁鲁", "desc": "《三角洲行动》工程干员，因巡飞弹技能在发射桥堵桥极其恶心闻名", "source": "classic", "type": "游戏梗", "category": "game", "aliases": ["巡飞弹", "大胖子", "三角洲乌鲁鲁"]},
        {"name": "老六", "desc": "指游戏中游离于团队之外、行为猥琐阴险的伏击者或苟分老银币", "source": "classic", "type": "游戏梗", "category": "game", "aliases": ["当老六", "真老六", "老六行为", "老六架枪"]},
        {"name": "尊嘟假嘟", "desc": "真的假的谐音，可爱卖萌语气", "source": "classic", "type": "谐音", "category": "abstract", "aliases": ["尊嘟", "假嘟", "zundu"]},
        {"name": "泰裤辣", "desc": "太酷了谐音，调侃热血或中二名场面", "source": "classic", "type": "谐音", "category": "abstract", "aliases": ["太裤辣", "太酷啦"]},
        {"name": "电子布洛芬", "desc": "指看了让人治愈开心、缓解疲惫的视频或段子", "source": "classic", "type": "修辞", "category": "acg", "aliases": ["精神布洛芬"]},
        {"name": "偷感很重", "desc": "形容小心翼翼、生怕被发现的害羞或心虚状态", "source": "classic", "type": "现象", "category": "general", "aliases": ["偷感"]},
        {"name": "硬控", "desc": "形容被某事物深深吸引或震撼，完全动弹不得", "source": "classic", "type": "现象", "category": "general", "aliases": ["硬控我", "被硬控"]},
        {"name": "水灵灵地", "desc": "调侃某人毫无预兆、理直气壮地做出某事", "source": "classic", "type": "现象", "category": "general", "aliases": ["水灵灵"]},
        {"name": "班味", "desc": "上班久了身上散发出的疲惫、憔悴与沧桑感", "source": "classic", "type": "现象", "category": "general", "aliases": ["去班味", "班味重"]},
        {"name": "疯狂星期四", "desc": "肯德基特惠活动演变出的各种离谱反转求请客文案（v我50）", "source": "classic", "type": "梗", "category": "general", "aliases": ["v我50", "狂四", "肯德基星期四"]},
        {"name": "CPU/KTV", "desc": "调侃PUA的代称（“你在CPU我吗”）", "source": "classic", "type": "谐音", "category": "abstract", "aliases": ["cpu我", "你在cpu我"]},
        {"name": "纯爱战士", "desc": "崇尚专一美好爱情、拒绝牛头人剧情的二次元同好", "source": "classic", "type": "二次元", "category": "acg", "aliases": ["纯爱战神"]},
        {"name": "牛头人(NTR)", "desc": "调侃被横刀夺爱、绿帽背叛情节", "source": "classic", "type": "二次元", "category": "acg", "aliases": ["ntr", "牛头人"]},
        {"name": "发癫/发疯文学", "desc": "情绪饱满、逻辑混乱却极具感染力的情绪发泄对话", "source": "classic", "type": "文风", "category": "abstract", "aliases": ["发癫", "发疯"]},
        {"name": "赛博菩萨", "desc": "在网上无偿分享资源、攻略或解决疑难的好心人", "source": "classic", "type": "网络赞誉", "category": "general", "aliases": ["网络菩萨"]},
        {"name": "义父", "desc": "对送福利、借账号、请客大佬的尊称调侃", "source": "classic", "type": "群聊称呼", "category": "abstract", "aliases": ["请客义父", "活义父"]},
        {"name": "汗流浃背了吧", "desc": "对局或对线占据上风时调侃对方心虚紧张", "source": "classic", "type": "对线调侃", "category": "game", "aliases": ["汗流浃背", "老弟汗流浃背了吧"]},
        {"name": "老登/小登", "desc": "对群内老油条与新人群友的调侃称呼", "source": "classic", "type": "称呼", "category": "abstract", "aliases": ["老登", "小登"]},
        {"name": "红温", "desc": "源自电竞选手Uzi比赛失利脸色涨红，引申为情绪急躁、破防、愤怒", "source": "classic", "type": "游戏梗", "category": "game", "aliases": ["红温了", "急了急了", "破大防"]},
    ]
    for m in classic_memes:
        if m["name"] not in seen_names:
            seen_names.add(m["name"])
            memes_list.append(m)

    payload = {
        "updated_at": __import__("time").time(),
        "total_count": len(memes_list),
        "memes": memes_list
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    logger.info(f"词库同步完成，已落盘至 {OUTPUT_FILE}，总词数: {len(memes_list)}")
    return payload

if __name__ == "__main__":
    sync_memes()
