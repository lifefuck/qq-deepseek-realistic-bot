#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
防止「deque 当 list 切片」再犯：静态扫出所有对时间线变量的切片写法。

背景：group_timelines 的值是 deque，deque 不支持切片。曾经写过 timeline[-8:]，
每句话都抛 TypeError 被 asyncio 吞掉，机器人在线但不回话，查了半天才定位。

用法：.venv/bin/python scripts/check_timeline_slice.py
退出码非 0 表示发现违规。
"""

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGETS = ["bot.py", "handlers", "services", "games.py", "fun_features.py"]


TIMELINE_NAMES = {"timeline", "timelines", "group_timelines"}


def is_timeline(node: ast.AST) -> bool:
    """只认真正存 deque 的名字；timeline_text 这类字符串字段不算。"""
    if isinstance(node, ast.Name):
        return node.id in TIMELINE_NAMES or node.id.endswith("_timeline")
    if isinstance(node, ast.Subscript):
        return is_timeline(node.value)
    if isinstance(node, ast.Attribute):
        return node.attr in TIMELINE_NAMES or node.attr.endswith("_timeline")
    return False


def scan(path: pathlib.Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as e:
        return [f"{path}: 语法错误 {e}"]

    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
            if is_timeline(node.value):
                bad.append(
                    f"{path.relative_to(ROOT)}:{node.lineno} 对时间线做了切片，"
                    f"deque 不支持，先 list() 再切"
                )
    return bad


def main() -> int:
    files = []
    for t in TARGETS:
        p = ROOT / t
        files.extend(sorted(p.rglob("*.py")) if p.is_dir() else [p])

    problems = []
    for f in files:
        problems.extend(scan(f))

    if problems:
        print("\n".join(problems))
        return 1
    print(f"时间线切片检查通过（{len(files)} 个文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
