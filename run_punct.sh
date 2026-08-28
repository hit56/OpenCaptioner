#!/bin/bash
# 标点服务（单机部署）：llama-server + Qwen3 标点模型。
# 网关通过 PUNCT_URL 连接，例如 http://<本机IP>:8080/v1/chat/completions

set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=run_helpers.sh
source "$(dirname "$0")/run_helpers.sh"

load_local_env

PUNCT_PORT="${PUNCT_PORT:-8080}"
PUNCT_MODEL="${PUNCT_MODEL:-./Qwen3_Merge-596M-F16.gguf}"
PUNCT_CTX_SIZE="${PUNCT_CTX_SIZE:-8192}"
PUNCT_BATCH="${PUNCT_BATCH:-2048}"
GPU_ID="${GPU_ID:-0}"
LOG_DIR="${LOG_DIR:-logs}"
mkdir -p "$LOG_DIR"
PUNCT_LOG="$LOG_DIR/punct_${PUNCT_PORT}.log"

if [ ! -f "$PUNCT_MODEL" ]; then
    echo "错误: 找不到标点模型: ${PUNCT_MODEL}" >&2
    exit 1
fi

stop_punct() {
    if [ -n "${PUNCT_PID:-}" ]; then
        kill -9 "$PUNCT_PID" 2>/dev/null || true
    fi
    pkill -9 -f "llama-server.*--port ${PUNCT_PORT}" 2>/dev/null || true
}

trap 'echo "正在停止标点服务..."; stop_punct; exit' INT TERM

stop_punct
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

echo "=== 启动标点服务 ==="
echo "  端口:  ${PUNCT_PORT}"
echo "  模型:  ${PUNCT_MODEL}"
echo "  GPU:   CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-} HIP_VISIBLE_DEVICES=${HIP_VISIBLE_DEVICES:-}"

start_logged_bg "$PUNCT_LOG" \
    ./llama-server \
        -m "$PUNCT_MODEL" \
        --port "$PUNCT_PORT" \
        --host 0.0.0.0 \
        --ctx-size "$PUNCT_CTX_SIZE" \
        -b "$PUNCT_BATCH" \
        --log-disable
PUNCT_PID=$!

wait_for_ready() {
    for i in $(seq 1 90); do
        if ! kill -0 "$PUNCT_PID" 2>/dev/null; then
            echo "错误: llama-server 已退出，见 ${PUNCT_LOG}" >&2
            return 1
        fi
        probe=$(curl --noproxy "*" -sS -m 5 -w "\n%{http_code}" -X POST "http://127.0.0.1:${PUNCT_PORT}/v1/chat/completions" \
            -H "Content-Type: application/json" \
            -d '{"model":"llm","messages":[{"role":"user","content":"ready"}],"max_tokens":1,"temperature":0.1,"chat_template_kwargs":{"enable_thinking":false}}' \
            2>/dev/null || true)
        code=$(echo "$probe" | tail -n 1)
        body=$(echo "$probe" | sed '$d')
        if [[ "$code" == "200" ]] && ! echo "$body" | grep -qi "loading model"; then
            echo "✅ 标点服务已就绪"
            echo "  对外地址: http://<本机IP>:${PUNCT_PORT}/v1/chat/completions"
            echo "  日志: 屏幕 + ${PUNCT_LOG}"
            return 0
        fi
        sleep 2
    done
    echo "错误: 标点服务未在预期时间内就绪" >&2
    return 1
}

if ! wait_for_ready; then
    stop_punct
    exit 1
fi

echo "按 Ctrl+C 停止"
wait "$PUNCT_PID"
