#!/bin/bash
# 离线 ASR / 精修 Worker（单机部署）：offline_worker.py。
# 网关通过 WORKER_URL 连接，例如 http://<本机IP>:7001
# Worker 不依赖固定网关地址；默认由网关主动轮询任务事件（WORKER_EVENT_MODE=pull）。
# 若网关使用 push/hybrid，可由网关在任务中携带 callback_url。
# 仍需可访问 PUNCT_URL。

set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=run_helpers.sh
source "$(dirname "$0")/run_helpers.sh"

load_local_env

require_env PUNCT_URL
require_env QWEN3_OFFLINE_MODEL_PATH

OFFLINE_WORKER_PORT="${OFFLINE_WORKER_PORT:-7001}"
OFFLINE_WORKER_CONCURRENCY="${OFFLINE_WORKER_CONCURRENCY:-2}"
WORKER_INSTANCE_ID="${WORKER_INSTANCE_ID:-offline-worker-1}"
GPU_ID="${GPU_ID:-0}"
INTERNAL_WORKER_TOKEN="${INTERNAL_WORKER_TOKEN:-}"
LOG_DIR="${LOG_DIR:-logs}"
mkdir -p "$LOG_DIR"
WORKER_LOG="$LOG_DIR/offline_worker_${OFFLINE_WORKER_PORT}.log"

stop_worker() {
    if [ -n "${WORKER_PID:-}" ]; then
        kill -9 "$WORKER_PID" 2>/dev/null || true
    fi
    pkill -9 -f "offline_worker.py.*--port ${OFFLINE_WORKER_PORT}" 2>/dev/null || true
}

trap 'echo "正在停止离线 Worker..."; stop_worker; exit' INT TERM

stop_worker
sleep 1

if [ -n "${CUDA_VISIBLE_DEVICES:-}" ]; then
    :
elif [ -n "${HIP_VISIBLE_DEVICES:-}" ]; then
    :
elif command -v nvidia-smi >/dev/null 2>&1; then
    export CUDA_VISIBLE_DEVICES="$GPU_ID"
else
    export HIP_VISIBLE_DEVICES="$GPU_ID"
fi

export PUNCT_URL PUNCT_URLS="${PUNCT_URLS:-$PUNCT_URL}"
export INTERNAL_WORKER_TOKEN
export QWEN3_OFFLINE_MODEL_PATH OFFLINE_WORKER_CONCURRENCY WORKER_INSTANCE_ID
GATEWAY_PUBLIC_URL="${GATEWAY_PUBLIC_URL:-${GATEWAY_URL:-http://127.0.0.1:7860}}"
GATEWAY_CALLBACK_URL="${GATEWAY_CALLBACK_URL:-$GATEWAY_PUBLIC_URL}"
export GATEWAY_PUBLIC_URL GATEWAY_CALLBACK_URL

echo "=== 启动离线 Worker ==="
echo "  监听:   0.0.0.0:${OFFLINE_WORKER_PORT}"
echo "  网关回调: ${GATEWAY_CALLBACK_URL}（产物同步 + push 事件）"
echo "  标点:   ${PUNCT_URL}"
echo "  模型:   ${QWEN3_OFFLINE_MODEL_PATH}"
echo "  并发:   ${OFFLINE_WORKER_CONCURRENCY}"
echo "  GPU:    CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-} HIP_VISIBLE_DEVICES=${HIP_VISIBLE_DEVICES:-}"

start_logged_bg "$WORKER_LOG" \
    env WORKER_LOG_FILE="$WORKER_LOG" \
        OFFLINE_WORKER_PORT="$OFFLINE_WORKER_PORT" \
        python offline_worker.py --port "$OFFLINE_WORKER_PORT"
WORKER_PID=$!

wait_for_port() {
    for _ in $(seq 1 180); do
        if ! kill -0 "$WORKER_PID" 2>/dev/null; then
            echo "错误: offline_worker 已退出，见 ${WORKER_LOG}" >&2
            return 1
        fi
        if (echo > /dev/tcp/127.0.0.1/"$OFFLINE_WORKER_PORT") 2>/dev/null; then
            echo "✅ 离线 Worker 端口已开放（模型加载可能仍在进行，见日志）"
            echo "  对外地址: http://<本机IP>:${OFFLINE_WORKER_PORT}"
            echo "  日志: 屏幕 + ${WORKER_LOG}"
            return 0
        fi
        sleep 2
    done
    echo "错误: 离线 Worker 未在预期时间内就绪" >&2
    return 1
}

if ! wait_for_port; then
    stop_worker
    exit 1
fi

echo "按 Ctrl+C 停止"
wait "$WORKER_PID"
