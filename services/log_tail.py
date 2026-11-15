# -*- coding: utf-8 -*-
"""服务日志读取：给 /logs 指令供料，把 journal 尾部打包成可折叠的聊天记录。

只做两件事——把 journalctl 的输出读回来、按页切好；怎么发交给 handlers。
"""
import subprocess

UNIT = "qq-deepseek-bot"
MAX_LINES = 400          # 一次最多读 400 行，再多消息体太大、转发容易超时
DEFAULT_LINES = 120
PER_PAGE = 15            # 一个节点装 15 行：太长的节点手机上滚不动
MAX_LINE_CHARS = 400     # 单行截断，粘贴型报错不该整段塞进节点


def parse_count(arg: str = "") -> int:
    """从指令参数里取行数：`/logs 200` → 200，没有或写错就回默认值。"""
    digits = "".join(ch for ch in (arg or "") if ch.isdigit())
    if not digits:
        return DEFAULT_LINES
    return max(20, min(MAX_LINES, int(digits)))


def read_recent(limit: int = DEFAULT_LINES, grep: str = "") -> tuple[list[str], str]:
    """读服务日志尾部，返回（行列表，错误说明）。

    走 `journalctl --user`：服务本身就在用户会话里，不需要额外权限。
    --user 不可用时（比如换了运行环境）退一步读系统 journal。
    """
    limit = max(1, min(MAX_LINES, int(limit)))
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
    return [_clip(ln) for ln in lines], ""


def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + "…"


def pack_pages(lines: list[str], per_page: int = PER_PAGE) -> list[tuple[str, str]]:
    """把日志行切成 (节点标题, 节点正文)。标题用首尾时间，方便在折叠列表里定位。"""
    pages = []
    step = max(1, per_page)
    for start in range(0, len(lines), step):
        chunk = lines[start:start + step]
        first = _header_clock(chunk[0])
        last = _header_clock(chunk[-1])
        title = f"{first} ~ {last}" if first and last and first != last else (first or f"第 {start + 1} 行起")
        pages.append((title, "\n".join(chunk)))
    return pages


def _header_clock(line: str) -> str:
    """从 `2026-11-15 19:37:51 [INFO] ...` 里抠出 `19:37:51`，抠不到就空串。"""
    parts = (line or "").split(" ")
    if len(parts) >= 2 and parts[1].count(":") == 2:
        return parts[1]
    return ""
