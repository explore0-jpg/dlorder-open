#!/bin/bash
# webpanel 启动脚本
set -e
cd "$(dirname "$0")"

if [ ! -f config.json ]; then
  echo "未找到 config.json，请先运行 bash setup.sh"
  exit 1
fi

HOST=$(python3 -c "import json;print(json.load(open('config.json'))['host'])")
PORT=$(python3 -c "import json;print(json.load(open('config.json'))['port'])")
DB=$(python3 -c "import json;print(json.load(open('config.json'))['db'])")
REPO=$(python3 -c "import json;print(json.load(open('config.json'))['repo'])")

echo "启动面板: http://${HOST}:${PORT}"
python3 app.py --host "$HOST" --port "$PORT" --db "$DB" --repo "$REPO"
