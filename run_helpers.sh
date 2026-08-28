#!/bin/bash
# Shared helpers for service startup scripts.
# LOG_TO_CONSOLE=1 (default): tee to screen + log file
# LOG_TO_CONSOLE=0: log file only

# 出口代理由 .env.local 决定。超算节点 profile 里常残留 SMTP 代理，
# 若让 shell 变量覆盖，OAuth 会走错代理。需要沿用 shell 代理时设 KEEP_SHELL_PROXY=1。
_PROXY_KEYS='http_proxy https_proxy ftp_proxy HTTP_PROXY HTTPS_PROXY FTP_PROXY no_proxy NO_PROXY'

load_env_file() {
    local file=$1
    if [ ! -f "$file" ]; then
        return 0
    fi
    echo "[Config] 加载环境文件: $file"

    # 启动前已 export 的非空变量优先（LLM_URL 等）；代理变量默认不保留。
    local -A preserved=()
    local line key
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ "$line" =~ ^[[:space:]]*$ ]] && continue
        if [[ "$line" =~ ^[[:space:]]*export[[:space:]]+ ]]; then
            line="${line#*export }"
            line="${line#"${line%%[![:space:]]*}"}"
        fi
        key="${line%%=*}"
        key="${key//[[:space:]]/}"
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        if [ "${KEEP_SHELL_PROXY:-0}" != "1" ]; then
            case " ${_PROXY_KEYS} " in
                *" ${key} "*) continue ;;
            esac
        fi
        if [ -n "${!key:-}" ]; then
            preserved["$key"]="${!key}"
        fi
    done < "$file"

    set -a
    # shellcheck disable=SC1090
    source "$file"
    set +a

    for key in "${!preserved[@]}"; do
        export "${key}=${preserved[$key]}"
    done
}

load_local_env() {
    local root
    root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    load_env_file "$root/smtp.env"
    load_env_file "$root/.env.local"
    if [ -n "${http_proxy:-}" ]; then
        export HTTP_PROXY="${HTTP_PROXY:-$http_proxy}"
    fi
    if [ -n "${https_proxy:-}" ]; then
        export HTTPS_PROXY="${HTTPS_PROXY:-$https_proxy}"
    fi
    if [ -n "${ftp_proxy:-}" ]; then
        export FTP_PROXY="${FTP_PROXY:-$ftp_proxy}"
    fi
}

require_env() {
    local name=$1
    if [ -z "${!name:-}" ]; then
        echo "错误: 请设置 ${name}（写入 .env.local 或导出环境变量，参见 .env.example）" >&2
        exit 1
    fi
}

start_logged_bg() {
    local log_file="$1"
    shift
    mkdir -p "$(dirname "$log_file")"
    export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
    if [[ "${LOG_TO_CONSOLE:-1}" == "1" ]]; then
        "$@" > >(tee -a "$log_file") 2>&1 &
    else
        "$@" >> "$log_file" 2>&1 &
    fi
}

run_logged() {
    local log_file="$1"
    shift
    mkdir -p "$(dirname "$log_file")"
    export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
    if [[ "${LOG_TO_CONSOLE:-1}" == "1" ]]; then
        "$@" > >(tee -a "$log_file") 2>&1
    else
        "$@" >> "$log_file" 2>&1
    fi
}
