#!/bin/bash
# 一键启动 webapp + Cloudflare Tunnel（免费公网访问）
# 用法: bash tunnel.sh [端口]
set -e
cd "$(dirname "$0")"
PORT="${1:-8080}"

echo "=== 自动点餐面板 + 公网隧道 ==="

# 检查 cloudflared
if ! command -v cloudflared &>/dev/null; then
  echo "安装 cloudflared..."
  if command -v pkg &>/dev/null; then
    pkg install cloudflared -y 2>/dev/null || {
      echo "尝试手动安装: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
      exit 1
    }
  elif command -v apt &>/dev/null; then
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/cloudflared.list
    sudo apt update && sudo apt install cloudflared -y
  else
    echo "请先安装 cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
    exit 1
  fi
fi

echo ""
echo "启动面板 (端口 $PORT)..."
python3 app.py --port "$PORT" --db webapp.db --repo ".." &
APP_PID=$!
sleep 1

echo "启动 Cloudflare 隧道..."
cloudflared tunnel --url http://localhost:$PORT --no-autoupdate 2>&1 &
TUNNEL_PID=$!

# 等隧道 URL 出现
echo ""
echo "等待隧道分配地址..."
sleep 5
TUNNEL_URL=$(ps aux | grep cloudflared | grep -o 'https://[^ ]*\.trycloudflare\.com' | head -1)

if [ -n "$TUNNEL_URL" ]; then
  echo ""
  echo "============================================"
  echo "  面板已上线！把这个链接发给同学："
  echo "  $TUNNEL_URL"
  echo "============================================"
  echo ""
  echo "（Ctrl+C 退出，隧道会自动关闭）"
else
  echo ""
  echo "隧道启动中，请查看上方 cloudflared 输出中的 https://xxx.trycloudflare.com 地址"
  echo ""
fi

# 捕获退出
trap "kill $APP_PID $TUNNEL_PID 2>/dev/null; echo '已停止'" EXIT INT TERM
wait
