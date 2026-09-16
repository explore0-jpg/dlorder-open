#!/usr/bin/env bash
# 微信/iOS 推送：notify.sh <标题> [正文 | @文件 | -stdin]
# 通道按先到先得：BARK_KEY(iOS Bark) > SENDKEY(Server酱-微信) > PUSHPLUS_TOKEN(PushPlus-微信)
# 推送失败退出码 1（让工作流转红曝光）。
set -euo pipefail

TOKK=""
SVR=""
CH=""

if [ -n "${BARK_KEY:-}" ]; then
  CH="Bark"
  SVR="${BARK_SERVER:-https://api.day.app/push}"
elif [ -n "${SENDKEY:-}" ]; then
  CH="Server酱"
  SVR="https://sctapi.ftqq.com/${SENDKEY}.send"
elif [ -n "${PUSHPLUS_TOKEN:-}" ]; then
  CH="PushPlus"
  SVR="${PUSHPLUS_SERVER:-https://www.pushplus.plus/send}"
else
  echo "[notify] 未配置任何推送通道（BARK_KEY/SENDKEY/PUSHPLUS_TOKEN）" >&2
  exit 1
fi

TITLE="${1:-自动抢餐通知}"
BODY=""
TMPF="${TMPDIR:-$HOME}/notify.$$.resp"
trap 'rm -f "$TMPF"' EXIT

if [ "${2:-}" = "-stdin" ]; then
  BODY="$(cat)"
elif [[ "${2:-}" == @* ]]; then
  FILE="${2#@}"
  if [ -f "$FILE" ]; then
    BODY="$(cat "$FILE")"
  else
    BODY="【文件缺失】$FILE"
  fi
else
  BODY="${2:-}"
fi

[ -z "$BODY" ] && BODY="（无详情，请看 GitHub Actions 日志）"

case "$CH" in
  Bark)
    PAYLOAD="$(BARK_KEY="$BARK_KEY" python3 -c 'import os,sys,json;print(json.dumps({"device_key":os.environ["BARK_KEY"],"title":sys.argv[1],"body":sys.argv[2],"level":"active","group":"自动抢餐"},ensure_ascii=False))' "$TITLE" "$BODY")"
    HTTP="$(curl -sS -m 30 -o "$TMPF" -w '%{http_code}' -H 'Content-Type: application/json' -d "$PAYLOAD" "$SVR")"
    OK=$(grep -qi '"code"\s*:\s*200' "$TMPF" 2>/dev/null && echo 1 || echo 0)
    ;;

  Server酱)
    HTTP="$(curl -sS -m 30 -o "$TMPF" -w '%{http_code}' \
      --data-urlencode "title=$TITLE" --data-urlencode "desp=$BODY" "$SVR")"
    OK=$(grep -q '"code":0' "$TMPF" 2>/dev/null && echo 1 || echo 0)
    ;;

  PushPlus)
    PAYLOAD="$(PUSHPLUS_TOKEN="$PUSHPLUS_TOKEN" python3 -c 'import os,sys,json;print(json.dumps({"token":os.environ["PUSHPLUS_TOKEN"],"title":sys.argv[1],"content":sys.argv[2],"template":"markdown"},ensure_ascii=False))' "$TITLE" "$BODY")"
    HTTP="$(curl -sS -m 30 -o "$TMPF" -w '%{http_code}' -H 'Content-Type: application/json' -d "$PAYLOAD" "$SVR")"
    OK=$(grep -q '"code":200' "$TMPF" 2>/dev/null && echo 1 || echo 0)
    ;;
esac

if [ "$HTTP" = "200" ] && [ "$OK" = "1" ]; then
  echo "[notify/${CH}] OK: $TITLE"
  exit 0
else
  echo "[notify/${CH}] 推送异常 HTTP=$HTTP: $(cat "$TMPF" 2>/dev/null | head -c 300)" >&2
  exit 1
fi