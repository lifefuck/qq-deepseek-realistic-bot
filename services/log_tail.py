# -*- coding: utf-8 -*-
"""服务日志读取：给 /logs 指令供料，把 journal 尾部打包成可折叠的聊天记录。

只做两件事——把 journalctl 的输出读回来、按页切好；怎么发交给 handlers。
"""
import re
import subprocess

UNIT = "qq-deepseek-bot"
MAX_LINES = 400          # 一次最多读 400 行，再多消息体太大、转发容易超时
DEFAULT_LINES = 120
MAX_SOURCE_LINES = 1500  # 简易日志同屏行数可以要多一点（翻译后更短）
DEFAULT_PLUS_LINES = 120   # 和 /logs 一样多行，只是换成看得懂的说法
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
# 简易日志：逐行翻译成人话。不筛行、不折叠，行数和原文一一对应
# 输出一律纯文字，不加表情符号——emoji 一多就回到机器人腔
# ---------------------------------------------------------------------------

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) ")
_LINE_RE = re.compile(r"^\[(INFO|WARNING|ERROR)\] \[[^\]]*\] ?(.*)$")
_GROUP_PREFIX_RE = re.compile(r"^\[(?:群 )?(\d{5,12})\] ?(.*)$")
_CHAT_RE = re.compile(r"^\[群 (\d+)\]\[(?:群聊|被@)\]\s*(.+)$")
_SENT_RE = re.compile(r"^\[群 (\d+)\] 消息已送达:\s*(.*)$")
_SCORE_RE = re.compile(r"^(?:\[群 (\d+)\] )?接话质量 ([\d.]+) vs 门槛 ([\d.]+) \((.*?)\) -> (\S+)")
_FAVOR_RE = re.compile(r"^\[好感判定\] 群 (\d+) 用户 (.*?)\((\d+)\): delta=([+-]?\d+), impression='(.*?)', 好感度: (\d+) -> (\d+)")
_DROP_RE = re.compile(r"^(?:\[群 (\d+)\] )?(.{0,24}?)丢弃[：:]\s*(.*)$")
_RECALL_RE = re.compile(r"^\[撤回\] 群 (\d+) 消息 (\d+) 被 (\d+) 撤回")
_GEN_RE = re.compile(r"^模型生成完成 \(耗时 ([\d.]+)s, 档 (\w+), rt (\d+)\): ?(.*)$")
_CACHE_RE = re.compile(r"^前缀缓存命中 (\d+) / 输入 (\d+) tokens")
_NICK_RE = re.compile(r"^群成员昵称缓存已更新[：:]\s*(\d+) 人")
_RAW_RE = re.compile(r"^收到原始消息 raw_message='(.*?)', is_at=(True|False)")
_LIGHT_RE = re.compile(r"^触发轻量会话分析 \(is_at=(\w+), is_follow_up=(\w+)[^)]*\)[，,]?\s*(?:发言内容[:：]\s*)?(.*)$")
_TIMING_RE = re.compile(r"^接话时机[:：]\s*(.*)$")
_MEM_RE = re.compile(r"^已成功加载按群隔离记忆库[，,]\s*共 (\d+) 个分区[，,]\s*(\d+) 位群友档案")
_SYNC_RE = re.compile(r"^成功同步 (\d+) 个群的群名称缓存")
_RECONNECT_RE = re.compile(r"^\[?群 (\d+)\]?\s*重连后补回 (\d+) 条历史消息")
_FORWARD_OK_RE = re.compile(r"^合并转发已发送[（(](\d+) 条[)）]")
_EXIT_RE = re.compile(r"(已手动退出|Stopping qq-deepseek|Stopped qq-deepseek|Deactivated successfully)")

_NONCHAT = ("systemd", "service", "Stopping", "Started", "Stopped", "Deactivated")
_DROP_WORDS = (("交集", "跟当前话题不相干"), ("复读", "刚说过类似的"), ("说过", "刚说过类似的"),
               ("连发", "刚连发过消息"), ("冷却", "冷却中"), ("长度", "太长了"), ("间隔", "离上次回复太近"))


def _pretty(text: str) -> str:
    """把 CQ 码翻译成看得懂的样子，再收掉多余空白。"""
    out = text or ""
    # 截断过的行尾没有 `】`，收尾括号可省，否则图片码会裸露在正文里
    out = re.sub(r"【CQ:image[^】]*】?", "[图片]", out)
    out = re.sub(r"【CQ:(?:face|mface|bface|sface)[^】]*】?", "[表情]", out)
    out = re.sub(r"【CQ:(?:reply|at)[^】]*】?", "", out)
    out = re.sub(r"【CQ:forward[^】]*】?", "[合并转发]", out)
    out = re.sub(r"【CQ:(?:record|video|file)[^】]*】?", "[文件]", out)
    out = re.sub(r"【CQ:[^】]*】?", "[消息]", out)
    out = re.sub(r"\[管理员\]\s*", "管理员 ", out)
    out = re.sub(r"\(QQ \d+\)", "", out)
    return re.sub(r"\s+", " ", out).strip()


def _name_of(names: dict, gid) -> str:
    key = str(gid or "")
    if not key.isdigit():
        return "群聊"
    return names.get(key) or names.get(int(key)) or f"群{key}"


def translate_msg(msg: str, level: str = "INFO", gname=None, gid_hint: str = "") -> str:
    """把一条日志消息翻成人话。认不出来的原样留着，至少不会丢行。"""
    gname = gname or (lambda gid: _name_of({}, gid))
    pretty = _pretty(msg)

    # 很多行前面挂着 `[群 123456789]`，剥掉再匹配规则
    pref = _GROUP_PREFIX_RE.match(msg)
    if pref and not _CHAT_RE.match(msg):
        gid_hint = gid_hint or pref.group(1)
        msg = pref.group(2)
        pretty = _pretty(msg)

    who = f"{gname(gid_hint)}｜" if gid_hint else ""

    fw = _FORWARD_OK_RE.match(msg)
    if fw:
        return f"{who}聊天记录发出去 {fw.group(1)} 条"

    cm = _CHAT_RE.match(msg)
    if cm:
        return f"{gname(cm.group(1))}｜{_pretty(cm.group(2))}"

    sm = _SENT_RE.match(msg)
    if sm:
        return f"我回 {gname(sm.group(1))}：{_pretty(sm.group(2)) or '（空）'}"
    sm2 = re.match(r"^消息已送达[:：]\s*(.*)$", msg)
    if sm2:
        return f"我回 {gname(gid_hint) if gid_hint else '群聊'}：{_pretty(sm2.group(1)) or '（空）'}"

    qm = _SCORE_RE.match(msg)
    if qm:
        gid = qm.group(1) or gid_hint
        score, need, why, verdict = qm.group(2), qm.group(3), qm.group(4), qm.group(5)
        act = "接了" if verdict.startswith("接") else "没接"
        where = f"{gname(gid)} " if gid else ""
        return f"{where}打分 {score}，门槛 {need}（{_pretty(why)}），{act}"

    fm = _FAVOR_RE.match(msg)
    if fm:
        gid, person, _uid, delta, imp, before, after = fm.groups()
        if delta in ("+0", "0", "-0") and not imp.strip():
            return f"{gname(gid)}｜{_pretty(person)} 好感没变（{before}/100）"
        extra = f"，印象改成「{_pretty(imp)[:40]}」" if imp.strip() else ""
        return f"{gname(gid)}｜{_pretty(person)} 好感 {before} → {after}（{delta}{extra}）"

    dm = _DROP_RE.match(msg)
    if dm:
        gid, reason, content = dm.groups()
        reason = _pretty(reason)
        for key, human in _DROP_WORDS:
            if key in reason:
                reason = human
                break
        where = f"{gname(gid)} " if gid else ""
        return f"{where}没接（{reason or '时机不对'}）：{_pretty(content)[:80]}"

    rm = _RECALL_RE.match(msg)
    if rm:
        return f"{gname(rm.group(1))}｜{rm.group(3)} 撤回了一条"

    gm = _GEN_RE.match(msg)
    if gm:
        cost, tier, _rt, content = gm.groups()
        return f"生成 {cost} 秒（档 {tier}）：{_pretty(content)[:80]}"

    cr = _CACHE_RE.match(msg)
    if cr:
        hit, total = cr.groups()
        return f"上下文复用 {hit}，新读 {total}（token）"

    nm = _NICK_RE.match(msg)
    if nm:
        return f"刷新群昵称 {nm.group(1)} 人"

    raw_m = _RAW_RE.match(msg)
    if raw_m:
        at = "在 @ 我" if raw_m.group(2) == "True" else "没 @ 我"
        return f"收到消息（{at}）：{_pretty(raw_m.group(1))[:80]}"
    if msg.startswith("收到原始消息"):
        # 超长消息被截断，is_at 字段没留下来
        body_txt = re.sub(r"^收到原始消息 raw_message='?", "", msg).rstrip("'")
        return f"收到消息：{_pretty(body_txt)[:80]}"

    lm = _LIGHT_RE.match(msg)
    if lm:
        at = "在 @ 我" if lm.group(1) == "True" else "没 @ 我"
        follow = "，接我上一句" if lm.group(2) == "True" else ""
        said = _pretty(lm.group(3) or "")
        return f"看群体气氛（{at}{follow}）{('：' + said[:60]) if said else ''}"

    tm = _TIMING_RE.match(msg)
    if tm:
        return f"时机：{_pretty(tm.group(1))}"

    mem = _MEM_RE.match(msg)
    if mem:
        return f"记忆库就绪：{mem.group(1)} 个群、{mem.group(2)} 位群友有档案"

    sy = _SYNC_RE.match(msg)
    if sy:
        return f"记下 {sy.group(1)} 个群名"

    rc = _RECONNECT_RE.match(msg)
    if rc:
        return f"重连 {gname(rc.group(1))}，补回 {rc.group(2)} 条"
    rc2 = re.match(r"^重连后补回 (\d+) 条", msg)
    if rc2:
        return f"重连 {gname(gid_hint) if gid_hint else '群聊'}，补回 {rc2.group(1)} 条"

    if "收到系统/群通知事件" in msg:
        return "收到系统通知（入群、撤回这类）"
    if "正在连接 NapCat" in msg:
        return "正在连 NapCat"
    if "已连接到 NapCat" in msg:
        return "连上 NapCat，开始监听"
    if "启动 QQ DeepSeek" in msg:
        return "机器人启动"
    if "白名单群:" in msg:
        # 只取方括号里的群号，后面的机器人 QQ、管理员 QQ 不是群
        lm2 = re.search(r"白名单群[:：]\s*\[([^\]]*)\]", msg)
        gs = [g.strip() for g in (lm2.group(1).split(",") if lm2 else []) if g.strip().isdigit()]
        return f"上岗群：{('、'.join(gs[:6])) or '无'}" + ("…" if len(gs) > 6 else "")
    if "Web 仪表盘已启动" in msg:
        url = re.search(r"(https?://\S+)", msg)
        return f"网页面板起来了{'：' + url.group(1) if url else ''}"
    if _EXIT_RE.search(msg):
        return "服务退出"
    if msg.startswith("Started") or "Started qq-deepseek" in msg:
        return "服务被拉起"
    if "网页只是站点外壳" in msg or "反爬页" in msg:
        return "发来的网页没解析出来（反爬页）"
    if "表情包已发送" in msg:
        g2 = re.search(r"\[群 (\d+)\]", msg)
        return f"{gname(g2.group(1)) if g2 else '群聊'}｜我发了个表情包"
    if "戳一戳" in msg or "poke" in msg.lower():
        return f"戳一戳：{pretty[:80]}"
    if any(k in msg for k in ("禁言", "踢出", "踢人")):
        return pretty[:100]
    if "自动感知" in msg or "上下文长度" in msg:
        return pretty[:100]

    if level == "ERROR":
        return f"{who}出错：{pretty[:160]}"
    if level == "WARNING":
        return f"{who}警告：{pretty[:160]}"
    # 认不出来的原样留着：宁可留着看不懂，也别让行数对不上
    return f"{who}{pretty or '（空行）'}"


def translate(lines: list[str], group_names: dict | None = None) -> list[str]:
    """逐行翻译，行数与原文对齐（多行消息的续行并进上一条）。"""
    names = group_names or {}

    def gname(gid) -> str:
        return _name_of(names, gid)

    out: list[str] = []
    for raw in lines:
        m = _TS_RE.match(raw)
        if not m:
            # 无时间戳：多为多行消息的续行，也可能混进 systemd 的启停行
            cont = _pretty(raw)
            if out and cont and len(cont) < 80 and not any(k in cont for k in _NONCHAT):
                out[-1] += f" {cont}"
            continue
        clock, body = m.group(2), raw[m.end():]
        lm = _LINE_RE.match(body)
        level, msg = (lm.group(1), lm.group(2)) if lm else ("INFO", body)
        out.append(f"{clock} {translate_msg(msg, level, gname)}")
    return out


def pack_plus(entries: list[str], per_page: int = PER_PAGE) -> list[tuple[str, str]]:
    """简易日志分页：和原文一样一页 15 行，行数不缩水。"""
    return pack_pages(entries, per_page=per_page) or [("没有内容", "这段时间日志是空的。")]
