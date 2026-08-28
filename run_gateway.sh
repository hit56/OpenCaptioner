#!/bin/bash
# 网关 + 本机离线 ASR：gateway_server.py + offline_worker.py。
# 标点 / LLM 为远程服务，通过环境变量或 .env.local 配置。

set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=run_helpers.sh
source "$(dirname "$0")/run_helpers.sh"

load_local_env

# embedding 服务（RAG 检索用），可选：未部署时 RAG 问答自动降级为塞全文。
: "${EMBED_URL:=http://127.0.0.1:8082/v1}"
EMBED_MODEL="${EMBED_MODEL:-embed}"
export PUNCT_URL="${PUNCT_URL:-}"
export LLM_URL="${LLM_URL:-}"
export BILI_META_LLM_URL="${BILI_META_LLM_URL:-}"
export BILI_COVER_IMAGE_URL="${BILI_COVER_IMAGE_URL:-}"
export EMBED_URL EMBED_MODEL
# 离线 worker 拉取 /cache、/files 与回调 /internal/* 的地址；需要平台 HTTPS 映射时显式覆盖。
export GATEWAY_PUBLIC_URL="${GATEWAY_PUBLIC_URL:-}"
export GATEWAY_CALLBACK_URL="${GATEWAY_CALLBACK_URL:-}"
export GATEWAY_PUBLIC_URL GATEWAY_CALLBACK_URL

require_env LLM_URL
require_env PUNCT_URL

stop_gateway() {
    if [ -n "${GATEWAY_PID:-}" ]; then
        kill -9 "$GATEWAY_PID" 2>/dev/null || true
    fi
    pkill -9 -f "gateway_server.py" 2>/dev/null || true
}

stop_worker() {
    if [ -n "${WORKER_PID:-}" ]; then
        kill -9 "$WORKER_PID" 2>/dev/null || true
    fi
    pkill -9 -f "offline_worker.py.*--port ${OFFLINE_WORKER_PORT:-7001}" 2>/dev/null || true
}

stop_all() {
    stop_gateway
    stop_worker
}

trap 'echo "正在停止 Gateway 和离线 Worker..."; stop_all; exit' INT TERM

LOG_DIR="${LOG_DIR:-logs}"
if [[ -d "$LOG_DIR" ]]; then
    # Keep directory inode stable and just clear existing files.
    rm -f "$LOG_DIR"/* "$LOG_DIR"/.[!.]* "$LOG_DIR"/..?* 2>/dev/null || true
else
    mkdir -p "$LOG_DIR"
fi
mkdir -p "$LOG_DIR"
GATEWAY_LOG="$LOG_DIR/gateway_server.log"
OFFLINE_WORKER_PORT="${OFFLINE_WORKER_PORT:-7001}"
OFFLINE_WORKER_CONCURRENCY="${OFFLINE_WORKER_CONCURRENCY:-2}"
WORKER_INSTANCE_ID="${WORKER_INSTANCE_ID:-offline-worker-1}"
RUN_LOCAL_OFFLINE_WORKER="${RUN_LOCAL_OFFLINE_WORKER:-1}"
WORKER_URL="${WORKER_URL:-http://127.0.0.1:${OFFLINE_WORKER_PORT}}"
WORKER_LOG="$LOG_DIR/offline_worker_${OFFLINE_WORKER_PORT}.log"

is_local_or_internal_host() {
    local host
    host="$(echo "${1:-}" | tr '[:upper:]' '[:lower:]')"
    case "$host" in
        localhost|::1) return 0 ;;
        127.*|10.*|192.168.*) return 0 ;;
        172.1[6-9].*|172.2[0-9].*|172.3[0-1].*) return 0 ;;
        *) return 1 ;;
    esac
}

GATEWAY_PORT="${GATEWAY_PORT:-7860}"
GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:${GATEWAY_PORT}}"
GATEWAY_PUBLIC_URL="${GATEWAY_PUBLIC_URL:-$GATEWAY_URL}"
GATEWAY_CALLBACK_URL="${GATEWAY_CALLBACK_URL:-$GATEWAY_PUBLIC_URL}"
: "${SCNET_OAUTH_CLIENT_ID:=}"
: "${SCNET_OAUTH_CLIENT_SECRET:=}"
if [ -z "${SCNET_OAUTH_REDIRECT_URI:-}" ]; then
    gateway_host="$(python3 - <<'PY' "$GATEWAY_PUBLIC_URL"
import sys
from urllib.parse import urlparse
host = (urlparse(sys.argv[1]).hostname or "").lower()
print(host)
PY
)"
    if ! is_local_or_internal_host "$gateway_host"; then
        SCNET_OAUTH_REDIRECT_URI="${GATEWAY_PUBLIC_URL}/"
    fi
fi
export SCNET_OAUTH_CLIENT_ID SCNET_OAUTH_CLIENT_SECRET SCNET_OAUTH_REDIRECT_URI
INTERNAL_WORKER_TOKEN="${INTERNAL_WORKER_TOKEN:-}"
LLM_MODEL="${LLM_MODEL:-llm}"
ENABLE_CONTENT_SAFETY="${ENABLE_CONTENT_SAFETY:-0}"
GPU_ID="${GPU_ID:-0}"
QWEN3_OFFLINE_MODEL_PATH="${QWEN3_OFFLINE_MODEL_PATH:-}"
FORCED_ALIGNER_MODEL_PATH="${FORCED_ALIGNER_MODEL_PATH:-}"

if [ -n "${CUDA_VISIBLE_DEVICES:-}" ]; then
    :
elif [ -n "${HIP_VISIBLE_DEVICES:-}" ]; then
    :
elif command -v nvidia-smi >/dev/null 2>&1; then
    export CUDA_VISIBLE_DEVICES="$GPU_ID"
else
    export HIP_VISIBLE_DEVICES="$GPU_ID"
fi

append_no_proxy_host() {
    local endpoint="${1:-}"
    local host
    host="$(echo "$endpoint" | sed -E 's#^[a-zA-Z]+://##' | sed -E 's#/.*##' | sed -E 's#:.*$##' | sed -E 's#^\[##; s#\]$##')"
    if [ -n "$host" ] && is_local_or_internal_host "$host"; then
        NO_PROXY="${NO_PROXY},${host}"
    fi
}

FRONTEND_DIR="${FRONTEND_DIR:-frontend}"
ENABLE_FRONTEND_BUILD="${ENABLE_FRONTEND_BUILD:-1}"
FRONTEND_BUILD_INSTALL_DEPS="${FRONTEND_BUILD_INSTALL_DEPS:-0}"

build_frontend_if_needed() {
    if [[ ! -d "${FRONTEND_DIR}" ]]; then
        echo "错误: 前端目录不存在: ${FRONTEND_DIR}" >&2
        return 1
    fi
    if [[ "${ENABLE_FRONTEND_BUILD}" == "1" ]]; then
        if ! command -v npm >/dev/null 2>&1; then
            echo "错误: 未检测到 npm，无法执行前端构建。" >&2
            return 1
        fi
        echo "=== 构建 React 前端 ==="
        if [[ "${FRONTEND_BUILD_INSTALL_DEPS}" == "1" ]]; then
            npm --prefix "${FRONTEND_DIR}" install
        fi
        npm --prefix "${FRONTEND_DIR}" run build
    else
        echo "=== 已跳过前端构建 (ENABLE_FRONTEND_BUILD=${ENABLE_FRONTEND_BUILD})，使用既有 dist ==="
    fi
    if [[ ! -f "${FRONTEND_DIR}/dist/index.html" ]]; then
        echo "错误: 未找到 ${FRONTEND_DIR}/dist/index.html，请先构建前端或设置 ENABLE_FRONTEND_BUILD=1" >&2
        return 1
    fi
}

echo "=== 清理旧 Gateway / 离线 Worker 进程 ==="
stop_all
sleep 1

if ! build_frontend_if_needed; then
    exit 1
fi

echo "=== 安装字体（字幕渲染）==="
mkdir -p ~/.local/share/fonts
cp -f msyh.ttf ~/.local/share/fonts/ 2>/dev/null || true
cp -f NotoSansThai-Regular.ttf ~/.local/share/fonts/ 2>/dev/null || true
fc-cache -fv >/dev/null 2>&1 || true

export LLM_URL LLM_MODEL GATEWAY_PORT GATEWAY_PUBLIC_URL GATEWAY_CALLBACK_URL INTERNAL_WORKER_TOKEN
export EMBED_URL EMBED_MODEL
export FORCED_ALIGNER_MODEL_PATH QWEN3_OFFLINE_MODEL_PATH OFFLINE_WORKER_CONCURRENCY WORKER_INSTANCE_ID
export PUNCT_URL PUNCT_URLS="${PUNCT_URLS:-$PUNCT_URL}"
export WORKER_URL WORKER_URLS="${WORKER_URLS:-$WORKER_URL}"
export ENABLE_CONTENT_SAFETY
export ADMIN_USERNAMES="${ADMIN_USERNAMES:-}"
# 仅本机/内网地址默认直连；其余是否走代理由 .env.local 的 http_proxy / NO_PROXY 决定。
NO_PROXY="127.0.0.1,localhost,::1,${NO_PROXY:-}"
append_no_proxy_host "$GATEWAY_URL"
append_no_proxy_host "$GATEWAY_PUBLIC_URL"
append_no_proxy_host "$WORKER_URL"
append_no_proxy_host "$PUNCT_URL"
append_no_proxy_host "$LLM_URL"
append_no_proxy_host "$BILI_META_LLM_URL"
append_no_proxy_host "$BILI_COVER_IMAGE_URL"
append_no_proxy_host "$EMBED_URL"
NO_PROXY="$(echo "$NO_PROXY" | sed 's/^,*//' | sed 's/,,*/,/g')"
export NO_PROXY
export no_proxy="$NO_PROXY"

wait_for_port() {
    local port=$1 name=$2 pid="${3:-}" timeout="${4:-180}"
    for _ in $(seq 1 "$timeout"); do
        if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
            echo "错误: ${name} 已退出" >&2
            return 1
        fi
        if (echo > /dev/tcp/127.0.0.1/"$port") 2>/dev/null; then
            echo "✅ ${name} 已就绪（端口 ${port}）"
            return 0
        fi
        sleep 1
    done
    echo "错误: ${name} 未在 ${timeout}s 内就绪" >&2
    return 1
}

echo "=== 启动 Gateway + 本机离线 Worker ==="
echo "  Gateway:        ${GATEWAY_URL}"
echo "  Public URL:     ${GATEWAY_PUBLIC_URL}  (离线 worker 拉取音频/回调)"
echo "  Punct:          ${PUNCT_URL}"
echo "  Offline Worker: ${WORKER_URL}"
echo "  Worker Model:   ${QWEN3_OFFLINE_MODEL_PATH}"
echo "  Worker Concur:  ${OFFLINE_WORKER_CONCURRENCY}"
echo "  LLM:            ${LLM_URL}"
echo "  EMBED:          ${EMBED_URL:-<disabled>}  (RAG 检索，可选)"
echo "  ForcedAligner:  ${FORCED_ALIGNER_MODEL_PATH}"
echo "  Callback URL:   ${GATEWAY_CALLBACK_URL}"
echo "  ContentSafety:  ${ENABLE_CONTENT_SAFETY} (0=关闭, 1=开启)"
echo "  GPU:            CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-} HIP_VISIBLE_DEVICES=${HIP_VISIBLE_DEVICES:-}"

if [[ "$RUN_LOCAL_OFFLINE_WORKER" == "1" ]]; then
    require_env QWEN3_OFFLINE_MODEL_PATH
    echo "=== 启动本机离线 Worker ==="
    start_logged_bg "$WORKER_LOG" \
        env WORKER_LOG_FILE="$WORKER_LOG" \
            OFFLINE_WORKER_PORT="$OFFLINE_WORKER_PORT" \
            python offline_worker.py --port "$OFFLINE_WORKER_PORT"
    WORKER_PID=$!

    if ! wait_for_port "$OFFLINE_WORKER_PORT" "离线 Worker" "$WORKER_PID" 180; then
        stop_all
        exit 1
    fi
else
    echo "=== 跳过本机离线 Worker（RUN_LOCAL_OFFLINE_WORKER=${RUN_LOCAL_OFFLINE_WORKER}）==="
fi

echo "=== 启动 Gateway ==="
start_logged_bg "$GATEWAY_LOG" \
    env GATEWAY_LOG_FILE="$GATEWAY_LOG" \
        GATEWAY_URL="$GATEWAY_URL" \
        GATEWAY_PUBLIC_URL="$GATEWAY_PUBLIC_URL" \
        GATEWAY_CALLBACK_URL="$GATEWAY_CALLBACK_URL" \
        SCNET_OAUTH_CLIENT_ID="$SCNET_OAUTH_CLIENT_ID" \
        SCNET_OAUTH_CLIENT_SECRET="$SCNET_OAUTH_CLIENT_SECRET" \
        SCNET_OAUTH_REDIRECT_URI="$SCNET_OAUTH_REDIRECT_URI" \
        ADMIN_USERNAMES="${ADMIN_USERNAMES:-}" \
        FORCED_ALIGNER_MODEL_PATH="$FORCED_ALIGNER_MODEL_PATH" \
        EMBED_URL="$EMBED_URL" \
        EMBED_MODEL="$EMBED_MODEL" \
        SMTP_HOST="${SMTP_HOST:-}" \
        SMTP_PORT="${SMTP_PORT:-}" \
        SMTP_USER="${SMTP_USER:-}" \
        SMTP_PASSWORD="${SMTP_PASSWORD:-}" \
        SMTP_FROM="${SMTP_FROM:-}" \
        SMTP_SSL="${SMTP_SSL:-}" \
        SMTP_TLS="${SMTP_TLS:-}" \
        SMTP_HTTP_PROXY="${SMTP_HTTP_PROXY:-${https_proxy:-${http_proxy:-}}}" \
        ${http_proxy:+http_proxy="$http_proxy"} \
        ${https_proxy:+https_proxy="$https_proxy"} \
        ${HTTP_PROXY:+HTTP_PROXY="$HTTP_PROXY"} \
        ${HTTPS_PROXY:+HTTPS_PROXY="$HTTPS_PROXY"} \
        ${YTDLP_PROXY:+YTDLP_PROXY="$YTDLP_PROXY"} \
        ${YTDLP_NODE_PATH:+YTDLP_NODE_PATH="$YTDLP_NODE_PATH"} \
        ${YOUTUBE_DOWNLOAD_API_URL:+YOUTUBE_DOWNLOAD_API_URL="$YOUTUBE_DOWNLOAD_API_URL"} \
        ${YOUTUBE_DOWNLOAD_API_TOKEN:+YOUTUBE_DOWNLOAD_API_TOKEN="$YOUTUBE_DOWNLOAD_API_TOKEN"} \
        ${YOUTUBE_DOWNLOAD_API_TIMEOUT:+YOUTUBE_DOWNLOAD_API_TIMEOUT="$YOUTUBE_DOWNLOAD_API_TIMEOUT"} \
        ${YOUTUBE_DOWNLOAD_API_PROXY:+YOUTUBE_DOWNLOAD_API_PROXY="$YOUTUBE_DOWNLOAD_API_PROXY"} \
        AUTH_EMAIL_DEV_MODE="${AUTH_EMAIL_DEV_MODE:-}" \
        python gateway_server.py --port "$GATEWAY_PORT"
GATEWAY_PID=$!

if ! wait_for_port "$GATEWAY_PORT" "Gateway" "$GATEWAY_PID" 60; then
    stop_all
    exit 1
fi

echo ""
if [[ "$RUN_LOCAL_OFFLINE_WORKER" == "1" ]]; then
    echo "✅ Gateway 和离线 Worker 已就绪"
    echo "日志: 屏幕 + ${GATEWAY_LOG} / ${WORKER_LOG} (仅文件: LOG_TO_CONSOLE=0)"
else
    echo "✅ Gateway 已就绪"
    echo "日志: 屏幕 + ${GATEWAY_LOG} (仅文件: LOG_TO_CONSOLE=0)"
fi
echo "按 Ctrl+C 停止"

if wait "$GATEWAY_PID"; then
    stop_worker
else
    exit_code=$?
    stop_worker
    exit "$exit_code"
fi
