#!/bin/bash
# Embedding 服务（单机部署）：vLLM + Qwen3-Embedding-8B（--task embed）。
# 直接 bash run_embed.sh 即可；网关通过 EMBED_URL 连接，例如 http://<本机IP>:8082/v1
# 供 RAG 视频问答检索使用；未部署时网关自动降级为整段转写塞入上下文。

set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=run_helpers.sh
source "$(dirname "$0")/run_helpers.sh"

load_local_env

EMBED_PORT="${EMBED_PORT:-8082}"
EMBED_HOST="${EMBED_HOST:-0.0.0.0}"
EMBED_MODEL_PATH="${EMBED_MODEL_PATH:-}"
require_env EMBED_MODEL_PATH
EMBED_MODEL="${EMBED_MODEL:-embed}"
# 单条输入上限；RAG 按约 500 字分块 embed，16384 已远超单块需要。模型支持到 32K。
EMBED_MAX_MODEL_LEN="${EMBED_MAX_MODEL_LEN:-16384}"
# 8B 权重默认占约 0.6 显存；同机可再调 EMBED_GPU_MEMORY_UTILIZATION / EMBED_GPU_ID
EMBED_GPU_MEMORY_UTILIZATION="${EMBED_GPU_MEMORY_UTILIZATION:-0.6}"
EMBED_GPU_ID="${EMBED_GPU_ID:-0}"
LOG_DIR="${LOG_DIR:-logs}"
mkdir -p "$LOG_DIR"
EMBED_LOG="$LOG_DIR/vllm_embed_${EMBED_PORT}.log"

if [ ! -d "$EMBED_MODEL_PATH" ]; then
    echo "错误: 找不到 embedding 模型目录: ${EMBED_MODEL_PATH}" >&2
    exit 1
fi

stop_embed() {
    if [ -n "${EMBED_PID:-}" ]; then
        kill -9 "$EMBED_PID" 2>/dev/null || true
    fi
    pkill -9 -f "vllm serve.*--port ${EMBED_PORT}" 2>/dev/null || true
}

on_interrupt() {
    echo "正在停止 embedding 服务..."
    stop_embed
    exit
}
trap on_interrupt INT TERM

stop_embed
sleep 1

if [ -n "${CUDA_VISIBLE_DEVICES:-}" ]; then
    :
elif [ -n "${HIP_VISIBLE_DEVICES:-}" ]; then
    :
elif command -v nvidia-smi >/dev/null 2>&1; then
    export CUDA_VISIBLE_DEVICES="$EMBED_GPU_ID"
else
    export HIP_VISIBLE_DEVICES="$EMBED_GPU_ID"
fi

echo "=== 启动 vLLM Embedding 服务 ==="
echo "  监听:   ${EMBED_HOST}:${EMBED_PORT}"
echo "  模型:   ${EMBED_MODEL_PATH}"
echo "  名称:   ${EMBED_MODEL}"
echo "  参数:   max_model_len=${EMBED_MAX_MODEL_LEN}, gpu_mem=${EMBED_GPU_MEMORY_UTILIZATION}"
echo "  GPU:    CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-} HIP_VISIBLE_DEVICES=${HIP_VISIBLE_DEVICES:-}"

start_logged_bg "$EMBED_LOG" \
    vllm serve "$EMBED_MODEL_PATH" \
        --task embed \
        --served-model-name "$EMBED_MODEL" \
        --host "$EMBED_HOST" \
        --port "$EMBED_PORT" \
        --max-model-len "$EMBED_MAX_MODEL_LEN" \
        --gpu-memory-utilization "$EMBED_GPU_MEMORY_UTILIZATION" \
        --trust-remote-code
EMBED_PID=$!

wait_for_ready() {
    local probe_payload
    probe_payload=$(printf '{"model":"%s","input":"ready"}' "$EMBED_MODEL")

    for _ in $(seq 1 180); do
        if ! kill -0 "$EMBED_PID" 2>/dev/null; then
            echo "错误: vLLM embedding 进程已退出，见日志: ${EMBED_LOG}" >&2
            return 1
        fi

        probe_output=$(curl --noproxy '*' -sS -m 10 -w '\n%{http_code}' -X POST \
            "http://127.0.0.1:${EMBED_PORT}/v1/embeddings" \
            -H 'Content-Type: application/json' \
            -d "$probe_payload" \
            2>/dev/null || true)
        probe_code=$(echo "$probe_output" | tail -n 1)
        probe_body=$(echo "$probe_output" | sed '$d')
        if [[ "$probe_code" == "200" ]] && echo "$probe_body" | grep -q '"embedding"'; then
            echo "vLLM embedding 服务已就绪"
            echo "  对外地址: http://<本机IP>:${EMBED_PORT}/v1"
            echo "  EMBED_URL: http://<本机IP>:${EMBED_PORT}/v1"
            echo "  日志:     屏幕 + ${EMBED_LOG}"
            return 0
        fi
        sleep 2
    done
    echo "错误: vLLM embedding 服务未在预期时间内就绪" >&2
    return 1
}

if ! wait_for_ready; then
    stop_embed
    exit 1
fi

echo "按 Ctrl+C 停止"
wait "$EMBED_PID"
