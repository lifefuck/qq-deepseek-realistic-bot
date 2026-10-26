#!/usr/bin/env bash
# 装上 git pre-commit 勾子。钩子本身不进版本库，新克隆的机器跑一次这个脚本即可。
set -e
cd "$(dirname "$0")/.."
mkdir -p .git/hooks
cat > .git/hooks/pre-commit <<'EOF'
#!/usr/bin/env bash
exec ./scripts/preflight.sh
EOF
chmod +x .git/hooks/pre-commit
echo "已安装 .git/hooks/pre-commit"
