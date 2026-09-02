#!/usr/bin/env bash
# ============================================================
# AuroraAgent 一键演示脚本
#   1) 启动 Web 服务（默认 mock 模式；可用 --api 传真实 API）
#   2) 可选：用 cloudflared 暴露公网链接
#
# 用法:
#   ./scripts/demo.sh                     # mock 模式 + 起服务 + 可选隧道
#   ./scripts/demo.sh --api               # 使用 .env 里的真实 API
#   ./scripts/demo.sh --api --no-tunnel   # 只起本地服务，不建隧道
# ============================================================
set -euo pipefail

cd "$(dirname "$0")/.."

API_MODE=0
TUNNEL=1
PORT="${AURORA_PORT:-8000}"

for arg in "$@"; do
  case "$arg" in
    --api) API_MODE=1 ;;
    --no-tunnel) TUNNEL=0 ;;
    *) echo "未知参数: $arg"; exit 1 ;;
  esac
done

echo "=============================================="
echo "  ▲ 极光 Agent OS — 演示启动"
echo "=============================================="
echo "端口: $PORT  模式: $( [ "$API_MODE" = 1 ] && echo 真实API || echo mock )"

ARGS=(--port "$PORT")
if [ "$API_MODE" = 1 ]; then
  set -a; [ -f .env ] && source .env; set +a
  if [ -z "${AURORA_API_KEY:-}" ]; then
    echo "!! --api 模式需要 AURORA_API_KEY（在 .env 或环境变量中）"
    exit 1
  fi
  ARGS+=(--api-base "${AURORA_API_BASE:-https://api.deepseek.com/v1}"
         --api-key "$AURORA_API_KEY"
         --model "${AURORA_MODEL:-deepseek-chat}")
else
  ARGS+=(--mock)
fi

# 启动服务（后台）
echo ">> 启动 Web 服务 http://127.0.0.1:$PORT ..."
PYTHONIOENCODING=utf-8 python run.py "${ARGS[@]}" &
SERVER_PID=$!
trap 'echo; echo ">> 停止服务(PID $SERVER_PID)..."; kill $SERVER_PID 2>/dev/null || true' EXIT INT TERM

sleep 3
echo ">> 本地可访问: http://127.0.0.1:$PORT"

if [ "$TUNNEL" = 1 ]; then
  if command -v cloudflared >/dev/null 2>&1; then
    echo ">> 正在建立 cloudflared 公网隧道 ..."
    echo "   复制下面的 https 链接发给远程访问者即可远程访问："
    cloudflared tunnel --url "http://127.0.0.1:$PORT"
  else
    echo "!! 未检测到 cloudflared，跳过隧道。"
    echo "   可手动执行: cloudflared tunnel --url http://127.0.0.1:$PORT"
    echo "   (或访问本地地址进行录屏)"
  fi
else
  echo ">> 已在本地运行。Ctrl+C 停止。"
  wait "$SERVER_PID"
fi