#!/bin/bash
# 大模型服务（单机部署）：vLLM + Qwen3。
# 网关通过 LLM_URL 连接，例如 http://<本机IP>:8081/v1

set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=run_helpers.sh
source "$(dirname "$0")/run_helpers.sh"

load_local_env

VLLM_PORT="${VLLM_PORT:-8081}"
VLLM_HOST="${VLLM_HOST:-0.0.0.0}"
LLM_MODEL_PATH="${LLM_MODEL_PATH:-}"
require_env LLM_MODEL_PATH
LLM_MODEL="${LLM_MODEL:-llm}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.55}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-64}"
BLOCK_SIZE="${BLOCK_SIZE:-16}"
GPU_ID="${GPU_ID:-0}"
LOG_DIR="${LOG_DIR:-logs}"
mkdir -p "$LOG_DIR"
VLLM_LOG="$LOG_DIR/vllm_${VLLM_PORT}.log"

if [ ! -d "$LLM_MODEL_PATH" ]; then
    echo "错误: 找不到大模型目录: ${LLM_MODEL_PATH}" >&2
    exit 1
fi

stop_vllm() {
    if [ -n "${VLLM_PID:-}" ]; then
        kill -9 "$VLLM_PID" 2>/dev/null || true
    fi
    pkill -9 -f "vllm serve.*--port ${VLLM_PORT}" 2>/dev/null || true
}

trap 'echo "正在停止 vLLM 服务..."; stop_vllm; exit' INT TERM

stop_vllm
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

echo "=== 启动 vLLM 服务 ==="
echo "  监听:   ${VLLM_HOST}:${VLLM_PORT}"
echo "  模型:   ${LLM_MODEL_PATH}"
echo "  名称:   ${LLM_MODEL}"
echo "  参数:   max_model_len=${MAX_MODEL_LEN}, gpu_mem=${GPU_MEMORY_UTILIZATION}, max_num_seqs=${MAX_NUM_SEQS}"
echo "  GPU:    CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-} HIP_VISIBLE_DEVICES=${HIP_VISIBLE_DEVICES:-}"

start_logged_bg "$VLLM_LOG" \
    vllm serve "$LLM_MODEL_PATH" \
        --served-model-name "$LLM_MODEL" \
        --host "$VLLM_HOST" \
        --port "$VLLM_PORT" \
        --max-model-len "$MAX_MODEL_LEN" \
        --enable-prefix-caching \
        --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION" \
        --block-size "$BLOCK_SIZE" \
        --max-num-seqs "$MAX_NUM_SEQS" \
        --trust-remote-code
VLLM_PID=$!

wait_for_ready() {
    for _ in $(seq 1 180); do
        if ! kill -0 "$VLLM_PID" 2>/dev/null; then
            echo "错误: vLLM 进程已退出，见日志: ${VLLM_LOG}" >&2
            return 1
        fi

        models_output=$(curl --noproxy "*" -sS -m 5 -w "\n%{http_code}" \
            "http://127.0.0.1:${VLLM_PORT}/v1/models" 2>/dev/null || true)
        models_code=$(echo "$models_output" | tail -n 1)
        models_body=$(echo "$models_output" | sed '$d')
        if [[ "$models_code" == "200" ]] && echo "$models_body" | grep -Eq "\"id\"[[:space:]]*:[[:space:]]*\"${LLM_MODEL}\""; then
            probe_output=$(curl --noproxy "*" -sS -m 10 -w "\n%{http_code}" -X POST \
                "http://127.0.0.1:${VLLM_PORT}/v1/chat/completions" \
                -H "Content-Type: application/json" \
                -d "{\"model\":\"${LLM_MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"ready\"}],\"max_tokens\":1,\"temperature\":0.0,\"chat_template_kwargs\":{\"enable_thinking\":false}}" \
                2>/dev/null || true)
            probe_code=$(echo "$probe_output" | tail -n 1)
            probe_body=$(echo "$probe_output" | sed '$d')
            if [[ "$probe_code" == "200" ]] && echo "$probe_body" | grep -q "\"choices\""; then
                echo "✅ vLLM 服务已就绪"
                echo "  对外地址: http://<本机IP>:${VLLM_PORT}/v1"
                echo "  LLM_URL:  http://<本机IP>:${VLLM_PORT}/v1"
                echo "  日志:     屏幕 + ${VLLM_LOG}"
                return 0
            fi
        fi
        sleep 2
    done
    echo "错误: vLLM 服务未在预期时间内就绪" >&2
    return 1
}

if ! wait_for_ready; then
    stop_vllm
    exit 1
fi

echo "按 Ctrl+C 停止"
wait "$VLLM_PID"
