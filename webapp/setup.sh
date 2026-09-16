#!/bin/bash
# webpanel 初始化脚本（VPS 上运行一次）
set -e
cd "$(dirname "$0")"

echo "=== 自动点餐面板初始化 ==="

# 依赖检查
python3 -c "import sqlite3; import http.server; import hashlib" 2>/dev/null || {
  echo "错误: 需要 python3 (>=3.8)"
  exit 1
}

# 数据库初始化（默认 webapp.db）
DB="${1:-webapp.db}"
python3 -c "
from app import init_db
init_db('$DB')
print('数据库已初始化:', '$DB')
"

# 仓库路径检查
REPO="${2:-..}"
if [ ! -f "$REPO/users.cloud.json" ]; then
  echo "警告: $REPO/users.cloud.json 不存在，请检查仓库路径"
fi

# 生成配置
cat > config.json <<EOF
{
  "host": "0.0.0.0",
  "port": 8080,
  "db": "$DB",
  "repo": "$REPO",
  "note": "首个注册用户自动成为管理员"
}
EOF
echo "配置已生成: config.json"

echo ""
echo "=== 初始化完成 ==="
echo "运行: bash run.sh"
