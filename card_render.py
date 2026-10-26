# -*- coding: utf-8 -*-
"""群里发的那几张图：年鉴、关系网、周榜。

只用 Pillow 手画，不引 matplotlib——为几张卡片装一个几十兆的绘图库不划算。
字体走系统 Noto Sans CJK，index=2 是简体。
"""
import io
import math
import os
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

_FONT_REG = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
_FONT_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"

BG = (11, 18, 32)
CARD = (19, 28, 46)
TEXT = (232, 238, 247)
MUTED = (140, 155, 178)
ACCENT = (59, 130, 246)      # 克莱因蓝
ACCENT_2 = (56, 189, 248)
GOLD = (251, 191, 36)

_GOLD_PALETTE = [(251, 191, 36), (148, 163, 184), (180, 120, 60), (59, 130, 246), (56, 189, 248)]


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = _FONT_BOLD if bold else _FONT_REG
    try:
        return ImageFont.truetype(path, size, index=2)
    except Exception:
        return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> List[str]:
    """中文没空格可断，只能按字符累加量宽度。"""
    lines, buf = [], ""
    for ch in text:
        if draw.textlength(buf + ch, font=font) <= width:
            buf += ch
        else:
            lines.append(buf)
            buf = ch
    if buf:
        lines.append(buf)
    return lines


def _gradient(width: int, height: int, top=BG, bottom=(5, 10, 20)) -> Image.Image:
    img = Image.new("RGB", (1, height))
    px = img.load()
    for y in range(height):
        t = y / max(1, height - 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    return img.resize((width, height))


def _stat_block(draw, x, y, w, h, label, value):
    draw.rounded_rectangle([x, y, x + w, y + h], radius=16, fill=CARD)
    draw.text((x + 20, y + 18), label, font=_font(20), fill=MUTED)
    draw.text((x + 20, y + 48), str(value), font=_font(44, bold=True), fill=TEXT)


def render_yearbook(group_name: str, totals: dict, top_msgs: List[Tuple[int, str, int]],
                    top_night: List[Tuple[int, str, int]], top_chars: List[Tuple[int, str, int]]) -> bytes:
    boards = [("话 最 多", top_msgs, "条", ACCENT),
              ("字 最 多", top_chars, "字", ACCENT_2),
              ("半 夜 还 在", top_night, "次", GOLD)]
    # 高度按榜单实际条数算，写死就会被最后一行顶穿
    body_y = 348
    H = body_y + sum(54 + max(1, len(rows)) * 78 + 20 for _t, rows, _u, _c in boards) + 90
    W = 900
    img = _gradient(W, H)
    d = ImageDraw.Draw(img)

    # 顶部品牌条
    d.rounded_rectangle([40, 40, 40 + 6, 148], radius=3, fill=ACCENT)
    d.text((70, 44), "群 年 鉴", font=_font(52, bold=True), fill=TEXT)
    d.text((72, 110), f"{group_name}", font=_font(24), fill=MUTED)

    y = 200
    bw = (W - 80 - 3 * 16) // 4
    _stat_block(d, 40, y, bw, 108, "群友", totals.get("members", 0))
    _stat_block(d, 40 + (bw + 16), y, bw, 108, "消息", totals.get("msgs", 0))
    _stat_block(d, 40 + 2 * (bw + 16), y, bw, 108, "总字数", totals.get("chars", 0))
    _stat_block(d, 40 + 3 * (bw + 16), y, bw, 108, "深夜", totals.get("night", 0))

    def board(title, rows, start_y, unit="条", color=ACCENT):
        d.text((40, start_y), title, font=_font(30, bold=True), fill=TEXT)
        yy = start_y + 54
        if not rows:
            d.text((40, yy), "数据还不够，再攒一阵", font=_font(22), fill=MUTED)
            return yy + 98
        peak = max(r[2] for r in rows) or 1
        for i, (_uid, nick, val) in enumerate(rows):
            name = nick if len(nick) <= 12 else nick[:11] + "…"
            rank_color = _GOLD_PALETTE[i] if i < 3 else MUTED
            d.text((40, yy), f"{i + 1}", font=_font(26, bold=True), fill=rank_color)
            d.text((80, yy), name, font=_font(26), fill=TEXT)
            d.text((W - 40 - d.textlength(f"{val} {unit}", font=_font(24)), yy + 2),
                   f"{val} {unit}", font=_font(24), fill=MUTED)
            bar_y = yy + 44
            d.rounded_rectangle([80, bar_y, W - 40, bar_y + 8], radius=4, fill=(30, 41, 62))
            d.rounded_rectangle([80, bar_y, 80 + int((W - 120) * val / peak), bar_y + 8], radius=4, fill=color)
            yy += 78
        return yy + 20

    y = body_y
    for title, rows, unit, color in boards:
        y = board(title, rows, y, unit, color)

    d.text((40, H - 60), f"数据跨度 {totals.get('days', 1)} 天 · 作者 life", font=_font(20), fill=MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_relation_map(group_name: str, nodes: List[Tuple[int, str, int]],
                        edges: List[Tuple[int, int, int]]) -> bytes:
    """nodes: (uid, nick, weight)；edges: (uid_a, uid_b, count)。圆形布局，不用力导向那套。"""
    S = 1000
    img = _gradient(S, S, (12, 20, 36), (6, 10, 18))
    d = ImageDraw.Draw(img)

    d.text((48, 44), "群 内 关 系 网", font=_font(46, bold=True), fill=TEXT)
    d.text((50, 106), f"{group_name} · 谁在跟谁说话", font=_font(22), fill=MUTED)

    if len(nodes) < 2:
        d.text((50, 500), "互动数据还不够，再让群里吵几天", font=_font(28), fill=MUTED)
        buf = io.BytesIO(); img.save(buf, format="PNG"); return buf.getvalue()

    cx, cy, R = S // 2, S // 2 + 40, 330
    pos = {}
    weight = {uid: w for uid, _n, w in nodes}
    max_w = max(weight.values()) or 1
    for i, (uid, _nick, _w) in enumerate(nodes):
        ang = -math.pi / 2 + 2 * math.pi * i / len(nodes)
        pos[uid] = (cx + R * math.cos(ang), cy + R * math.sin(ang))

    max_edge = max([c for _a, _b, c in edges], default=1) or 1
    for a, b, cnt in edges:
        if a not in pos or b not in pos:
            continue
        w = 1 + int(5 * cnt / max_edge)
        d.line([pos[a], pos[b]], fill=(45, 74, 120), width=w)

    for uid, nick, w in nodes:
        x, y = pos[uid]
        r = 26 + int(26 * w / max_w)
        d.ellipse([x - r, y - r, x + r, y + r], fill=CARD, outline=ACCENT, width=3)
        label = nick if len(nick) <= 6 else nick[:5] + "…"
        tw = d.textlength(label, font=_font(22))
        ly = y + r + 8 if y < cy else y - r - 34
        d.text((x - tw / 2, ly), label, font=_font(22), fill=TEXT)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_weekly(group_name: str, rows: List[Tuple[int, str, int]], week_label: str) -> bytes:
    W = 900
    H = 260 + max(1, len(rows)) * 96 + 90
    img = _gradient(W, H, (14, 20, 34), (7, 11, 20))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle([40, 40, 46, 140], radius=3, fill=GOLD)
    d.text((70, 44), "本 周 好 感 榜", font=_font(46, bold=True), fill=TEXT)
    d.text((72, 108), f"{group_name} · {week_label}", font=_font(22), fill=MUTED)

    y = 200
    if not rows:
        d.text((40, y), "这周还没人说话，榜是空的", font=_font(26), fill=MUTED)
    peak = max([r[2] for r in rows], default=1) or 1
    for i, (_uid, nick, val) in enumerate(rows):
        d.text((40, y), f"{i + 1}", font=_font(30, bold=True), fill=_GOLD_PALETTE[i % len(_GOLD_PALETTE)])
        d.text((84, y), nick if len(nick) <= 12 else nick[:11] + "…", font=_font(28), fill=TEXT)
        d.text((W - 40 - d.textlength(f"{val} 条", font=_font(24)), y + 4), f"{val} 条", font=_font(24), fill=MUTED)
        by = y + 46
        d.rounded_rectangle([84, by, W - 40, by + 10], radius=5, fill=(30, 41, 62))
        d.rounded_rectangle([84, by, 84 + int((W - 124) * val / peak), by + 10], radius=5, fill=GOLD)
        y += 96

    d.text((40, H - 56), "每周一凌晨结算，一周不说话会掉好感", font=_font(20), fill=MUTED)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
