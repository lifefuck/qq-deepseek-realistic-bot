# -*- coding: utf-8 -*-
"""服务日志读取：给 /logs 指令供料，把 journal 尾部打包成可折叠的聊天记录。

只做两件事——把 journalctl 的输出读回来、按页切好；怎么发交给 handlers。
"""
import re
import subprocess

UNIT = "qq-deepseek-bot"
MAX_LINES = 400          # 一次最多读 400 行，再多消息体太大、转发容易超时
DEFAULT_LINES = 120
MAX_SOURCE_LINES = 1500  # 简易日志要读更多原行：大部分会被过滤掉，剩不了几条
DEFAULT_PLUS_LINES = 200   # 简易日志默认 200 行≈70 件事，三页看完
PER_PAGE = 15            # 一个节点装 15 行：太长的节点手机上滚不动
PLUS_PER_PAGE = 25       # 简易日志一行一个事件，切稀一点更好翻
MAX_LINE_CHARS = 400     # 单行截断，粘贴型报错不该整段塞进节点


def parse_count(arg: str = "", plus: bool = False) -> int:
    """从指令参数里取行数：`/logs 200` → 200，没有或写错就回默认值。"""
    digits = "".join(ch for ch in (arg or "") if ch.isdigit())
    top, default = (MAX_SOURCE_LINES, DEFAULT_PLUS_LINES) if plus else (MAX_LINES, DEFAULT_LINES)
    if not digits:
        return default
    return max(20, min(top, int(digits)))


def is_plus(arg: str = "") -> bool:
    """`/logs plus` 走简易人话版；`/logs 简易`、`/logs 简 300` 同样认。"""
    low = (arg or "").lower()
    return "plus" in low or "简易" in low or "+" in low or re.search(r"(^|\s)简", low) is not None


def read_recent(limit: int = DEFAULT_LINES, grep: str = "") -> tuple[list[str], str]:
    """读服务日志尾部，返回（行列表，错误说明）。

    走 `journalctl --user`：服务本身就在用户会话里，不需要额外权限。
    --user 不可用时（比如换了运行环境）退一步读系统 journal。
    """
    limit = max(1, min(MAX_SOURCE_LINES, int(limit)))
    base = ["-u", UNIT, "-o", "cat", "-n", str(limit), "--no-pager", "--no-hostname"]
    out = ""
    for extra in (["--user"], []):
        try:
            res = subprocess.run(["journalctl"] + extra + base, capture_output=True, text=True, timeout=15)
        except FileNotFoundError:
            return [], "本机没有 journalctl"
        except subprocess.TimeoutExpired:
            return [], "读日志超时"
        text = (res.stdout or "").strip()
        if text:
            out = text
            break
    if not out:
        return [], "journalctl 没返回内容"
    lines = [ln.rstrip() for ln in out.splitlines() if ln.strip()]
    if grep:
        hit = [ln for ln in lines if grep in ln]
        lines = hit or lines
    return [_clip(sanitize_line(ln)) for ln in lines], ""


def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + "…"


_CQ_RE = re.compile(r"\[CQ:([^\]]*)\]")


def sanitize_line(line: str) -> str:
    """把日志里的 CQ 码换成全角括号。

    日志里有群消息原文，夹着 `[CQ:image,file=...,url=...]`；节点正文会被协议端
    当 CQ 串解析，于是它去下载那张图，下载失败整条合并转发直接退化成纯文本。
    换成全角括号就只是个普通字符串了。
    """
    return _CQ_RE.sub(lambda m: f"【CQ:{m.group(1)}】", line or "")


def pack_pages(lines: list[str], per_page: int = PER_PAGE) -> list[tuple[str, str]]:
    """把日志行切成 (节点标题, 节点正文)。标题用首尾时间，方便在折叠列表里定位。"""
    pages = []
    step = max(1, per_page)
    for start in range(0, len(lines), step):
        chunk = lines[start:start + step]
        first = _header_clock(chunk[0])
        last = _header_clock(chunk[-1])
        title = f"{first} ~ {last}" if first and last and first != last else (first or f"第 {start + 1} 行起")
        pages.append((title, "\n".join(sanitize_line(ln) for ln in chunk)))
    return pages


def _header_clock(line: str) -> str:
    """从 `2026-11-15 19:37:51 [INFO] ...` 里抠出 `19:37:51`，抠不到就空串。"""
    parts = (line or "").split(" ")
    if len(parts) >= 2 and parts[1].count(":") == 2:
        return parts[1]
    return ""


# ---------------------------------------------------------------------------
# 简易日志：把技术日志翻译成人话
# ---------------------------------------------------------------------------

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) ")
_LINE_RE = re.compile(r"^\[(INFO|WARNING|ERROR)\] \[[^\]]*\] ?(.*)$")
_CHAT_RE = re.compile(r"^\[群 (\d+)\]\[(?:群聊|被@)\]\s*(.+)$")
_SENT_RE = re.compile(r"^\[群 (\d+)\] 消息已送达:\s*(.*)$")
_SCORE_RE = re.compile(r"^(?:\[群 (\d+)\] )?接话质量 ([\d.]+) vs 门槛 ([\d.]+) \((.*?)\) -> (\S+)")
_FAVOR_RE = re.compile(r"^\[好感判定\] 群 (\d+) 用户 (.*?)\((\d+)\): delta=([+-]?\d+), impression='(.*?)', 好感度: (\d+) -> (\d+)")
_DROP_RE = re.compile(r"^(?:\[群 (\d+)\] )?(.{0,24}?)丢弃[：:]\s*(.*)$")
_RECALL_RE = re.compile(r"^\[撤回\] 群 (\d+) 消息 (\d+) 被 (\d+) 撤回")
_EXIT_RE = re.compile(r"(已手动退出|服务已手动退出|Stopping qq-deepseek|Stopped qq-deepseek)")

# 纯技术噪音，简易日志一律不显示
_NOISE = ("前缀缓存命中", "群成员昵称缓存已更新", "触发轻量会话分析", "收到原始消息",
          "收到系统/群通知事件", "已成功加载按群隔离记忆库", "成功同步", "Web 仪表盘已启动",
          "白名单群:", "模型生成完成", "token", "tokens", "上下文长度", "共 ", "自动感知")
# 这些不是群友说的话，续行不该往里拼
_NONCHAT = ("systemd", "service", "Stopping", "Started", "Stopped", "Deactivated")

_DROP_WORDS = (("交集", "跟当前话题不相干"), ("复读", "刚说过类似的"), ("说过", "刚说过类似的"),
               ("连发", "刚连发过消息"), ("冷却", "冷却中"), ("长度", "太长了"))


def _pretty(text: str) -> str:
    """把 CQ 码翻译成看得懂的样子，再掐掉多余空白。"""
    out = text or ""
    out = re.sub(r"【CQ:image[^】]*】", "[图片]", out)
    out = re.sub(r"【CQ:(?:face|mface|bface|sface)[^】]*】", "[表情]", out)
    out = re.sub(r"【CQ:(?:reply|at)[^】]*】", "", out)
    out = re.sub(r"【CQ:forward[^】]*】", "[合并转发]", out)
    out = re.sub(r"【CQ:(?:record|video|file)[^】]*】", "[文件]", out)
    out = re.sub(r"\[管理员\]\s*", "管理员 ", out)
    out = re.sub(r"\(QQ \d+\)", "", out)
    return re.sub(r"\s+", " ", out).strip()


def summarize(lines: list[str], group_names: dict | None = None) -> tuple[list[str], int]:
    """把原始日志行翻成「谁在群里说了什么 / 我做了什么」，返回（人话行，被折叠的行数）。"""
    names = group_names or {}

    def gname(gid) -> str:
        key = str(gid or "")
        if not key.isdigit():
            return "群聊"
        return names.get(key) or names.get(int(key)) or f"群{key}"

    entries: list[str] = []
    hidden = 0
    for raw in lines:
        m = _TS_RE.match(raw)
        if not m:
            # 无时间戳：多为多行聊天的续行，也可能混进 systemd 的启停行
            cont = _pretty(raw)
            if entries and cont and len(cont) < 60 and entries[-1].startswith("💬") \
                    and not any(k in cont for k in _NONCHAT):
                entries[-1] += f" {cont}"
            continue
        clock, body = m.group(2), raw[m.end():]
        lm = _LINE_RE.match(body)
        level, msg = (lm.group(1), lm.group(2)) if lm else ("INFO", body)
        pretty = _pretty(msg)

        if any(k in msg for k in _NOISE):
            hidden += 1
            continue

        text = ""
        cm = _CHAT_RE.match(msg)
        sm = _SENT_RE.match(msg)
        qm = _SCORE_RE.match(msg)
        fm = _FAVOR_RE.match(msg)
        dm = _DROP_RE.match(msg)
        rm = _RECALL_RE.match(msg)

        if cm:
            text = f"💬 {gname(cm.group(1))}｜{_pretty(cm.group(2))}"
        elif sm:
            text = f"🤖 {gname(sm.group(1))}｜我：{_pretty(sm.group(2)) or '（空）'}"
        elif qm:
            gid, score, need, why, verdict = qm.groups()
            act = "开口了" if verdict.startswith("接") else "没开口"
            where = f"{gname(gid)} " if gid else ""
            text = f"📊 {where}旁观打分 {score} / 门槛 {need}（{_pretty(why)}）→ {act}"
        elif fm:
            gid, who, _uid, delta, imp, before, after = fm.groups()
            if delta in ("+0", "0", "-0") and not imp.strip():
                hidden += 1
                continue          # 好感没动的判定是纯噪音，不占篇幅
            extra = f"，印象：{_pretty(imp)[:40]}" if imp.strip() else ""
            text = f"🧠 {gname(gid)}｜对 {_pretty(who)} 好感 {before} → {after}（{delta}{extra}）"
        elif dm:
            gid, reason, content = dm.groups()
            reason = _pretty(reason)
            for key, human in _DROP_WORDS:
                if key in reason:
                    reason = human
                    break
            where = f"{gname(gid)} " if gid else ""
            text = f"🚫 {where}拦下（{reason or '不合时机'}）：{_pretty(content)[:80]}"
        elif rm:
            text = f"↩️ {gname(rm.group(1))}｜{rm.group(3)} 撤回了一条消息"
        elif "重连后补回" in msg:
            n = re.search(r"补回 (\d+) 条", msg)
            text = f"🔌 重连后补回 {n.group(1) if n else '若干'} 条历史消息"
        elif "已连接到 NapCat" in msg:
            text = "🔌 连上 NapCat，开始监听"
        elif "启动 QQ DeepSeek" in msg:
            text = "🚀 机器人启动"
        elif _EXIT_RE.search(msg):
            text = "🛑 服务退出"
        elif "网页只是站点外壳" in msg or "反爬页" in msg:
            text = "🕸 网页卡片没解析出来（反爬页）"
        elif "表情包已发送" in msg or "[表情包]" in msg:
            gm = re.search(r"\[群 (\d+)\]", msg)
            text = f"🖼 {gname(gm.group(1)) if gm else '群聊'}｜我发了个表情包"
        elif "戳一戳" in msg or "poke" in msg.lower():
            text = f"👉 {pretty[:80]}"
        elif level == "ERROR":
            text = f"❌ 出错：{pretty[:120]}"
        elif level == "WARNING":
            text = f"⚠️ 注意：{pretty[:120]}"
        elif any(k in msg for k in ("禁言", "踢出", "踢人", "撤回")):
            text = f"🛠 {pretty[:100]}"
        elif any(k in msg for k in ("记忆", "好感", "印象")):
            text = f"🧠 {pretty[:100]}"
        else:
            hidden += 1
            continue

        entries.append(f"{clock} {text}")
    return _collapse(entries), hidden


def _collapse(entries: list[str]) -> list[str]:
    """连续重复的同一句话压成一条：刷屏的「卡片没解析出来」不该占五页。"""
    out: list[str] = []
    last_text, repeat = "", 0
    for line in entries:
        clock, _, text = line.partition(" ")
        if text == last_text:
            repeat += 1
            continue
        if repeat:
            out[-1] = f"{out[-1]}（×{repeat + 1}）"
        out.append(line)
        last_text, repeat = text, 0
    if repeat:
        out[-1] = f"{out[-1]}（×{repeat + 1}）"
    return out


def pack_plus(entries: list[str], hidden: int = 0, per_page: int = PLUS_PER_PAGE) -> list[tuple[str, str]]:
    """简易日志分页；末尾补一个节点说明有多少行被折叠，免得以为漏了。"""
    pages = pack_pages(entries, per_page=per_page)
    if not pages:
        pages = [("没有事件", "这段时间日志里没有可读事件。")]
    if hidden:
        pages.append(("说明", f"另有 {hidden} 行技术日志被折叠（缓存、分词数、仪表盘状态这类）。\n想看原文：/logs"))
    return pages
