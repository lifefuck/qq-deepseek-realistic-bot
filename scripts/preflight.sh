#!/usr/bin/env bash
# 提交前必过：语法 + 冒烟。拆模块那晚循环导入崩了八次才发现，就是缺这一道。
set -e
cd "$(dirname "$0")/.."

PY=.venv/bin/python
[ -x "$PY" ] || PY=python3

echo "==> 语法检查"
$PY -m py_compile bot.py connectors.py prompts.py safety_guard.py media_resolver.py \
    meme_catalog.py games.py cross_interaction.py cross_memory.py \
    services/*.py handlers/*.py

echo "==> 启动勾子引入检查"
$PY - <<'EOF'
import importlib, sys
# 单独 import 一遍，循环导入在这里就会炸，不用等起服务
for mod in ("bot", "handlers.group", "handlers.admin", "handlers.private", "services.llm", "services.messaging"):
    importlib.import_module(mod)
print("import ok")
EOF

echo "==> 冒烟测试"
$PY tests/smoke.py
