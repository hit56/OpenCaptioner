import aiohttp
import uvicorn
from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException, Form, Request, Query
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import asyncio
import io
import json
import requests
import os
import shutil
import datetime
import tempfile
import re
import sys
import time
import uuid
import zipfile
from urllib.parse import urlencode, urlparse
import utils
from subtitles import configure, run_video_subtitle_pipeline
from subtitles.burn_in import (
    auto_generate_subtitle_video_sync,
    export_subtitle_content_for_session,
    export_subtitle_vtt_for_session,
    export_subtitle_vtt_for_draft,
    apply_subtitle_edits,
    build_cue_list_for_session,
    export_cues_vtt_draft,
    rebuild_data_with_cues,
)
from scnet_auth import (
    build_scnet_authorize_url,
    exchange_scnet_code_for_tokens,
    fetch_scnet_userinfo,
    resolve_public_url_from_request,
    resolve_scnet_redirect_uri,
    SCNET_OAUTH_CLIENT_ID,
)
from local_auth import (
    init_db as init_local_auth_db,
    validate_session as validate_local_session,
)
from admin import is_admin_user
import upload_task_store
import bilibili_downloader
import bilibili_uploader
import douyin_downloader
import youtube_downloader
from subtitles import rag


def create_http_session(**kwargs) -> aiohttp.ClientSession:
    # aiohttp 3.12 defaults trust_env=False; honor HTTP(S)_PROXY / NO_PROXY explicitly.
    kwargs.setdefault("trust_env", True)
    return aiohttp.ClientSession(**kwargs)

# ==========================================
# 路径与日志初始化
# ==========================================
sys.path.append("./src")
try:
    from logger import Logger
    os.makedirs('./logs', exist_ok=True)
    gateway_log_file = os.environ.get("GATEWAY_LOG_FILE", "./logs/gateway.log")
    server_log = Logger(gateway_log_file, level='info', backCount=30)
    click_log_file = os.environ.get("CLICK_LOG_FILE", "./logs/click.log")
    click_log = Logger(click_log_file, level='info', backCount=90)
    print("[Init] Gateway Logger initialized successfully.")
except ImportError as e:
    print(f"[Error] 无法导入 Logger 模块: {e}")
    sys.exit(1)


def record_client_click(
    *,
    action: str,
    client_user_id: str | None = None,
    client_time: str | None = None,
    label: str | None = None,
    tab: str | None = None,
    task_id: str | None = None,
    file_name: str | None = None,
    path: str | None = None,
    meta: dict | None = None,
    source: str = "frontend",
) -> None:
    """记录用户点击/交互日志，user_tag 与 uploads 文件名中的匿名 uid 段一致。"""
    if not click_log:
        return
    entry = {
        "server_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "client_time": client_time,
        "user_tag": client_user_id_tag(client_user_id),
        "client_user_id": _normalize_client_user_id(client_user_id),
        "action": action,
        "label": label,
        "tab": tab,
        "task_id": task_id,
        "file_name": file_name,
        "path": path,
        "source": source,
        "meta": meta or {},
    }
    click_log.logger.info(json.dumps(entry, ensure_ascii=False))

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# 全局配置 (挂载与目录需与 Worker 一致)
# ==========================================
SAVE_ROOT = "saved_data"
UPLOAD_DIR = os.path.join(SAVE_ROOT, "uploads")
SEGMENT_DIR = os.path.join(SAVE_ROOT, "segments") 
CACHE_DIR = os.path.join(SAVE_ROOT, "cache")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(SEGMENT_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

app.mount("/files", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/segments", StaticFiles(directory=SEGMENT_DIR), name="segments")
app.mount("/cache", StaticFiles(directory=CACHE_DIR), name="cache")

AVATAR_DIR = os.path.join(os.getcwd(), "avatars")
if os.path.exists(AVATAR_DIR):
    app.mount("/avatars", StaticFiles(directory=AVATAR_DIR), name="avatars")

FRONTEND_DIST_DIR = os.path.join(os.getcwd(), "frontend", "dist")
FRONTEND_INDEX_PATH = os.path.join(FRONTEND_DIST_DIR, "index.html")
if os.path.exists(os.path.join(FRONTEND_DIST_DIR, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIST_DIR, "assets")), name="frontend-assets")

WORKER_URLS = utils.parse_worker_service_urls(
    os.environ.get("WORKER_URL", "").strip()
    or f"http://127.0.0.1:{os.environ.get('OFFLINE_WORKER_PORT', '7001')}"
)
OFFLINE_WORKER_STATUS_TIMEOUT_S = float(os.environ.get("OFFLINE_WORKER_STATUS_TIMEOUT_S", "0.8"))
_worker_idx = 0
_worker_lock = asyncio.Lock()

PUNCT_URLS = utils.parse_punct_service_urls(
    os.environ.get("PUNCT_URL", "").strip()
    or f"http://127.0.0.1:{os.environ.get('PUNCT_PORT', '8080')}/v1/chat/completions"
)
LLM_URLS = utils.parse_llm_service_urls()
EMBED_URLS = utils.parse_embed_service_urls()
_punct_router = utils.ServiceURLRouter(PUNCT_URLS)
_llm_router = utils.ServiceURLRouter(LLM_URLS)
# embedding 服务可选：未配置 EMBED_URL 时路由为 None，RAG 问答降级为塞全文
_embed_router = utils.ServiceURLRouter(EMBED_URLS) if EMBED_URLS else None
GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "7860"))
GATEWAY_PUBLIC_URL = (
    os.environ.get("GATEWAY_PUBLIC_URL", "").strip()
    or os.environ.get("GATEWAY_URL", "").strip()
    or f"http://127.0.0.1:{GATEWAY_PORT}"
).rstrip("/")
GATEWAY_CALLBACK_URL = (
    os.environ.get("GATEWAY_CALLBACK_URL", "").strip()
    or GATEWAY_PUBLIC_URL
).rstrip("/")
WORKER_EVENT_MODE = os.environ.get("WORKER_EVENT_MODE", "pull").strip().lower()
WORKER_EVENT_POLL_INTERVAL_S = max(float(os.environ.get("WORKER_EVENT_POLL_INTERVAL_S", "0.5")), 0.1)
WORKER_EVENT_POLL_MAX_IDLE_S = max(float(os.environ.get("WORKER_EVENT_POLL_MAX_IDLE_S", "1800")), 30.0)
WORKER_EVENT_POLL_TIMEOUT_S = max(float(os.environ.get("WORKER_EVENT_POLL_TIMEOUT_S", "60")), 5.0)
WORKER_EVENT_POLL_MAX_RETRIES = max(int(os.environ.get("WORKER_EVENT_POLL_MAX_RETRIES", "5")), 1)
_subtitle_finalize_sessions: set[str] = set()
_subtitle_finalize_lock = asyncio.Lock()
INTERNAL_WORKER_TOKEN = os.environ.get("INTERNAL_WORKER_TOKEN", "").strip()
ENABLE_CONTENT_SAFETY = os.environ.get("ENABLE_CONTENT_SAFETY", "0").strip().lower() not in {
    "0",
    "false",
    "off",
    "no",
}

_blocked_content_sessions: set[str] = set()
_blocked_content_sessions_lock = asyncio.Lock()


async def get_next_worker_url(route_source: str = "unknown", session_id: str = ""):
    global _worker_idx
    async with _worker_lock:
        selected_idx = _worker_idx % len(WORKER_URLS)
        route_seq = _worker_idx + 1
        url = WORKER_URLS[selected_idx]
        _worker_idx += 1
        session_prefix = f"[{session_id}] " if session_id else ""
        server_log.logger.info(
            f"{session_prefix}[Worker-RR] source={route_source} seq={route_seq} idx={selected_idx} selected={url}"
        )
        return url


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def get_best_offline_worker_url(route_source: str = "unknown", session_id: str = ""):
    if len(WORKER_URLS) <= 1:
        return await get_next_worker_url(route_source=f"{route_source}.single", session_id=session_id)

    session_prefix = f"[{session_id}] " if session_id else ""
    timeout = aiohttp.ClientTimeout(total=OFFLINE_WORKER_STATUS_TIMEOUT_S)

    async def fetch_worker_status(session: aiohttp.ClientSession, idx: int, url: str):
        try:
            async with session.get(f"{url}/worker_status", timeout=timeout) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except Exception as e:
            server_log.logger.warning(
                f"{session_prefix}[Worker-Load] probe_failed idx={idx} url={url} err={e}"
            )
            return None

        active_tasks = max(_safe_int(data.get("active_tasks"), 0), 0)
        queue_size = max(_safe_int(data.get("queue_size"), 0), 0)
        pending_tasks = max(_safe_int(data.get("pending_tasks"), active_tasks + queue_size), active_tasks + queue_size)
        available_slots = max(_safe_int(data.get("available_slots"), 0), 0)
        return {
            "idx": idx,
            "url": url,
            "active_tasks": active_tasks,
            "queue_size": queue_size,
            "pending_tasks": pending_tasks,
            "available_slots": available_slots,
        }

    async with create_http_session() as session:
        probe_results = await asyncio.gather(
            *(fetch_worker_status(session, idx, url) for idx, url in enumerate(WORKER_URLS))
        )

    candidates = [item for item in probe_results if item]
    if not candidates:
        return await get_next_worker_url(route_source=f"{route_source}.fallback_rr", session_id=session_id)

    global _worker_idx
    async with _worker_lock:
        start_idx = _worker_idx % len(WORKER_URLS)
        route_seq = _worker_idx + 1
        best_pending = min(item["pending_tasks"] for item in candidates)
        best_available = max(
            item["available_slots"] for item in candidates
            if item["pending_tasks"] == best_pending
        )
        best_candidates = [
            item for item in candidates
            if item["pending_tasks"] == best_pending and item["available_slots"] == best_available
        ]
        best_candidates.sort(key=lambda item: (item["idx"] - start_idx) % len(WORKER_URLS))
        selected = best_candidates[0]
        _worker_idx += 1

    server_log.logger.info(
        f"{session_prefix}[Worker-LB] source={route_source} seq={route_seq} idx={selected['idx']} "
        f"selected={selected['url']} pending={selected['pending_tasks']} active={selected['active_tasks']} "
        f"queue={selected['queue_size']} available={selected['available_slots']}"
    )
    return selected["url"]

def get_next_punct_url(route_source: str = "unknown", session_id: str = ""):
    selection = _punct_router.select(routing_key=session_id or None)
    if len(PUNCT_URLS) > 1:
        session_prefix = f"[{session_id}] " if session_id else ""
        seq_text = f" seq={selection['seq']}" if selection["seq"] is not None else ""
        server_log.logger.info(
            f"{session_prefix}[Punct-LB] source={route_source} mode={selection['mode']} "
            f"idx={selection['idx']} selected={selection['url']}{seq_text}"
        )
    return selection["url"]


def get_next_llm_url(route_source: str = "unknown", session_id: str = ""):
    selection = _llm_router.select(routing_key=session_id or None)
    if len(LLM_URLS) > 1:
        session_prefix = f"[{session_id}] " if session_id else ""
        seq_text = f" seq={selection['seq']}" if selection["seq"] is not None else ""
        server_log.logger.info(
            f"{session_prefix}[LLM-LB] source={route_source} mode={selection['mode']} "
            f"idx={selection['idx']} selected={selection['url']}{seq_text}"
        )
    return selection["url"]

def get_next_embed_url(route_source: str = "unknown", session_id: str = ""):
    if _embed_router is None:
        raise RuntimeError("EMBED_URL 未配置，embedding 服务不可用")
    selection = _embed_router.select(routing_key=session_id or None)
    if len(EMBED_URLS) > 1:
        session_prefix = f"[{session_id}] " if session_id else ""
        seq_text = f" seq={selection['seq']}" if selection["seq"] is not None else ""
        server_log.logger.info(
            f"{session_prefix}[EMBED-LB] source={route_source} mode={selection['mode']} "
            f"idx={selection['idx']} selected={selection['url']}{seq_text}"
        )
    return selection["url"]

@app.on_event("startup")
async def startup_event():
    configure(
        logger=server_log.logger,
        get_llm_url=get_next_llm_url,
        get_embed_url=get_next_embed_url,
    )
    utils.init_punctuation_pipeline(server_log.logger)
    server_log.logger.info(
        f"[Config] ENABLE_CONTENT_SAFETY={'1' if ENABLE_CONTENT_SAFETY else '0'}"
    )
    server_log.logger.info(
        f"[Config] GATEWAY_PUBLIC_URL={GATEWAY_PUBLIC_URL}"
    )
    server_log.logger.info(
        f"[Config] GATEWAY_CALLBACK_URL={GATEWAY_CALLBACK_URL}"
    )

# ==========================================
# 任务管理器 (SSE 推送)
# ==========================================
SESSION_ID_RE = re.compile(r"(\d{14}_[a-f0-9]{8}|\d{8}_\d{6}_[a-f0-9]{8})")
# 上传/离线任务 session_id -> 登录用户 owner id（与 upload_tasks.user_id 一致）
session_owners: dict[str, str] = {}
CLIENT_OWNERS_FILE = os.path.join(SAVE_ROOT, "client_owners.json")
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".flv", ".webm", ".wmv", ".3gp", ".m4v"}
MEDIA_TYPE_BY_EXT = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".flv": "video/x-flv",
    ".webm": "video/webm",
    ".wmv": "video/x-ms-wmv",
    ".3gp": "video/3gpp",
    ".m4v": "video/x-m4v",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
}


def load_session_owners():
    if not os.path.exists(CLIENT_OWNERS_FILE):
        return
    try:
        with open(CLIENT_OWNERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            session_owners.update({str(k): str(v) for k, v in data.items()})
    except Exception as e:
        server_log.logger.warning(f"加载 client_owners 失败: {e}")


def persist_session_owners():
    try:
        with open(CLIENT_OWNERS_FILE, "w", encoding="utf-8") as f:
            json.dump(session_owners, f, ensure_ascii=False)
    except Exception as e:
        server_log.logger.warning(f"保存 client_owners 失败: {e}")


load_session_owners()


def _normalize_client_user_id(client_user_id: str | None) -> str | None:
    if not client_user_id:
        return None
    value = str(client_user_id).strip()
    return value or None


def client_user_id_tag(client_user_id: str | None) -> str:
    """匿名用户 ID 的前 8 位十六进制，用于 uploads 文件名。"""
    normalized = _normalize_client_user_id(client_user_id)
    if normalized and normalized != "anonymous":
        hex_only = re.sub(r"[^a-f0-9]", "", normalized.lower())
        if len(hex_only) >= 8:
            return hex_only[:8]
    return str(uuid.uuid4())[:8]


def new_session_id(created_at_dt: datetime.datetime | None = None) -> str:
    """YYYYMMDDHHMMSS_{uuid8}"""
    dt = created_at_dt or datetime.datetime.now(datetime.timezone.utc)
    return f"{dt.strftime('%Y%m%d%H%M%S')}_{str(uuid.uuid4())[:8]}"


def split_session_id_parts(session_id: str) -> tuple[str, str] | None:
    """解析 session_id，返回 (YYYYMMDDHHMMSS, uuid8)；非网关格式（如前端占位 UUID）返回 None。"""
    matched = SESSION_ID_RE.fullmatch(session_id)
    if not matched:
        return None
    core = matched.group(1)
    parts = core.rsplit("_", 1)
    if len(parts) != 2:
        return None
    ts_raw, uuid8 = parts
    if len(ts_raw) == 15 and ts_raw[8] == "_":
        ts_tag = f"{ts_raw[:8]}{ts_raw[9:]}"
    else:
        ts_tag = ts_raw
    return ts_tag, uuid8


def build_upload_stored_basename(session_id: str, client_user_id: str | None, original_filename: str) -> str:
    """YYYYMMDDHHMMSS_{user8}_{session_uuid8}_{原文件名}"""
    parts = split_session_id_parts(session_id)
    if not parts:
        raise ValueError(f"Invalid session_id: {session_id}")
    ts_tag, uuid8 = parts
    user_tag = client_user_id_tag(client_user_id)
    return f"{ts_tag}_{user_tag}_{uuid8}_{os.path.basename(original_filename)}"


def upload_file_matches_session(name: str, session_id: str) -> bool:
    if name.startswith(f"{session_id}_"):
        return True
    parts = split_session_id_parts(session_id)
    if not parts:
        return False
    ts_tag, uuid8 = parts
    return name.startswith(f"{ts_tag}_") and f"_{uuid8}_" in name


def collect_upload_paths_for_session(session_id: str) -> list[tuple[str, str]]:
    upload_candidates: list[tuple[str, str]] = []
    if not os.path.isdir(UPLOAD_DIR):
        return upload_candidates
    for name in os.listdir(UPLOAD_DIR):
        if not upload_file_matches_session(name, session_id):
            continue
        ext = os.path.splitext(name)[1].lower()
        upload_candidates.append((os.path.join(UPLOAD_DIR, name), ext))
    return upload_candidates


STORED_MEDIA_SESSION_RE = re.compile(r"^(\d{14})_[a-f0-9]{8}_([a-f0-9]{8})(?:_|\.wav$)")


def extract_session_id_from_media_path(path: str) -> str | None:
    if not path:
        return None
    if path.startswith("/segments/"):
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and SESSION_ID_RE.fullmatch(parts[1] or ""):
            return parts[1]
    basename = os.path.basename(path.split("?", 1)[0])
    stored_match = STORED_MEDIA_SESSION_RE.match(basename)
    if stored_match:
        return f"{stored_match.group(1)}_{stored_match.group(2)}"
    match = SESSION_ID_RE.match(basename)
    return match.group(1) if match else None


class TaskManager:
    def __init__(self):
        self.tasks = {}
        self.lock = asyncio.Lock()

    async def create_task(self, task_id, client_user_id: str | None = None):
        owner = _normalize_client_user_id(client_user_id)
        async with self.lock:
            self.tasks[task_id] = {"history": [], "queues": set(), "client_user_id": owner}
            if owner:
                session_owners[task_id] = owner
                persist_session_owners()

    async def get_owner(self, task_id: str) -> str | None:
        async with self.lock:
            task = self.tasks.get(task_id)
            if task:
                owner = task.get("client_user_id") or session_owners.get(task_id)
                if owner:
                    return owner
        owner = session_owners.get(task_id)
        if owner:
            return owner
        try:
            return upload_task_store.get_task_owner(UPLOAD_TASKS_DB_PATH, task_id)
        except Exception:
            return None

    async def verify_access(self, task_id: str, client_user_id: str | None) -> bool:
        requester = _normalize_client_user_id(client_user_id)
        owner = await self.get_owner(task_id)
        if not owner:
            return True
        return bool(requester) and requester == owner

    async def broadcast(self, task_id, message_dict):
        msg_str = json.dumps(message_dict, ensure_ascii=False)
        async with self.lock:
            if task_id in self.tasks:
                self.tasks[task_id]["history"].append(msg_str)
                for q in self.tasks[task_id]["queues"]:
                    await q.put(msg_str)

    async def subscribe(self, task_id):
        async with self.lock:
            if task_id not in self.tasks: return None, None
            q = asyncio.Queue()
            self.tasks[task_id]["queues"].add(q)
            return list(self.tasks[task_id]["history"]), q

    async def unsubscribe(self, task_id, q):
        async with self.lock:
            if task_id in self.tasks and q in self.tasks[task_id]["queues"]:
                self.tasks[task_id]["queues"].remove(q)

    async def get_status(self, task_id):
        async with self.lock:
            if task_id not in self.tasks:
                return {"exists": False, "last_event": None, "is_terminal": False}

            history = self.tasks[task_id]["history"]
            last_event = None
            if history:
                try:
                    last_event = json.loads(history[-1]).get("type")
                except Exception:
                    last_event = None

            return {
                "exists": True,
                "last_event": last_event,
                "is_terminal": last_event in ["done", "error"],
            }

task_manager = TaskManager()


@app.middleware("http")
async def protect_user_media(request: Request, call_next):
    path = request.url.path
    if path.startswith("/media/"):
        session_id = path.split("/media/", 1)[-1].split("/", 1)[0]
        if session_id:
            client_user_id = _normalize_client_user_id(
                request.headers.get("X-Client-User-Id")
                or request.query_params.get("client_user_id")
            )
            owner = await task_manager.get_owner(session_id)
            if owner and (not client_user_id or client_user_id != owner):
                return Response(status_code=403, content="Forbidden")
        return await call_next(request)
    if path.startswith("/cache/") or path.startswith("/files/") or path.startswith("/segments/"):
        worker_token = request.headers.get("X-Internal-Worker-Token", "")
        if INTERNAL_WORKER_TOKEN and worker_token == INTERNAL_WORKER_TOKEN:
            return await call_next(request)
        session_id = extract_session_id_from_media_path(path)
        if session_id:
            client_user_id = _normalize_client_user_id(
                request.headers.get("X-Client-User-Id")
                or request.query_params.get("client_user_id")
            )
            owner = await task_manager.get_owner(session_id)
            if owner and (not client_user_id or client_user_id != owner):
                return Response(status_code=403, content="Forbidden")
    return await call_next(request)

# ==========================================
# 辅助函数 (网关轻量级逻辑)
# ==========================================
def parse_llm_json(text):
    import json, re
    try: return json.loads(text)
    except: pass
    match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
    if match:
        try: return json.loads(match.group(1))
        except: pass
    match = re.search(r'\[\s*\{.*?\}\s*\]', text, re.DOTALL)
    if match:
        try: return json.loads(match.group(0))
        except: pass
    return None

def get_content_safety_result_sync(text: str, route_source: str = "gateway.content_safety", session_id: str = "") -> dict:
    if not ENABLE_CONTENT_SAFETY:
        return {
            "is_safe": True,
            "matched": {},
            "violations": [],
            "summary": "",
            "keyword_triggered": False,
            "review_performed": False,
            "review_is_safe": True,
            "review_error": "",
            "final_reason": "disabled_by_config",
        }
    if not text or not text.strip():
        return {
            "is_safe": True,
            "matched": {},
            "violations": [],
            "summary": "",
            "keyword_triggered": False,
            "review_performed": False,
            "review_is_safe": True,
            "review_error": "",
            "final_reason": "empty_text",
        }
    return utils.check_content_safety(
        text,
        logger=server_log.logger,
        source=route_source,
        review_url_getter=lambda: get_next_llm_url(
            route_source=f"{route_source}.review",
            session_id=session_id,
        ),
    )


def check_content_safety_sync(text: str, route_source: str = "gateway.content_safety", session_id: str = "") -> bool:
    return get_content_safety_result_sync(text, route_source=route_source, session_id=session_id).get("is_safe", True)

async def check_content_safety_async(text: str, route_source: str = "gateway.content_safety", session_id: str = "") -> bool:
    return await asyncio.to_thread(check_content_safety_sync, text, route_source, session_id)


async def _mark_session_content_blocked(session_id: str):
    async with _blocked_content_sessions_lock:
        _blocked_content_sessions.add(session_id)


async def _is_session_content_blocked(session_id: str) -> bool:
    async with _blocked_content_sessions_lock:
        return session_id in _blocked_content_sessions


async def _clear_session_content_blocked(session_id: str):
    async with _blocked_content_sessions_lock:
        _blocked_content_sessions.discard(session_id)


def _log_offline_content_violation(
    session_id: str,
    safety_result: dict,
    trigger_text: str,
    original_filename: str = "",
):
    keyword_summary = safety_result.get("summary") or json.dumps(
        safety_result.get("matched", {}),
        ensure_ascii=False,
    )
    filename_part = f" filename={original_filename}" if original_filename else ""
    server_log.logger.warning(
        f"[{session_id}] [ContentSafety] 离线识别内容未通过审核，已静默终止任务。"
        f"{filename_part} 命中关键词: {keyword_summary}。"
        f" LLM二次校验结论: 不通过。触发文本: {trigger_text}"
    )


def _collect_offline_event_texts(event_type: str, data: dict) -> list[str]:
    texts = []
    if event_type == "segment_final":
        text = (data.get("text") or "").strip()
        if text:
            texts.append(text)
        return texts

    if event_type == "done":
        final_segments = data.get("final_segments")
        if isinstance(final_segments, list):
            combined = " ".join(
                (seg.get("text") or "").strip()
                for seg in final_segments
                if isinstance(seg, dict) and (seg.get("text") or "").strip()
            )
            if combined:
                texts.append(combined)
    return texts


async def maybe_block_offline_worker_event(session_id: str, event_type: str, data: dict) -> bool:
    if not ENABLE_CONTENT_SAFETY:
        return False

    for text in _collect_offline_event_texts(event_type, data):
        safety_result = await asyncio.to_thread(
            get_content_safety_result_sync,
            text,
            "gateway.offline_content_safety",
            session_id,
        )
        if safety_result.get("is_safe", True):
            continue
        _log_offline_content_violation(
            session_id,
            safety_result,
            text,
            original_filename=(data.get("original_filename") or ""),
        )
        await _mark_session_content_blocked(session_id)
        return True
    return False


def _load_final_results_list(session_id: str) -> list:
    result_path = os.path.join(SEGMENT_DIR, session_id, "final_result.json")
    if not os.path.exists(result_path):
        return []
    with open(result_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def _save_final_results_list(session_id: str, final_results_list: list):
    result_path = os.path.join(SEGMENT_DIR, session_id, "final_result.json")
    os.makedirs(os.path.dirname(result_path), exist_ok=True)
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(final_results_list, f, ensure_ascii=False, indent=2)


_offline_session_context: dict[str, dict] = {}


def _init_offline_session_context(session_id: str, ui_language: str):
    from subtitles.i18n import normalize_ui_language

    _offline_session_context[session_id] = {
        "ui_language": normalize_ui_language(ui_language),
        "session_lang": "unknown",
        "translations": {},
    }


def _clear_offline_session_context(session_id: str):
    _offline_session_context.pop(session_id, None)


def _merge_pending_segment_translations(session_id: str, final_results_list: list) -> bool:
    ctx = _offline_session_context.get(session_id)
    if not ctx:
        return False
    pending = ctx.get("translations") or {}
    if not pending:
        return False
    changed = False
    for item in final_results_list:
        idx = item.get("index")
        if idx in pending and not (item.get("translation") or "").strip():
            item["translation"] = pending[idx]
            changed = True
    return changed


async def _translate_segment_final_on_gateway(session_id: str, segment_data: dict) -> dict:
    from subtitles.segment_translation import attach_segment_translation

    ctx = _offline_session_context.setdefault(session_id, {
        "ui_language": "zh-CN",
        "session_lang": "unknown",
        "translations": {},
    })
    session_lang = ctx.get("session_lang", "unknown")
    ui_language = ctx.get("ui_language", "zh-CN")
    item = dict(segment_data)
    loop = asyncio.get_running_loop()
    try:
        translation = await loop.run_in_executor(
            None,
            attach_segment_translation,
            item,
            session_lang,
            ui_language,
        )
    except Exception as e:
        server_log.logger.error(f"[{session_id}] segment_final translation failed: {e}")
        return segment_data

    if translation:
        ctx.setdefault("translations", {})[item.get("index")] = translation
        item["translation"] = translation
    return item


def _segment_translations_payload(final_results_list: list) -> list[dict]:
    payload = []
    for item in final_results_list or []:
        text = (item.get("text") or "").strip()
        translation = (item.get("translation") or "").strip()
        if translation and translation != text:
            payload.append({
                "index": item.get("index"),
                "translation": translation,
            })
    return payload


def _final_segments_payload(final_results_list: list) -> list[dict]:
    payload = []
    for item in final_results_list or []:
        if not isinstance(item, dict):
            continue
        text = (item.get("text") or "").strip()
        if not text:
            continue
        translation = (item.get("translation") or "").strip()
        segment = {
            "index": item.get("index"),
            "timestamp": item.get("timestamp") or "",
            "text": text,
        }
        if item.get("speaker") is not None:
            segment["speaker"] = item.get("speaker")
        if item.get("segment_url"):
            segment["segment_url"] = item.get("segment_url")
        if translation and translation != text:
            segment["translation"] = translation
        payload.append(segment)
    return payload


async def _ensure_and_broadcast_segment_translations(
    session_id: str,
    session_lang: str,
    ui_language: str,
    *,
    announce_progress: bool = True,
) -> list[dict]:
    from subtitles.segment_translation import (
        needs_segment_translation,
        translate_results_list,
    )

    final_results_list = _load_final_results_list(session_id)
    if not final_results_list:
        return []

    if _merge_pending_segment_translations(session_id, final_results_list):
        _save_final_results_list(session_id, final_results_list)

    if not needs_segment_translation(final_results_list, session_lang, ui_language):
        return _segment_translations_payload(final_results_list)

    if announce_progress:
        await task_manager.broadcast(session_id, {
            "type": "progress",
            "message": "正在翻译识别结果...",
        })

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None,
            translate_results_list,
            final_results_list,
            session_lang,
            ui_language,
        )
    except Exception as e:
        server_log.logger.error(f"[{session_id}] Segment translation failed: {e}")
        return _segment_translations_payload(final_results_list)

    _save_final_results_list(session_id, final_results_list)

    translations = _segment_translations_payload(final_results_list)
    for item in translations:
        await task_manager.broadcast(session_id, {
            "type": "segment_translation",
            "index": item["index"],
            "translation": item["translation"],
        })
    return translations


async def dispatch_worker_event(session_id: str, event_type: str, data: dict) -> bool:
    """Forward worker events to clients. Returns True if the event stream should stop."""
    if await _is_session_content_blocked(session_id):
        is_terminal = event_type in {"done", "error", "asr_done"}
        if is_terminal:
            await _clear_session_content_blocked(session_id)
        return is_terminal

    if await maybe_block_offline_worker_event(session_id, event_type, data):
        return True

    if event_type == "speaker_stats":
        detected_lang = data.get("detected_lang")
        if detected_lang:
            _offline_session_context.setdefault(session_id, {
                "ui_language": "zh-CN",
                "session_lang": "unknown",
                "translations": {},
            })["session_lang"] = str(detected_lang)
        speakers = data.get("speakers")
        _update_upload_task_row(
            session_id,
            status="processing",
            detected_lang=str(detected_lang) if detected_lang else None,
            detected_lang_name=str(data.get("detected_lang_name") or "") or None,
            speaker_stats=speakers if isinstance(speakers, list) else None,
        )

    if event_type == "segment_final":
        enriched = await _translate_segment_final_on_gateway(session_id, data)
        await task_manager.broadcast(session_id, {"type": "segment_final", **enriched})
        translation = (enriched.get("translation") or "").strip()
        if translation:
            await task_manager.broadcast(session_id, {
                "type": "segment_translation",
                "index": enriched.get("index"),
                "translation": translation,
            })
        return False

    if event_type == "asr_done":
        _update_upload_task_row(
            session_id,
            status="processing",
            message=str(data.get("message") or "asr_done"),
        )
        await _handle_asr_done(session_id, data)
        return True

    if event_type == "done":
        session_lang = data.get("session_lang", "zh")
        ui_language = data.get("ui_language", "zh-CN")
        ctx = _offline_session_context.get(session_id)
        if ctx:
            if data.get("session_lang"):
                ctx["session_lang"] = data.get("session_lang")
            if data.get("ui_language"):
                from subtitles.i18n import normalize_ui_language
                ctx["ui_language"] = normalize_ui_language(data.get("ui_language"))
        segment_translations = await _ensure_and_broadcast_segment_translations(
            session_id,
            session_lang,
            ui_language,
        )
        final_results_list = _load_final_results_list(session_id)
        enriched = dict(data)
        if segment_translations:
            enriched["segment_translations"] = segment_translations
        if final_results_list:
            enriched["final_segments"] = _final_segments_payload(final_results_list)

        video_url = data.get("video_url")
        audio_duration = data.get("audio_duration")
        media_duration = None
        try:
            if audio_duration is not None and str(audio_duration).strip():
                # supports "1:23", "2408.5s" or plain seconds
                raw = str(audio_duration).strip().rstrip("sS")
                if ":" in raw:
                    parts = [float(p) for p in raw.split(":")]
                    media_duration = 0.0
                    for part in parts:
                        media_duration = media_duration * 60 + part
                else:
                    media_duration = float(raw)
        except (TypeError, ValueError):
            media_duration = None
        if media_duration is None:
            try:
                if data.get("audio_duration_seconds") is not None:
                    media_duration = float(data.get("audio_duration_seconds"))
            except (TypeError, ValueError):
                media_duration = None

        _update_upload_task_row(
            session_id,
            status="done",
            message=str(data.get("message") or "done"),
            video_url=f"/media/{session_id}/subtitled" if video_url else None,
            media_duration_seconds=media_duration,
            detected_lang=str(data.get("session_lang") or "") or None,
        )
        await task_manager.broadcast(session_id, {"type": "done", **enriched})
        _clear_offline_session_context(session_id)
        return True

    if event_type == "error":
        _update_upload_task_row(
            session_id,
            status="error",
            message=str(data.get("message") or "error"),
        )
        await task_manager.broadcast(session_id, {"type": event_type, **data})
        _clear_offline_session_context(session_id)
        return True

    await task_manager.broadcast(session_id, {"type": event_type, **data})
    return False


def build_worker_media_fetch_url(local_filepath: str, client_user_id: str | None = None) -> str | None:
    abs_path = os.path.abspath(local_filepath)
    cache_root = os.path.abspath(CACHE_DIR)
    upload_root = os.path.abspath(UPLOAD_DIR)
    if abs_path.startswith(cache_root + os.sep):
        rel = "/cache/" + os.path.basename(abs_path)
    elif abs_path.startswith(upload_root + os.sep):
        rel = "/files/" + os.path.basename(abs_path)
    else:
        return None
    base_url = f"{GATEWAY_PUBLIC_URL}{rel}"
    normalized_user_id = _normalize_client_user_id(client_user_id)
    if normalized_user_id:
        return f"{base_url}?{urlencode({'client_user_id': normalized_user_id})}"
    return base_url


def build_worker_process_payload(
    session_id: str,
    local_filepath: str,
    original_filename: str,
    fast_mode: bool,
    request_start_time: float | None,
    ui_language: str,
    client_user_id: str | None = None,
) -> dict:
    payload = {
        "session_id": session_id,
        "filepath": local_filepath,
        "original_filename": original_filename,
        "fast_mode": fast_mode,
        "request_start_time": request_start_time,
        "ui_language": ui_language,
    }
    if GATEWAY_CALLBACK_URL:
        payload["callback_url"] = GATEWAY_CALLBACK_URL
    file_fetch_url = build_worker_media_fetch_url(local_filepath, client_user_id=client_user_id)
    if file_fetch_url:
        payload["file_fetch_url"] = file_fetch_url
    return payload


async def _run_and_finalize_subtitles(session_id: str, data: dict):
    result_path = os.path.join(SEGMENT_DIR, session_id, "final_result.json")
    if not os.path.exists(result_path):
        _update_upload_task_row(
            session_id,
            status="error",
            message="未收到识别产物，字幕生成无法继续。",
        )
        await task_manager.broadcast(session_id, {
            "type": "error",
            "message": "未收到识别产物，字幕生成无法继续。",
        })
        return

    if not _load_final_results_list(session_id):
        _update_upload_task_row(
            session_id,
            status="error",
            message="未识别到语音内容。",
        )
        await task_manager.broadcast(session_id, {
            "type": "error",
            "message": "未识别到语音内容。",
        })
        _clear_offline_session_context(session_id)
        return

    loop = asyncio.get_running_loop()

    def progress_cb(message):
        asyncio.run_coroutine_threadsafe(
            task_manager.broadcast(session_id, {"type": "progress", "message": message}),
            loop,
        )

    try:
        ctx = _offline_session_context.get(session_id)
        if ctx:
            if data.get("session_lang"):
                ctx["session_lang"] = data.get("session_lang")
            if data.get("ui_language"):
                from subtitles.i18n import normalize_ui_language
                ctx["ui_language"] = normalize_ui_language(data.get("ui_language"))
        segment_translations = await _ensure_and_broadcast_segment_translations(
            session_id,
            data.get("session_lang", "zh"),
            data.get("ui_language", "zh-CN"),
        )
        result = await run_video_subtitle_pipeline(
            session_id,
            data.get("original_filename", ""),
            data.get("ui_language", "zh-CN"),
            data.get("session_lang", "zh"),
            progress_cb=progress_cb,
            fast_mode=False,
            asr_stage_timings=data.get("stage_timings"),
        )
        stage_timings = dict(data.get("stage_timings") or {})
        stage_timings.update(result.get("stage_timings") or {})
        stage_timing_summary = {k: round(v, 3) for k, v in stage_timings.items() if v > 0}
        stage_timing_summary["accounted_stage_s"] = round(sum(stage_timings.values()), 3)

        display_name = os.path.basename(data.get("original_filename", "") or "") or data.get("original_filename", "")
        video_url = result.get("video_url")
        if video_url:
            video_url = f"/media/{session_id}/subtitled"
        else:
            orig_ext = os.path.splitext(display_name)[1].lower()
            if orig_ext in VIDEO_EXTENSIONS:
                _update_upload_task_row(
                    session_id,
                    status="error",
                    message="字幕视频压制失败，请重新上传或刷新后重试。",
                )
                await task_manager.broadcast(session_id, {
                    "type": "error",
                    "message": "字幕视频压制失败，请重新上传或刷新后重试。",
                })
                _clear_offline_session_context(session_id)
                return
        done_payload = {
            "type": "done",
            "message": (
                f"{display_name} 识别与压制完成"
                f"（处理 {data.get('proc_duration', '')} / 总耗时 {data.get('total_duration', '')}）"
            ),
            "original_filename": data.get("original_filename"),
            "audio_duration": data.get("audio_duration"),
            "proc_duration": data.get("proc_duration"),
            "proc_duration_seconds": data.get("proc_duration_seconds"),
            "total_duration": data.get("total_duration"),
            "total_duration_seconds": data.get("total_duration_seconds"),
            "speaker_count": data.get("speaker_count"),
            "video_url": video_url,
            "stage_timings": stage_timing_summary,
        }
        if segment_translations:
            done_payload["segment_translations"] = segment_translations
        final_results_list = _load_final_results_list(session_id)
        if final_results_list:
            done_payload["final_segments"] = _final_segments_payload(final_results_list)

        media_duration = None
        try:
            raw = str(data.get("audio_duration") or "").strip().rstrip("sS")
            if raw:
                if ":" in raw:
                    media_duration = 0.0
                    for part in raw.split(":"):
                        media_duration = media_duration * 60 + float(part)
                else:
                    media_duration = float(raw)
        except (TypeError, ValueError):
            media_duration = None
        if media_duration is None:
            try:
                if data.get("audio_duration_seconds") is not None:
                    media_duration = float(data.get("audio_duration_seconds"))
            except (TypeError, ValueError):
                media_duration = None

        _update_upload_task_row(
            session_id,
            status="done",
            message=str(done_payload.get("message") or "done"),
            video_url=video_url,
            media_duration_seconds=media_duration,
            detected_lang=str(data.get("session_lang") or "") or None,
        )
        await task_manager.broadcast(session_id, done_payload)
        _clear_offline_session_context(session_id)
    except Exception as e:
        server_log.logger.error(f"[{session_id}] subtitle pipeline failed: {e}")
        _update_upload_task_row(
            session_id,
            status="error",
            message=f"字幕视频生成失败：{e}",
        )
        await task_manager.broadcast(session_id, {
            "type": "error",
            "message": f"字幕视频生成失败：{e}",
        })
        _clear_offline_session_context(session_id)


async def _handle_asr_done(session_id: str, data: dict):
    await task_manager.broadcast(session_id, {"type": "asr_done", **data})
    async with _subtitle_finalize_lock:
        if session_id in _subtitle_finalize_sessions:
            server_log.logger.warning(f"[{session_id}] 字幕压制已在进行，跳过重复 asr_done")
            return
        _subtitle_finalize_sessions.add(session_id)
    asyncio.create_task(_run_and_finalize_subtitles(session_id, data))


async def pull_worker_events(session_id: str, worker_url: str):
    if WORKER_EVENT_MODE != "pull":
        return
    latest_seq = -1
    idle_seconds = 0.0
    consecutive_failures = 0
    timeout = aiohttp.ClientTimeout(total=WORKER_EVENT_POLL_TIMEOUT_S)
    async with create_http_session(timeout=timeout) as session:
        while True:
            try:
                async with session.get(
                    f"{worker_url}/task_events/{session_id}",
                    params={"after_seq": latest_seq},
                ) as resp:
                    resp.raise_for_status()
                    payload = await resp.json()
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                server_log.logger.warning(
                    f"[{session_id}] pull worker events failed "
                    f"({consecutive_failures}/{WORKER_EVENT_POLL_MAX_RETRIES}): {e}"
                )
                if consecutive_failures >= WORKER_EVENT_POLL_MAX_RETRIES:
                    server_log.logger.error(
                        f"[{session_id}] pull worker events gave up after "
                        f"{WORKER_EVENT_POLL_MAX_RETRIES} consecutive failures"
                    )
                    await task_manager.broadcast(session_id, {
                        "type": "error",
                        "message": "与离线处理节点通信异常，请稍后重试。",
                    })
                    return
                await asyncio.sleep(WORKER_EVENT_POLL_INTERVAL_S)
                continue

            events = payload.get("events") or []
            if events:
                idle_seconds = 0.0
            else:
                idle_seconds += WORKER_EVENT_POLL_INTERVAL_S

            should_stop = False
            for event in events:
                event_type = event.get("type", "progress")
                data = event.get("data") or {}
                latest_seq = max(latest_seq, _safe_int(event.get("seq"), latest_seq))
                if await dispatch_worker_event(session_id, event_type, data):
                    should_stop = True
            if should_stop:
                return

            if payload.get("is_terminal"):
                return

            if idle_seconds >= WORKER_EVENT_POLL_MAX_IDLE_S:
                await task_manager.broadcast(session_id, {
                    "type": "error",
                    "message": "离线处理超时，未收到完成事件，请稍后重试。",
                })
                return
            await asyncio.sleep(WORKER_EVENT_POLL_INTERVAL_S)


def review_filename_with_llm_sync(filename: str, session_id: str = "") -> dict:
    if not ENABLE_CONTENT_SAFETY:
        return {
            "performed": False,
            "is_safe": True,
            "raw": "",
            "error": "",
            "url": "",
            "reason": "disabled_by_config",
        }
    if not filename or not str(filename).strip():
        return {
            "performed": False,
            "is_safe": True,
            "raw": "",
            "error": "",
            "url": "",
        }
    review_url = get_next_llm_url(
        route_source="gateway.filename_initial_llm_review",
        session_id=session_id,
    )
    return utils.run_llm_content_safety_review(
        filename,
        review_url,
        logger=server_log.logger,
        source="gateway.filename_initial_llm_review",
    )

def shorten_error_text(text: str, limit: int = 800) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + " ..."

async def run_subprocess(command):
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return (
        process.returncode,
        stdout.decode("utf-8", errors="ignore").strip(),
        stderr.decode("utf-8", errors="ignore").strip(),
    )

async def probe_media_duration(path: str) -> float:
    if not os.path.exists(path):
        return 0.0

    return_code, stdout, _ = await run_subprocess([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1",
        path,
    ])
    if return_code != 0 or not stdout:
        return 0.0

    try:
        return float(stdout.splitlines()[-1].strip())
    except (ValueError, IndexError):
        return 0.0

async def is_usable_audio_output(path: str, min_duration: float = 0.5) -> bool:
    if not os.path.exists(path):
        return False

    try:
        if os.path.getsize(path) <= 44:
            return False
    except OSError:
        return False

    return await probe_media_duration(path) >= min_duration

async def run_ffmpeg_extract_attempt(session_id: str, label: str, command, output_path: str, accept_partial: bool = False):
    if os.path.exists(output_path):
        os.remove(output_path)

    return_code, _, stderr = await run_subprocess(command)
    usable = await is_usable_audio_output(output_path)

    if usable:
        duration = await probe_media_duration(output_path)
        if return_code == 0:
            server_log.logger.info(f"[{session_id}] {label} 成功，恢复音频 {duration:.2f}s")
            return True, return_code, stderr, duration
        if accept_partial:
            server_log.logger.warning(
                f"[{session_id}] {label} 非零退出(code={return_code})，但已恢复可用音频 {duration:.2f}s，将继续用于 ASR。"
            )
            if stderr:
                server_log.logger.warning(f"[{session_id}] {label} 部分恢复日志: {shorten_error_text(stderr)}")
            return True, return_code, stderr, duration

    return False, return_code, stderr, 0.0

async def extract_audio_with_recovery(session_id: str, source_path: str, output_path: str):
    tolerant_audio_filter = "aresample=async=1:min_hard_comp=0.100:first_pts=0"
    repair_container_path = os.path.join(CACHE_DIR, f"{session_id}_repair.mp4")
    repair_audio_path = os.path.join(CACHE_DIR, f"{session_id}_repair_audio.mka")
    attempts = []

    try:
        strict_command = [
            "ffmpeg",
            "-i", source_path,
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-y",
            "-loglevel", "error",
            output_path,
        ]
        ok, return_code, stderr, _ = await run_ffmpeg_extract_attempt(
            session_id,
            "直接抽取音频",
            strict_command,
            output_path,
            accept_partial=False,
        )
        if ok:
            return
        attempts.append(f"直接抽取(code={return_code}): {shorten_error_text(stderr, 240)}")

        tolerant_extract_command = [
            "ffmpeg",
            "-fflags", "+discardcorrupt+genpts+igndts",
            "-err_detect", "ignore_err",
            "-i", source_path,
            "-map", "0:a:0?",
            "-vn",
            "-af", tolerant_audio_filter,
            "-ac", "1",
            "-ar", "16000",
            "-y",
            "-loglevel", "error",
            output_path,
        ]
        ok, return_code, stderr, _ = await run_ffmpeg_extract_attempt(
            session_id,
            "坏包容忍直抽",
            tolerant_extract_command,
            output_path,
            accept_partial=True,
        )
        if ok:
            return
        attempts.append(f"坏包容忍直抽(code={return_code}): {shorten_error_text(stderr, 240)}")

        for repair_path in [repair_container_path, repair_audio_path]:
            if os.path.exists(repair_path):
                os.remove(repair_path)

        remux_command = [
            "ffmpeg",
            "-fflags", "+discardcorrupt+genpts+igndts",
            "-err_detect", "ignore_err",
            "-i", source_path,
            "-map", "0",
            "-c", "copy",
            "-movflags", "+faststart",
            "-y",
            "-loglevel", "error",
            repair_container_path,
        ]
        remux_code, _, remux_stderr = await run_subprocess(remux_command)
        if os.path.exists(repair_container_path):
            if remux_code != 0:
                server_log.logger.warning(
                    f"[{session_id}] 宽容重封装返回非零(code={remux_code})，但已生成中间文件，将继续尝试抽音频。"
                )
            remux_extract_command = [
                "ffmpeg",
                "-fflags", "+discardcorrupt+genpts+igndts",
                "-err_detect", "ignore_err",
                "-i", repair_container_path,
                "-map", "0:a:0?",
                "-vn",
                "-af", tolerant_audio_filter,
                "-ac", "1",
                "-ar", "16000",
                "-y",
                "-loglevel", "error",
                output_path,
            ]
            ok, return_code, stderr, _ = await run_ffmpeg_extract_attempt(
                session_id,
                "重封装后抽音频",
                remux_extract_command,
                output_path,
                accept_partial=True,
            )
            if ok:
                return
            attempts.append(f"重封装后抽音频(code={return_code}): {shorten_error_text(stderr, 240)}")
        else:
            attempts.append(f"宽容重封装(code={remux_code}): {shorten_error_text(remux_stderr, 240)}")

        audio_remux_command = [
            "ffmpeg",
            "-fflags", "+discardcorrupt+genpts+igndts",
            "-err_detect", "ignore_err",
            "-i", source_path,
            "-map", "0:a:0?",
            "-c", "copy",
            "-y",
            "-loglevel", "error",
            repair_audio_path,
        ]
        audio_remux_code, _, audio_remux_stderr = await run_subprocess(audio_remux_command)
        if os.path.exists(repair_audio_path):
            if audio_remux_code != 0:
                server_log.logger.warning(
                    f"[{session_id}] 音频重封装返回非零(code={audio_remux_code})，但已生成中间文件，将继续尝试抽音频。"
                )
            audio_extract_command = [
                "ffmpeg",
                "-fflags", "+discardcorrupt+genpts+igndts",
                "-err_detect", "ignore_err",
                "-i", repair_audio_path,
                "-af", tolerant_audio_filter,
                "-ac", "1",
                "-ar", "16000",
                "-y",
                "-loglevel", "error",
                output_path,
            ]
            ok, return_code, stderr, _ = await run_ffmpeg_extract_attempt(
                session_id,
                "音频重封装后抽音频",
                audio_extract_command,
                output_path,
                accept_partial=True,
            )
            if ok:
                return
            attempts.append(f"音频重封装后抽音频(code={return_code}): {shorten_error_text(stderr, 240)}")
        else:
            attempts.append(f"音频重封装(code={audio_remux_code}): {shorten_error_text(audio_remux_stderr, 240)}")

        raise RuntimeError("; ".join(attempts))
    finally:
        for temp_path in [repair_container_path, repair_audio_path]:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

# ==========================================
# 路由
# ==========================================
@app.get("/favicon.ico")
async def favicon():
    from fastapi import Response
    return Response(status_code=204)

@app.get("/")
async def get():
    if os.path.exists(FRONTEND_INDEX_PATH):
        with open(FRONTEND_INDEX_PATH, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse(
        "<h1>frontend/dist/index.html not found</h1>"
        "<p>请先构建 React 前端：<code>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</code></p>"
        "<p>或通过 <code>ENABLE_FRONTEND_BUILD=1 bash run_gateway.sh</code> 启动网关。</p>",
        status_code=503,
    )


async def dispatch_offline_worker(
    session_id: str,
    target_process_path: str,
    original_filename: str | None,
    request_start_time: float | None,
    ui_language: str,
    client_user_id: str | None,
):
    """把已落盘的媒体交给离线 Worker 做 ASR，并开始拉取事件。

    上传接口与 B 站接口共用此逻辑：Worker 不可达时同步把任务标记为 error 并通知前端。
    """
    try:
        worker_url = await get_best_offline_worker_url(route_source="upload.process_offline", session_id=session_id)
        async with create_http_session() as session:
            payload = build_worker_process_payload(
                session_id,
                target_process_path,
                original_filename,
                False,
                request_start_time,
                ui_language or "zh-CN",
                client_user_id=client_user_id,
            )
            async with session.post(f"{worker_url}/process_offline", json=payload) as resp:
                resp.raise_for_status()  # 确保 HTTP 状态码为 200
        asyncio.create_task(pull_worker_events(session_id, worker_url))
    except Exception as e:
        server_log.logger.error(f"[{session_id}] 无法连接到 Worker 节点: {e}")
        # Worker 挂了，立刻通知前端中断 Loading 状态，避免死等
        _update_upload_task_row(
            session_id,
            status="error",
            message="AI 处理节点当前不可用，请稍后重试。",
        )
        await task_manager.broadcast(session_id, {
            "type": "error",
            "message": "AI 处理节点当前不可用，请稍后重试。"
        })


@app.post("/upload")
async def upload_audio(
    request: Request,
    file: UploadFile = File(...),
    client_start_ms: str | None = Form(None),
    ui_language: str | None = Form(None),
    client_user_id: str | None = Form(None),
):
    _, owner_user_id = await resolve_request_auth(request)
    # Prefer authenticated owner; keep form field only as soft consistency check.
    form_user = _normalize_client_user_id(client_user_id)
    if form_user and form_user != owner_user_id:
        server_log.logger.warning(
            f"[Upload] client_user_id mismatch form={form_user} auth={owner_user_id}, using auth"
        )
    client_user_id = owner_user_id

    request_start_time = time.time()
    if client_start_ms:
        try:
            client_start_s = float(client_start_ms) / 1000.0
            if 0 < request_start_time - client_start_s < 12 * 3600:
                request_start_time = client_start_s
        except (TypeError, ValueError):
            pass

    filename_review = await asyncio.to_thread(review_filename_with_llm_sync, file.filename, "")
    if not filename_review.get("is_safe", True):
        llm_raw = filename_review.get("raw", "")
        server_log.logger.warning(
            f"[Upload ContentSafety] 文件名未通过审核，已静默拒绝上传。"
            f" filename={file.filename}, llm_raw={llm_raw}"
        )
        raise HTTPException(status_code=400, detail="无法处理该文件，请稍后重试。")

    created_at_dt = datetime.datetime.now(datetime.timezone.utc)
    session_id = new_session_id(created_at_dt)
    created_at = created_at_dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{created_at_dt.microsecond // 1000:03d}Z"
    original_basename = build_upload_stored_basename(session_id, client_user_id, file.filename or "")
    original_filepath = os.path.join(UPLOAD_DIR, original_basename)
    
    # 【优化 1】：异步非阻塞存盘
    # shutil.copyfileobj 是同步阻塞的，写超大文件时会卡死网关，必须丢到线程池中运行
    def save_upload_file():
        with open(original_filepath, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    
    await asyncio.to_thread(save_upload_file)

    playback_filename = f"{session_id}.wav"
    playback_filepath = os.path.join(CACHE_DIR, playback_filename)
    
    # 【优化 2】：真正的异步非阻塞 FFmpeg 调用 + 坏包恢复链路
    try:
        await extract_audio_with_recovery(session_id, original_filepath, playback_filepath)
        file_url = f"/cache/{playback_filename}"
        target_process_path = playback_filepath

    except Exception as e:
        orig_ext = os.path.splitext(file.filename or "")[1].lower()
        if orig_ext in VIDEO_EXTENSIONS:
            server_log.logger.warning(
                f"[{session_id}] 视频抽音频失败，拒绝处理: {e}"
            )
            try:
                os.remove(original_filepath)
            except OSError:
                pass
            raise HTTPException(
                status_code=400,
                detail="视频文件无效或损坏，无法提取音频",
            )
        server_log.logger.warning(f"[{session_id}] 异步提取音频异常，将回退使用源文件: {e}")
        target_process_path = original_filepath
        file_url = f"/files/{os.path.basename(original_filepath)}"

    await task_manager.create_task(session_id, client_user_id=client_user_id)
    original_file_url = f"/files/{original_basename}"
    _persist_upload_task_row(
        task_id=session_id,
        user_id=client_user_id,
        file_name=file.filename or original_basename,
        status="processing",
        message="queued",
        created_at=created_at,
        file_url=file_url,
        original_file_url=original_file_url,
    )
    _init_offline_session_context(session_id, ui_language or "zh-CN")
    
    # 【优化 3】：带异常通知的异步 Worker 触发器
    # 丢入后台任务池执行，不阻塞当前接口返回
    asyncio.create_task(
        dispatch_offline_worker(
            session_id,
            target_process_path,
            file.filename,
            request_start_time,
            ui_language or "zh-CN",
            client_user_id,
        )
    )

    return {
        "status": "queued",
        "task_id": session_id,
        "created_at": created_at,
        "file_url": file_url,
        "original_file_url": original_file_url,
    }


class BilibiliUploadRequest(BaseModel):
    url: str
    ui_language: str | None = None
    client_user_id: str | None = None
    client_start_ms: str | None = None


class VideoUploadRequest(BaseModel):
    """通用视频链接提交：自动识别 B 站 / 抖音 / YouTube 等平台并路由到对应下载器。"""
    url: str
    ui_language: str | None = None
    client_user_id: str | None = None
    client_start_ms: str | None = None


class BilibiliPublishRequest(BaseModel):
    """将已烧录字幕的成片一键投稿到 B 站。"""
    title: str | None = None
    desc: str | None = None
    tags: str | None = None
    tid: int | None = None
    copyright: int | None = None
    source: str | None = None
    client_user_id: str | None = None


# task_id -> 投稿任务状态（内存；进程重启后清空）
_bili_publish_jobs: dict[str, dict] = {}
_bili_publish_jobs_guard = __import__("threading").Lock()


def _default_publish_title_from_filename(file_name: str) -> str:
    base = os.path.splitext(file_name or "")[0].strip() or "字幕视频"
    # 去掉常见后缀尾巴
    base = re.sub(r"(_subtitle|_subtitled|_字幕)$", "", base, flags=re.IGNORECASE).strip() or "字幕视频"
    return bilibili_uploader.sanitize_title(base)


async def _set_bili_publish_job(task_id: str, **fields):
    with _bili_publish_jobs_guard:
        cur = dict(_bili_publish_jobs.get(task_id) or {})
        cur.update(fields)
        cur["task_id"] = task_id
        cur["updated_at"] = time.time()
        _bili_publish_jobs[task_id] = cur
        return dict(cur)


async def _get_bili_publish_job(task_id: str) -> dict | None:
    with _bili_publish_jobs_guard:
        job = _bili_publish_jobs.get(task_id)
        return dict(job) if job else None


async def _run_bilibili_publish(
    task_id: str,
    video_path: str,
    *,
    title: str,
    desc: str,
    tags: str | None,
    tid: int | None,
    copyright: int | None,
    source: str | None,
):
    """后台线程执行投稿，并通过内存状态 + SSE 推送进度。"""
    loop = asyncio.get_running_loop()

    def progress_cb(message: str, ratio: float):
        percent = round(float(ratio) * 100, 1)
        asyncio.run_coroutine_threadsafe(
            _set_bili_publish_job(
                task_id,
                status="publishing",
                message=message,
                progress=percent,
            ),
            loop,
        )
        asyncio.run_coroutine_threadsafe(
            task_manager.broadcast(task_id, {
                "type": "bilibili_publish_progress",
                "message": message,
                "progress": percent,
            }),
            loop,
        )

    try:
        result = await asyncio.to_thread(
            bilibili_uploader.publish_video,
            video_path,
            title=title,
            desc=desc or "",
            tags=tags,
            tid=tid,
            copyright=copyright,
            source=source,
            progress=progress_cb,
        )
        await _set_bili_publish_job(
            task_id,
            status="done",
            message="投稿成功",
            progress=100,
            bvid=result.get("bvid"),
            aid=result.get("aid"),
            url=result.get("url"),
            title=result.get("title") or title,
            error=None,
        )
        await task_manager.broadcast(task_id, {
            "type": "bilibili_publish_done",
            "message": "投稿成功",
            "bvid": result.get("bvid"),
            "aid": result.get("aid"),
            "url": result.get("url"),
            "title": result.get("title") or title,
        })
        server_log.logger.info(
            f"[BilibiliPublish] ok task={task_id} bvid={result.get('bvid')} aid={result.get('aid')}"
        )
    except bilibili_uploader.BiliUploadError as e:
        msg = str(e) or "投稿失败"
        await _set_bili_publish_job(
            task_id,
            status="error",
            message=msg,
            error=msg,
        )
        await task_manager.broadcast(task_id, {
            "type": "bilibili_publish_error",
            "message": msg,
        })
        server_log.logger.warning(f"[BilibiliPublish] failed task={task_id}: {msg}")
    except Exception as e:
        msg = f"投稿异常: {e}"
        await _set_bili_publish_job(
            task_id,
            status="error",
            message=msg,
            error=msg,
        )
        await task_manager.broadcast(task_id, {
            "type": "bilibili_publish_error",
            "message": msg,
        })
        server_log.logger.exception(f"[BilibiliPublish] unexpected task={task_id}")


def _sanitize_bili_title(title: str) -> str:
    """把 B 站标题转成安全的文件名主体（保留中文，剔除路径/控制字符）。"""
    cleaned = re.sub(r'[\\/:*?"<>|\n\r\t]', "_", title or "").strip()
    cleaned = cleaned.strip(". ")
    return cleaned[:80] or "bilibili_video"


async def _run_url_download_and_process(
    *,
    session_id: str,
    original_filename: str,
    original_filepath: str,
    ui_language: str,
    client_user_id: str | None,
    request_start_time: float | None,
    platform_label: str,
    download_fn,
    download_start_msg: str,
    download_fail_msg: str,
):
    """后台任务：下载视频 -> 抽音频 -> 派发 Worker，全程通过 SSE 上报进度。

    平台无关的通用入口：B 站 / 抖音 / YouTube 等入口各自构造 ``download_fn`` 闭包
    （已绑定 url、清晰度、cookie/代理等平台特定参数），只接收 ``progress_cb``。
    """
    loop = asyncio.get_running_loop()

    # 下载进度节流：仅在整数百分比变化时推送，避免刷屏。
    last_pct = {"video": -1, "audio": -1}

    def progress_cb(stage: str, done: int, total: int):
        if total <= 0:
            return
        pct = int(done * 100 / total)
        if pct == last_pct.get(stage):
            return
        last_pct[stage] = pct
        # 视频流约占体积大头，用「视频 0-90% / 音频 90-100%」粗略映射到整体下载进度。
        overall = pct * 0.9 if stage == "video" else 90 + pct * 0.1
        label = "下载视频流" if stage == "video" else "下载音频流"
        msg = f"正在从{platform_label}下载：{label} {pct}%"
        asyncio.run_coroutine_threadsafe(
            task_manager.broadcast(session_id, {
                "type": "progress",
                "phase": "processing",
                "queue_position": 0,
                "message": msg,
                "download_percent": round(overall, 1),
            }),
            loop,
        )

    try:
        await task_manager.broadcast(session_id, {
            "type": "progress",
            "phase": "processing",
            "queue_position": 0,
            "message": download_start_msg,
        })
        await asyncio.to_thread(download_fn, progress_cb)
    except Exception as e:
        server_log.logger.warning(f"[{session_id}] {platform_label}视频下载失败: {e}")
        try:
            if os.path.exists(original_filepath):
                os.remove(original_filepath)
        except OSError:
            pass
        _update_upload_task_row(
            session_id,
            status="error",
            message=download_fail_msg,
        )
        await task_manager.broadcast(session_id, {
            "type": "error",
            "message": download_fail_msg,
        })
        return

    playback_filename = f"{session_id}.wav"
    playback_filepath = os.path.join(CACHE_DIR, playback_filename)
    try:
        await extract_audio_with_recovery(session_id, original_filepath, playback_filepath)
        target_process_path = playback_filepath
    except Exception as e:
        server_log.logger.warning(f"[{session_id}] {platform_label}视频抽音频失败: {e}")
        _update_upload_task_row(
            session_id,
            status="error",
            message="下载的视频无法解析音轨，请稍后重试。",
        )
        await task_manager.broadcast(session_id, {
            "type": "error",
            "message": "下载的视频无法解析音轨，请稍后重试。",
        })
        return

    await task_manager.broadcast(session_id, {
        "type": "progress",
        "phase": "processing",
        "queue_position": 0,
        "message": "下载完成，正在识别…",
    })
    await dispatch_offline_worker(
        session_id,
        target_process_path,
        original_filename,
        request_start_time,
        ui_language or "zh-CN",
        client_user_id,
    )


def _make_bilibili_download_fn(url, original_filepath, target_qn, sessdata, meta_info):
    """构造 B 站下载闭包，供 _run_url_download_and_process 在线程中调用。"""
    def _download(progress_cb):
        return bilibili_downloader.download_to_path(
            url,
            original_filepath,
            target_qn=target_qn,
            sessdata=sessdata,
            progress_cb=progress_cb,
            info=meta_info,
        )
    return _download


def _make_youtube_download_fn(url, original_filepath, target_format, proxy, meta_info):
    """构造 YouTube 下载闭包，供 _run_url_download_and_process 在线程中调用。"""
    def _download(progress_cb):
        return youtube_downloader.download_to_path(
            url,
            original_filepath,
            target_format=target_format,
            proxy=proxy,
            progress_cb=progress_cb,
            info=meta_info,
        )
    return _download


def _make_douyin_download_fn(url, original_filepath, target_format, proxy, meta_info):
    """构造抖音下载闭包，供 _run_url_download_and_process 在线程中调用。"""
    def _download(progress_cb):
        return douyin_downloader.download_to_path(
            url,
            original_filepath,
            target_format=target_format,
            proxy=proxy,
            progress_cb=progress_cb,
            info=meta_info,
        )
    return _download


async def _run_bilibili_download_and_process(
    *,
    session_id: str,
    url: str,
    original_filename: str,
    original_filepath: str,
    ui_language: str,
    client_user_id: str | None,
    request_start_time: float | None,
    meta_info: dict | None,
    target_qn: int | None = None,
    sessdata: str | None = None,
):
    """后台任务：下载 B 站视频 -> 抽音频 -> 派发 Worker（保留旧入口，转发到通用实现）。"""
    await _run_url_download_and_process(
        session_id=session_id,
        original_filename=original_filename,
        original_filepath=original_filepath,
        ui_language=ui_language,
        client_user_id=client_user_id,
        request_start_time=request_start_time,
        platform_label="哔哩哔哩",
        download_fn=_make_bilibili_download_fn(
            url, original_filepath, target_qn, sessdata, meta_info
        ),
        download_start_msg="正在从哔哩哔哩下载视频…",
        download_fail_msg="哔哩哔哩视频下载失败，请检查链接或稍后重试。",
    )


@app.post("/upload_bilibili")
async def upload_bilibili(req: BilibiliUploadRequest, request: Request):
    """粘贴 B 站视频链接：解析标题 -> 后台下载 -> 复用离线识别与字幕流程。

    登录状态可选；清晰度默认请求可用最高档。若配置了 bilibili_config.json
    中的 SESSDATA，则所有用户共用该账号解锁更高画质。
    """
    _auth_user, owner_user_id = await resolve_request_auth_optional(request)
    # 登录用户以鉴权身份为准；匿名用户回退到前端携带的 client_user_id，
    # 以便其后续 SSE / 历史请求（携带同一 UUID）能通过归属校验。
    client_user_id = owner_user_id or _normalize_client_user_id(req.client_user_id)

    target_qn = bilibili_downloader.resolve_target_qn()
    sessdata = bilibili_downloader.config_sessdata()

    url = (req.url or "").strip()
    if not url or not bilibili_downloader.looks_like_bilibili(url):
        raise HTTPException(status_code=400, detail="请输入有效的哔哩哔哩视频链接或 BV 号。")

    ui_language = req.ui_language or "zh-CN"

    request_start_time = time.time()
    if req.client_start_ms:
        try:
            client_start_s = float(req.client_start_ms) / 1000.0
            if 0 < request_start_time - client_start_s < 12 * 3600:
                request_start_time = client_start_s
        except (TypeError, ValueError):
            pass

    # 解析基础信息（标题/cid），失败即视为无效链接。
    try:
        meta_info = await asyncio.to_thread(bilibili_downloader.fetch_basic_meta, url)
    except Exception as e:
        server_log.logger.warning(f"[Bilibili] 解析视频信息失败 url={url}: {e}")
        raise HTTPException(status_code=400, detail="无法解析该哔哩哔哩链接，请确认视频是否存在或可公开访问。")

    title = _sanitize_bili_title(str(meta_info.get("title") or meta_info.get("bvid") or "bilibili_video"))
    display_filename = f"{title}.mp4"

    # 文件名内容审核，与本地上传保持一致。
    filename_review = await asyncio.to_thread(review_filename_with_llm_sync, display_filename, "")
    if not filename_review.get("is_safe", True):
        server_log.logger.warning(
            f"[Bilibili ContentSafety] 标题未通过审核，已静默拒绝。title={display_filename}"
        )
        raise HTTPException(status_code=400, detail="无法处理该视频，请稍后重试。")

    created_at_dt = datetime.datetime.now(datetime.timezone.utc)
    session_id = new_session_id(created_at_dt)
    created_at = created_at_dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{created_at_dt.microsecond // 1000:03d}Z"
    original_basename = build_upload_stored_basename(session_id, client_user_id, display_filename)
    original_filepath = os.path.join(UPLOAD_DIR, original_basename)

    await task_manager.create_task(session_id, client_user_id=client_user_id)
    original_file_url = f"/files/{original_basename}"
    file_url = f"/cache/{session_id}.wav"
    _persist_upload_task_row(
        task_id=session_id,
        user_id=client_user_id,
        file_name=display_filename,
        status="processing",
        message="正在从哔哩哔哩下载视频…",
        created_at=created_at,
        file_url=file_url,
        original_file_url=original_file_url,
    )
    _init_offline_session_context(session_id, ui_language)

    asyncio.create_task(
        _run_bilibili_download_and_process(
            session_id=session_id,
            url=url,
            original_filename=display_filename,
            original_filepath=original_filepath,
            ui_language=ui_language,
            client_user_id=client_user_id,
            request_start_time=request_start_time,
            meta_info=meta_info,
            target_qn=target_qn,
            sessdata=sessdata,
        )
    )

    return {
        "status": "queued",
        "task_id": session_id,
        "created_at": created_at,
        "file_url": file_url,
        "original_file_url": original_file_url,
        "file_name": display_filename,
    }


@app.post("/upload_video")
async def upload_video(req: VideoUploadRequest, request: Request):
    """粘贴视频链接：自动识别平台（B 站 / 抖音 / YouTube）-> 解析标题 -> 后台下载
    -> 复用离线识别与字幕流程。

    登录状态可选；清晰度默认请求可用最高档。B 站若配置了 SESSDATA 则共用该
    账号解锁更高画质。YouTube / 抖音下载默认复用环境变量 https_proxy/http_proxy
    （或 YTDLP_PROXY）；抖音另可配置 douyin_config.json 的 cookies_file。
    """
    _auth_user, owner_user_id = await resolve_request_auth_optional(request)
    client_user_id = owner_user_id or _normalize_client_user_id(req.client_user_id)

    url = (req.url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="请输入视频链接。")

    is_bili = bilibili_downloader.looks_like_bilibili(url)
    is_dy = douyin_downloader.looks_like_douyin(url)
    is_yt = youtube_downloader.looks_like_youtube(url)
    if not is_bili and not is_dy and not is_yt:
        raise HTTPException(
            status_code=400,
            detail="请输入有效的视频链接（支持哔哩哔哩 / 抖音 / YouTube）。",
        )

    ui_language = req.ui_language or "zh-CN"

    request_start_time = time.time()
    if req.client_start_ms:
        try:
            client_start_s = float(req.client_start_ms) / 1000.0
            if 0 < request_start_time - client_start_s < 12 * 3600:
                request_start_time = client_start_s
        except (TypeError, ValueError):
            pass

    # 平台优先级：B 站 > 抖音 > YouTube（避免误匹配时落到错误下载器）。
    if is_bili:
        target_qn = bilibili_downloader.resolve_target_qn()
        sessdata = bilibili_downloader.config_sessdata()
        try:
            meta_info = await asyncio.to_thread(bilibili_downloader.fetch_basic_meta, url)
        except Exception as e:
            server_log.logger.warning(f"[Bilibili] 解析视频信息失败 url={url}: {e}")
            raise HTTPException(
                status_code=400, detail="无法解析该哔哩哔哩链接，请确认视频是否存在或可公开访问。"
            )
        title = _sanitize_bili_title(
            str(meta_info.get("title") or meta_info.get("bvid") or "bilibili_video")
        )
        platform_label = "哔哩哔哩"
        download_start_msg = "正在从哔哩哔哩下载视频…"
        download_fail_msg = "哔哩哔哩视频下载失败，请检查链接或稍后重试。"
    elif is_dy:
        target_format = douyin_downloader.resolve_target_format()
        proxy = douyin_downloader.config_proxy()
        try:
            meta_info = await asyncio.to_thread(
                douyin_downloader.fetch_basic_meta, url, None, proxy
            )
        except douyin_downloader.DouyinError as e:
            server_log.logger.warning(f"[Douyin] 解析视频信息失败 url={url}: {e}")
            raise HTTPException(
                status_code=400,
                detail=f"无法解析该抖音链接：{e}",
            )
        except Exception as e:
            server_log.logger.warning(f"[Douyin] 解析视频信息失败 url={url}: {e}")
            raise HTTPException(
                status_code=400, detail="无法解析该抖音链接，请稍后重试。"
            )
        title = _sanitize_bili_title(
            str(meta_info.get("title") or meta_info.get("id") or "douyin_video")
        )
        platform_label = "抖音"
        download_start_msg = "正在从抖音下载视频…"
        download_fail_msg = "抖音视频下载失败，请检查链接、网络或 cookies 后稍后重试。"
    else:
        target_format = youtube_downloader.resolve_target_format()
        proxy = youtube_downloader.config_proxy()
        try:
            meta_info = await asyncio.to_thread(
                youtube_downloader.fetch_basic_meta, url, None, proxy
            )
        except youtube_downloader.YoutubeError as e:
            server_log.logger.warning(f"[YouTube] 解析视频信息失败 url={url}: {e}")
            raise HTTPException(
                status_code=400,
                detail="无法解析该 YouTube 链接，请确认视频是否存在、可公开访问，或检查下载服务配置。",
            )
        except Exception as e:
            server_log.logger.warning(f"[YouTube] 解析视频信息失败 url={url}: {e}")
            raise HTTPException(
                status_code=400, detail="无法解析该 YouTube 链接，请稍后重试。"
            )
        title = _sanitize_bili_title(
            str(meta_info.get("title") or meta_info.get("id") or "youtube_video")
        )
        platform_label = "YouTube"
        download_start_msg = "正在从 YouTube 下载视频…"
        download_fail_msg = "YouTube 视频下载失败，请检查链接或下载服务配置后稍后重试。"

    display_filename = f"{title}.mp4"

    # 文件名内容审核，与本地上传保持一致。
    filename_review = await asyncio.to_thread(review_filename_with_llm_sync, display_filename, "")
    if not filename_review.get("is_safe", True):
        server_log.logger.warning(
            f"[{platform_label} ContentSafety] 标题未通过审核，已静默拒绝。title={display_filename}"
        )
        raise HTTPException(status_code=400, detail="无法处理该视频，请稍后重试。")

    created_at_dt = datetime.datetime.now(datetime.timezone.utc)
    session_id = new_session_id(created_at_dt)
    created_at = created_at_dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{created_at_dt.microsecond // 1000:03d}Z"
    original_basename = build_upload_stored_basename(session_id, client_user_id, display_filename)
    original_filepath = os.path.join(UPLOAD_DIR, original_basename)

    # 现在已知真实输出路径，构造平台对应的下载闭包。
    if is_bili:
        download_fn = _make_bilibili_download_fn(
            url, original_filepath, target_qn, sessdata, meta_info
        )
    elif is_dy:
        download_fn = _make_douyin_download_fn(
            url, original_filepath, target_format, proxy, meta_info
        )
    else:
        download_fn = _make_youtube_download_fn(
            url, original_filepath, target_format, proxy, meta_info
        )

    await task_manager.create_task(session_id, client_user_id=client_user_id)
    original_file_url = f"/files/{original_basename}"
    file_url = f"/cache/{session_id}.wav"
    _persist_upload_task_row(
        task_id=session_id,
        user_id=client_user_id,
        file_name=display_filename,
        status="processing",
        message=download_start_msg,
        created_at=created_at,
        file_url=file_url,
        original_file_url=original_file_url,
    )
    _init_offline_session_context(session_id, ui_language)

    asyncio.create_task(
        _run_url_download_and_process(
            session_id=session_id,
            original_filename=display_filename,
            original_filepath=original_filepath,
            ui_language=ui_language,
            client_user_id=client_user_id,
            request_start_time=request_start_time,
            platform_label=platform_label,
            download_fn=download_fn,
            download_start_msg=download_start_msg,
            download_fail_msg=download_fail_msg,
        )
    )

    return {
        "status": "queued",
        "task_id": session_id,
        "created_at": created_at,
        "file_url": file_url,
        "original_file_url": original_file_url,
        "file_name": display_filename,
    }


def resolve_task_media_path(session_id: str) -> tuple[str, str] | None:
    """返回 (绝对路径, media_type)。视频任务优先返回烧录字幕后的成片。"""
    subtitled_path = os.path.join(CACHE_DIR, f"{session_id}_subtitled.mp4")
    if os.path.exists(subtitled_path):
        return subtitled_path, "video/mp4"

    upload_candidates = collect_upload_paths_for_session(session_id)

    for path, ext in upload_candidates:
        if ext in VIDEO_EXTENSIONS:
            return path, MEDIA_TYPE_BY_EXT.get(ext, "application/octet-stream")

    cache_wav = os.path.join(CACHE_DIR, f"{session_id}.wav")
    if os.path.exists(cache_wav):
        return cache_wav, "audio/wav"

    reprocess_wav = os.path.join(CACHE_DIR, f"{session_id}_reprocess.wav")
    if os.path.exists(reprocess_wav):
        return reprocess_wav, "audio/wav"

    if upload_candidates:
        path, ext = upload_candidates[0]
        return path, MEDIA_TYPE_BY_EXT.get(ext, "application/octet-stream")

    return None


@app.get("/media/{session_id}/subtitled")
async def serve_subtitled_media(
    session_id: str,
    client_user_id: str | None = Query(None),
    download_subtitled: int | None = Query(None),
):
    """字幕成片专用端点：FileResponse + Range，避免 StaticFiles 中途断流。"""
    if not await task_manager.verify_access(session_id, client_user_id):
        raise HTTPException(status_code=403, detail="无权访问该媒体文件")
    subtitled_path = os.path.join(CACHE_DIR, f"{session_id}_subtitled.mp4")
    if not os.path.exists(subtitled_path):
        raise HTTPException(status_code=404, detail="字幕视频不存在")
    if download_subtitled:
        record_client_click(
            action="download_subtitled_video",
            client_user_id=client_user_id,
            task_id=session_id,
            label="下载字幕视频",
            source="media_serve",
            meta={"download_subtitled": True},
        )
    return FileResponse(
        subtitled_path,
        media_type="video/mp4",
        filename=f"{session_id}_subtitled.mp4",
        content_disposition_type="inline",
        headers={"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=604800"},
    )


@app.get("/task/{task_id}/publish_bilibili/meta")
async def publish_task_bilibili_meta(
    task_id: str,
    request: Request,
    client_user_id: str | None = Query(None),
    refresh: int | None = Query(None),
):
    """用专用大模型根据转写文本生成投稿标题 / 简介 / 标签。

    默认复用落盘缓存；refresh=1 时强制重新生成。
    """
    requester = _normalize_client_user_id(client_user_id)
    _auth_user, owner_user_id = await resolve_request_auth_optional(request)
    if owner_user_id:
        requester = owner_user_id
    if not await task_manager.verify_access(task_id, requester):
        raise HTTPException(status_code=403, detail="无权访问该任务")

    task_row = upload_task_store.get_task(UPLOAD_TASKS_DB_PATH, task_id) or {}
    file_name = task_row.get("file_name") or f"{task_id}.mp4"
    fallback_title = _default_publish_title_from_filename(file_name)

    result_path = os.path.join(SEGMENT_DIR, task_id, "final_result.json")
    try:
        source_mtime = os.path.getmtime(result_path)
    except OSError:
        source_mtime = None

    def _cover_payload():
        cover_file = bilibili_uploader.publish_cover_path(CACHE_DIR, task_id)
        if os.path.isfile(cover_file) and os.path.getsize(cover_file) > 64:
            return {
                "cover_url": f"/media/{task_id}/publish_cover",
                "cover_available": True,
            }
        return {"cover_url": None, "cover_available": False}

    force_refresh = bool(refresh)
    if not force_refresh:
        cached = await asyncio.to_thread(
            bilibili_uploader.load_publish_meta_cache,
            CACHE_DIR,
            task_id,
            source_mtime,
        )
        if cached:
            return {
                "task_id": task_id,
                "title": cached.get("title") or fallback_title,
                "desc": cached.get("desc") or "",
                "tags": cached.get("tags") or "",
                "cached": True,
                **_cover_payload(),
            }

    final_results_list = _load_final_results_list(task_id)
    transcript = ""
    try:
        from subtitles.rag import full_text_of

        transcript = full_text_of(final_results_list) if final_results_list else ""
    except Exception:
        parts = []
        for item in final_results_list or []:
            if isinstance(item, dict):
                t = item.get("text") or item.get("raw_text") or ""
                if t:
                    parts.append(str(t))
        transcript = "".join(parts)

    try:
        meta = await asyncio.to_thread(
            bilibili_uploader.generate_publish_meta,
            transcript,
            file_name=file_name,
            fallback_title=fallback_title,
        )
    except bilibili_uploader.BiliUploadError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        server_log.logger.exception(f"[BilibiliMeta] task={task_id} failed")
        raise HTTPException(status_code=503, detail=f"生成投稿文案失败: {e}") from e

    title = meta.get("title") or fallback_title
    desc = meta.get("desc") or ""
    tags = meta.get("tags") or ""
    await asyncio.to_thread(
        bilibili_uploader.save_publish_meta_cache,
        CACHE_DIR,
        task_id,
        title=title,
        desc=desc,
        tags=tags,
        source_mtime=source_mtime,
    )
    return {
        "task_id": task_id,
        "title": title,
        "desc": desc,
        "tags": tags,
        "cached": False,
        **_cover_payload(),
    }


class BilibiliPublishMetaSaveRequest(BaseModel):
    title: str
    desc: str | None = None
    tags: str | None = None
    client_user_id: str | None = None


@app.put("/task/{task_id}/publish_bilibili/meta")
async def save_publish_task_bilibili_meta(
    task_id: str,
    req: BilibiliPublishMetaSaveRequest,
    request: Request,
    client_user_id: str | None = Query(None),
):
    """保存用户编辑后的投稿标题 / 简介 / 标签，下次直接复用。"""
    requester = _normalize_client_user_id(client_user_id) or _normalize_client_user_id(
        req.client_user_id
    )
    _auth_user, owner_user_id = await resolve_request_auth_optional(request)
    if owner_user_id:
        requester = owner_user_id
    if not await task_manager.verify_access(task_id, requester):
        raise HTTPException(status_code=403, detail="无权访问该任务")

    title = bilibili_uploader.sanitize_title((req.title or "").strip())
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")

    result_path = os.path.join(SEGMENT_DIR, task_id, "final_result.json")
    try:
        source_mtime = os.path.getmtime(result_path)
    except OSError:
        source_mtime = None

    saved = await asyncio.to_thread(
        bilibili_uploader.save_publish_meta_cache,
        CACHE_DIR,
        task_id,
        title=title,
        desc=(req.desc or "").strip(),
        tags=(req.tags or "").strip(),
        source_mtime=source_mtime,
    )
    return {
        "task_id": task_id,
        "title": saved.get("title") or title,
        "desc": saved.get("desc") or "",
        "tags": saved.get("tags") or "",
        "cached": True,
    }


class BilibiliPublishCoverRequest(BaseModel):
    title: str | None = None
    desc: str | None = None
    tags: str | None = None
    refresh: bool | None = False
    client_user_id: str | None = None


@app.post("/task/{task_id}/publish_bilibili/cover")
async def generate_publish_task_cover(
    task_id: str,
    req: BilibiliPublishCoverRequest,
    request: Request,
    client_user_id: str | None = Query(None),
):
    """根据摘要核心提炼画面 Prompt，再调用 Qwen-Image 生成投稿封面。"""
    requester = _normalize_client_user_id(client_user_id) or _normalize_client_user_id(
        req.client_user_id
    )
    _auth_user, owner_user_id = await resolve_request_auth_optional(request)
    if owner_user_id:
        requester = owner_user_id
    if not await task_manager.verify_access(task_id, requester):
        raise HTTPException(status_code=403, detail="无权访问该任务")

    cover_file = bilibili_uploader.publish_cover_path(CACHE_DIR, task_id)
    if not req.refresh and os.path.isfile(cover_file) and os.path.getsize(cover_file) > 64:
        return {
            "task_id": task_id,
            "cover_url": f"/media/{task_id}/publish_cover",
            "cached": True,
        }

    # 优先用请求体；否则回退到已缓存文案
    title = (req.title or "").strip()
    desc = (req.desc or "").strip()
    tags = (req.tags or "").strip()
    if not (title or desc):
        cached = await asyncio.to_thread(
            bilibili_uploader.load_publish_meta_cache,
            CACHE_DIR,
            task_id,
            None,
        )
        if cached:
            title = title or str(cached.get("title") or "")
            desc = desc or str(cached.get("desc") or "")
            tags = tags or str(cached.get("tags") or "")

    if not (title or desc):
        raise HTTPException(status_code=400, detail="请先生成标题/简介，再生成封面")

    summary = ""
    try:
        summary = await asyncio.to_thread(rag.load_cached_summary, task_id)
        if not summary:
            final_results_list = _load_final_results_list(task_id)
            if final_results_list:
                built = await rag.get_or_build_summary(
                    task_id, final_results_list, ui_language="zh-CN"
                )
                summary = str((built or {}).get("summary") or "").strip()
    except Exception as e:
        server_log.logger.warning(
            f"[PublishCover] task={task_id} 摘要不可用，改用标题/简介: {e}"
        )

    try:
        result = await asyncio.to_thread(
            bilibili_uploader.generate_publish_cover,
            CACHE_DIR,
            task_id,
            title=title,
            desc=desc,
            tags=tags,
            summary=summary,
        )
    except bilibili_uploader.BiliUploadError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        server_log.logger.exception(f"[PublishCover] task={task_id} failed")
        raise HTTPException(status_code=503, detail=f"封面生成失败: {e}") from e

    cover_prompt = str(result.get("prompt") or "").strip()
    server_log.logger.info(
        f"[PublishCover] task={task_id} summary_chars={len(summary)} "
        f"prompt={cover_prompt[:400]}"
    )

    # 把封面路径写回 meta 缓存（保留原 title/desc/tags）
    result_path = os.path.join(SEGMENT_DIR, task_id, "final_result.json")
    try:
        source_mtime = os.path.getmtime(result_path)
    except OSError:
        source_mtime = None
    await asyncio.to_thread(
        bilibili_uploader.save_publish_meta_cache,
        CACHE_DIR,
        task_id,
        title=title or "字幕视频",
        desc=desc,
        tags=tags,
        source_mtime=source_mtime,
        cover_path=result.get("cover_path"),
    )
    return {
        "task_id": task_id,
        "cover_url": result.get("cover_url") or f"/media/{task_id}/publish_cover",
        "cached": False,
    }


@app.get("/media/{session_id}/publish_cover")
async def serve_publish_cover(
    session_id: str,
    client_user_id: str | None = Query(None),
    download: int | None = Query(None),
):
    """投稿封面图：鉴权后返回 PNG。"""
    if not await task_manager.verify_access(session_id, client_user_id):
        raise HTTPException(status_code=403, detail="无权访问该媒体文件")
    cover_path = bilibili_uploader.publish_cover_path(CACHE_DIR, session_id)
    if not os.path.isfile(cover_path):
        raise HTTPException(status_code=404, detail="封面尚未生成")
    headers = {
        "Cache-Control": "private, max-age=3600",
    }
    return FileResponse(
        cover_path,
        media_type="image/png",
        filename=f"{session_id}_cover.png",
        content_disposition_type="attachment" if download else "inline",
        headers=headers,
    )


@app.post("/task/{task_id}/publish_bilibili")
async def publish_task_to_bilibili(
    task_id: str,
    req: BilibiliPublishRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    client_user_id: str | None = Query(None),
):
    """将任务对应的字幕成片一键投稿到哔哩哔哩（后台异步上传）。"""
    requester = _normalize_client_user_id(client_user_id) or _normalize_client_user_id(
        req.client_user_id
    )
    # 优先登录身份；匿名仍可用 client_user_id 做归属校验
    _auth_user, owner_user_id = await resolve_request_auth_optional(request)
    if owner_user_id:
        requester = owner_user_id

    if not await task_manager.verify_access(task_id, requester):
        raise HTTPException(status_code=403, detail="无权访问该任务")

    subtitled_path = os.path.join(CACHE_DIR, f"{task_id}_subtitled.mp4")
    if not os.path.isfile(subtitled_path):
        raise HTTPException(status_code=404, detail="字幕视频尚未生成，无法投稿")

    existing = await _get_bili_publish_job(task_id)
    if existing and existing.get("status") == "publishing":
        raise HTTPException(status_code=409, detail="该任务正在投稿中，请稍候")

    task_row = upload_task_store.get_task(UPLOAD_TASKS_DB_PATH, task_id) or {}
    file_name = task_row.get("file_name") or f"{task_id}.mp4"
    title = bilibili_uploader.sanitize_title(
        (req.title or "").strip() or _default_publish_title_from_filename(file_name)
    )
    desc = (req.desc or "").strip()
    tags = (req.tags or "").strip() or None
    tid = req.tid
    copyright = req.copyright
    source = (req.source or "").strip() or None

    # 启动前粗检凭证，避免用户点了按钮才发现未配置
    cfg = bilibili_downloader.load_config()
    cookie_map = bilibili_downloader.load_cookie_map(cfg)
    sessdata = cookie_map.get("SESSDATA")
    bili_jct = cookie_map.get("bili_jct")
    cookie_path = bilibili_downloader.resolve_cookie_file_path(cfg)
    if not sessdata:
        detail = (
            f"未配置 B 站登录凭证：cookie_file={cookie_path or '(未设置)'} 中缺少 SESSDATA。"
            "请在 bilibili_config.json 设置 cookie_file 指向 Netscape cookie 文件。"
        )
        raise HTTPException(status_code=400, detail=detail)
    if not bili_jct:
        detail = (
            f"未配置 bili_jct：cookie_file={cookie_path or '(未设置)'} 中缺少 bili_jct。"
            "投稿需要 CSRF token，请更新 cookie 文件或在配置中填写 bili_jct。"
        )
        raise HTTPException(status_code=400, detail=detail)

    await _set_bili_publish_job(
        task_id,
        status="publishing",
        message="投稿任务已启动…",
        progress=0,
        title=title,
        bvid=None,
        aid=None,
        url=None,
        error=None,
    )
    record_client_click(
        action="publish_bilibili",
        client_user_id=requester,
        task_id=task_id,
        label="投稿到 Bilibili",
        file_name=file_name,
        source="api",
        meta={"title": title},
    )
    background_tasks.add_task(
        _run_bilibili_publish,
        task_id,
        subtitled_path,
        title=title,
        desc=desc,
        tags=tags,
        tid=tid,
        copyright=copyright,
        source=source,
    )
    return {
        "status": "publishing",
        "task_id": task_id,
        "title": title,
        "message": "投稿已开始，请稍候…",
    }


@app.get("/task/{task_id}/publish_bilibili/status")
async def publish_task_to_bilibili_status(
    task_id: str,
    request: Request,
    client_user_id: str | None = Query(None),
):
    """查询字幕成片投稿进度。"""
    requester = _normalize_client_user_id(client_user_id)
    _auth_user, owner_user_id = await resolve_request_auth_optional(request)
    if owner_user_id:
        requester = owner_user_id
    if not await task_manager.verify_access(task_id, requester):
        raise HTTPException(status_code=403, detail="无权访问该任务")

    job = await _get_bili_publish_job(task_id)
    if not job:
        return {"task_id": task_id, "status": "idle"}
    return job


@app.get("/media/{session_id}")
async def serve_task_media(session_id: str, client_user_id: str | None = Query(None)):
    if not await task_manager.verify_access(session_id, client_user_id):
        raise HTTPException(status_code=403, detail="无权访问该媒体文件")
    resolved = resolve_task_media_path(session_id)
    if not resolved:
        raise HTTPException(status_code=404, detail="媒体文件不存在")
    media_path, media_type = resolved
    return FileResponse(
        media_path,
        media_type=media_type,
        headers={"Accept-Ranges": "bytes", "Cache-Control": "private, no-cache, must-revalidate"},
    )


# 【新增】接收 Worker 发来的 SSE 事件
class PushEventReq(BaseModel):
    session_id: str
    event_type: str
    data: dict

@app.post("/internal/sync_session_artifacts")
async def sync_session_artifacts(
    request: Request,
    session_id: str = Query(...),
    archive: UploadFile = File(...),
):
    worker_token = request.headers.get("X-Internal-Worker-Token", "")
    if INTERNAL_WORKER_TOKEN and worker_token != INTERNAL_WORKER_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")

    session_seg_dir = os.path.join(SEGMENT_DIR, session_id)
    os.makedirs(session_seg_dir, exist_ok=True)
    content = await archive.read()
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            for name in zf.namelist():
                if name.endswith("/") or ".." in name.replace("\\", "/"):
                    continue
                base = os.path.basename(name)
                if not base:
                    continue
                dest = os.path.join(session_seg_dir, base)
                with zf.open(name) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    except zipfile.BadZipFile as e:
        raise HTTPException(status_code=400, detail=f"Invalid artifact archive: {e}") from e

    server_log.logger.info(f"[{session_id}] Session artifacts synced to gateway")
    return {"status": "ok", "session_id": session_id}


@app.post("/internal/push_event")
async def receive_push_event(req: PushEventReq):
    # pull 模式下网关已通过轮询消费 worker 事件，忽略回调避免重复触发字幕压制。
    if WORKER_EVENT_MODE == "pull":
        return {"status": "ok", "ignored": True}
    data = req.data if isinstance(req.data, dict) else {}
    await dispatch_worker_event(req.session_id, req.event_type, data)
    return {"status": "ok"}

@app.get("/task_status/{task_id}")
async def task_status(task_id: str, client_user_id: str | None = Query(None)):
    if not await task_manager.verify_access(task_id, client_user_id):
        raise HTTPException(status_code=403, detail="无权访问该任务")
    return await task_manager.get_status(task_id)


@app.get("/task_segment_results/{task_id}")
async def task_segment_results(
    task_id: str,
    client_user_id: str | None = Query(None),
    ui_language: str | None = Query(None),
):
    """Return persisted ASR segments (with translations) for completed offline tasks."""
    if not await task_manager.verify_access(task_id, client_user_id):
        raise HTTPException(status_code=403, detail="无权访问该任务")
    final_results_list = _load_final_results_list(task_id)
    if final_results_list:
        from subtitles.i18n import normalize_ui_language
        from subtitles.segment_translation import needs_segment_translation, translate_results_list

        normalized_ui_language = normalize_ui_language(ui_language or "zh-CN")
        if needs_segment_translation(final_results_list, "unknown", normalized_ui_language):
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(
                    None,
                    translate_results_list,
                    final_results_list,
                    "unknown",
                    normalized_ui_language,
                )
                _save_final_results_list(task_id, final_results_list)
            except Exception as e:
                server_log.logger.error(f"[{task_id}] task_segment_results translation failed: {e}")
    segments = []
    for item in final_results_list:
        if not isinstance(item, dict):
            continue
        text = (item.get("text") or "").strip()
        translation = (item.get("translation") or "").strip()
        if not text and not translation:
            continue
        segments.append({
            "index": item.get("index"),
            "timestamp": item.get("timestamp"),
            "text": text,
            "translation": translation if translation and translation != text else None,
            "speaker": item.get("speaker"),
        })
    return {"segments": segments}


@app.get("/task_media_info/{task_id}")
async def task_media_info(task_id: str, client_user_id: str | None = Query(None)):
    """查询任务媒体产物（不依赖内存中的 SSE 任务），用于前端重新打开页面时恢复字幕视频。"""
    if not await task_manager.verify_access(task_id, client_user_id):
        raise HTTPException(status_code=403, detail="无权访问该任务")
    subtitled_path = os.path.join(CACHE_DIR, f"{task_id}_subtitled.mp4")
    subtitled_available = os.path.isfile(subtitled_path)
    return {
        "subtitled_available": subtitled_available,
        "video_url": f"/media/{task_id}/subtitled" if subtitled_available else None,
    }


@app.get("/task/{task_id}/summary")
async def task_summary(
    task_id: str,
    client_user_id: str | None = Query(None),
    ui_language: str | None = Query(None),
):
    """生成/读取视频转写摘要（首个请求即生成并落盘缓存，重开读缓存）。"""
    if not await task_manager.verify_access(task_id, client_user_id):
        raise HTTPException(status_code=403, detail="无权访问该任务")
    final_results_list = _load_final_results_list(task_id)
    if not final_results_list:
        raise HTTPException(status_code=404, detail="未找到识别结果，无法生成摘要")
    try:
        result = await rag.get_or_build_summary(
            task_id, final_results_list, ui_language=ui_language or "zh-CN"
        )
    except Exception as e:
        server_log.logger.error(f"[{task_id}] 摘要生成失败: {e}")
        raise HTTPException(status_code=503, detail="摘要生成失败，请稍后重试")
    return result


class RagChatRequest(BaseModel):
    question: str
    history: list[dict] | None = None
    ui_language: str | None = None


class RagChatHistoryRequest(BaseModel):
    messages: list[dict] | None = None


@app.get("/task/{task_id}/chat")
async def get_task_chat(
    task_id: str,
    client_user_id: str | None = Query(None),
):
    """读取该任务已保存的问答对话历史。"""
    if not await task_manager.verify_access(task_id, client_user_id):
        raise HTTPException(status_code=403, detail="无权访问该任务")
    return {"messages": rag.load_chat_history(task_id)}


@app.put("/task/{task_id}/chat")
async def put_task_chat(
    task_id: str,
    payload: RagChatHistoryRequest,
    client_user_id: str | None = Query(None),
):
    """覆盖保存该任务的问答对话历史（例如清空）。"""
    if not await task_manager.verify_access(task_id, client_user_id):
        raise HTTPException(status_code=403, detail="无权访问该任务")
    messages = rag.save_chat_history(task_id, payload.messages or [])
    return {"messages": messages}


@app.post("/task/{task_id}/chat")
async def task_chat(
    task_id: str,
    payload: RagChatRequest,
    client_user_id: str | None = Query(None),
):
    """就视频转写内容进行 RAG 问答，SSE 流式返回；完成后持久化本轮对话。"""
    if not await task_manager.verify_access(task_id, client_user_id):
        raise HTTPException(status_code=403, detail="无权访问该任务")
    question = (payload.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    final_results_list = _load_final_results_list(task_id)
    if not final_results_list:
        raise HTTPException(status_code=404, detail="未找到识别结果，无法问答")

    async def event_generator():
        answer_parts: list[str] = []
        try:
            got_any = False
            async for delta in rag.answer_question_stream(
                task_id,
                final_results_list,
                question,
                history=payload.history,
                ui_language=payload.ui_language or "zh-CN",
            ):
                got_any = True
                if delta:
                    answer_parts.append(delta)
                yield f"data: {json.dumps({'type': 'delta', 'text': delta}, ensure_ascii=False)}\n\n"
            if not got_any:
                yield f"data: {json.dumps({'type': 'delta', 'text': ''}, ensure_ascii=False)}\n\n"
            try:
                rag.append_chat_turn(task_id, question, "".join(answer_parts))
            except Exception as save_err:
                server_log.logger.warning(f"[{task_id}] 保存问答历史失败: {save_err}")
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            server_log.logger.error(f"[{task_id}] RAG 问答失败: {e}")
            yield f"data: {json.dumps({'type': 'error', 'message': '问答服务异常，请稍后重试'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


class SubtitleEditSegment(BaseModel):
    index: int
    text: str
    translation: str | None = None


class SubtitleEditRequest(BaseModel):
    segments: list[SubtitleEditSegment]
    ui_language: str | None = None


@app.post("/task/{task_id}/subtitles/edit")
async def edit_task_subtitles(
    task_id: str,
    payload: SubtitleEditRequest,
    client_user_id: str | None = Query(None),
):
    """编辑 AI 生成的字幕（原文/译文），保存到 final_result.json 并重新刻印视频。

    仅对传入的片段覆盖 text/translation，并将其 sub_segments 合并为单条字幕条目
    （时间跨度沿用该段的 start_ts/end_ts），未传入的片段保留原有细粒度 sub_segments。
    """
    if not await task_manager.verify_access(task_id, client_user_id):
        raise HTTPException(status_code=403, detail="无权访问该任务")

    final_results_list = _load_final_results_list(task_id)
    if not final_results_list:
        raise HTTPException(status_code=404, detail="未找到识别结果，无法编辑字幕")

    edits = [seg.model_dump() for seg in payload.segments]
    edit_indices = {e["index"] for e in edits if e.get("index") is not None}
    changed_count = sum(
        1
        for item in final_results_list
        if isinstance(item, dict) and item.get("index") in edit_indices
    )

    if changed_count == 0:
        raise HTTPException(status_code=400, detail="没有匹配的片段可编辑")

    # 与草稿预览走同一套 apply_subtitle_edits，保证保存后与预览形态一致
    final_results_list = apply_subtitle_edits(final_results_list, edits)

    _save_final_results_list(task_id, final_results_list)

    # 取原始文件名用于定位源视频
    task_row = upload_task_store.get_task(UPLOAD_TASKS_DB_PATH, task_id)
    original_filename = (task_row or {}).get("file_name") or ""
    ui_language = payload.ui_language or "zh-CN"

    await task_manager.broadcast(task_id, {
        "type": "subtitle_reburn_progress",
        "message": "正在重新刻印字幕视频……",
    })

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None,
            auto_generate_subtitle_video_sync,
            task_id,
            original_filename,
            ui_language,
        )
    except Exception as e:
        server_log.logger.error(f"[{task_id}] 字幕重新刻印失败: {e}")
        await task_manager.broadcast(task_id, {
            "type": "error",
            "message": f"字幕视频重新刻印失败：{e}",
        })
        raise HTTPException(status_code=500, detail="字幕视频重新刻印失败")

    video_url = result.get("url")
    if not video_url:
        await task_manager.broadcast(task_id, {
            "type": "error",
            "message": "字幕视频重新刻印失败，请稍后重试。",
        })
        raise HTTPException(status_code=500, detail="字幕视频重新刻印失败")

    canonical_url = f"/media/{task_id}/subtitled"
    _update_upload_task_row(task_id, video_url=canonical_url, status="done")

    await task_manager.broadcast(task_id, {
        "type": "subtitle_updated",
        "video_url": canonical_url,
        "final_segments": _final_segments_payload(final_results_list),
        "message": "字幕已更新并重新刻印完成",
    })

    return {
        "ok": True,
        "video_url": canonical_url,
        "changed_segments": changed_count,
    }


@app.get("/task/{task_id}/subtitles/export")
async def export_task_subtitles(
    task_id: str,
    client_user_id: str | None = Query(None),
    ui_language: str | None = Query(None),
):
    """旁路导出当前字幕文件（SRT 或泰语 ASS），内容反映最新 final_result.json（含用户编辑）。

    无需先刻印视频即可下载；用于用户下载字幕文件自行使用或校正。
    """
    if not await task_manager.verify_access(task_id, client_user_id):
        raise HTTPException(status_code=403, detail="无权访问该任务")

    content, ext = await asyncio.get_running_loop().run_in_executor(
        None,
        export_subtitle_content_for_session,
        task_id,
        ui_language,
    )
    if not content:
        raise HTTPException(status_code=404, detail="暂无可导出的字幕内容")

    media_type = "application/x-subrip" if ext == "srt" else "text/plain"
    # 文件名沿用任务原始名（去扩展）+ .srt/.ass
    task_row = upload_task_store.get_task(UPLOAD_TASKS_DB_PATH, task_id)
    base_name = (task_row or {}).get("file_name") or task_id
    stem = os.path.splitext(os.path.basename(base_name))[0] or task_id
    download_name = f"{stem}.{ext}"
    return Response(
        content=content.encode("utf-8-sig" if ext == "ass" else "utf-8"),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )


@app.get("/task/{task_id}/subtitles/preview.vtt")
async def preview_task_subtitles_vtt(
    task_id: str,
    client_user_id: str | None = Query(None),
):
    """播放器实时预览用的 WebVTT 字幕，反映最新 final_result.json（含用户编辑）。

    与烧录成片走同一套 build_subtitle_entries 产出，保证预览形态与最终一致。
    """
    if not await task_manager.verify_access(task_id, client_user_id):
        raise HTTPException(status_code=403, detail="无权访问该任务")

    content = await asyncio.get_running_loop().run_in_executor(
        None,
        export_subtitle_vtt_for_session,
        task_id,
    )
    if not content:
        # 无字幕内容时返回空的合法 VTT，避免播放器报轨道加载错误
        content = "WEBVTT\n\n"
    return Response(
        content=content.encode("utf-8"),
        media_type="text/vtt; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/task/{task_id}/subtitles/preview.vtt")
async def preview_task_subtitles_vtt_draft(
    task_id: str,
    payload: SubtitleEditRequest,
    client_user_id: str | None = Query(None),
):
    """未保存草稿的 WebVTT 实时预览：在内存副本上应用编辑，不落盘、不刻印。

    与最终烧录同源，用于用户编辑过程中即时查看字幕在视频上的真实形态。
    """
    if not await task_manager.verify_access(task_id, client_user_id):
        raise HTTPException(status_code=403, detail="无权访问该任务")

    edits = [seg.model_dump() for seg in payload.segments]
    content = await asyncio.get_running_loop().run_in_executor(
        None,
        export_subtitle_vtt_for_draft,
        task_id,
        edits,
    )
    if not content:
        content = "WEBVTT\n\n"
    return Response(
        content=content.encode("utf-8"),
        media_type="text/vtt; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


class SubtitleCue(BaseModel):
    start: float
    end: float
    text: str = ""
    trans: str | None = None


class SubtitleCuesRequest(BaseModel):
    cues: list[SubtitleCue]
    ui_language: str | None = None


def _cues_payload(cues: list[SubtitleCue]) -> list[dict]:
    return [
        {
            "start": float(c.start),
            "end": float(c.end),
            "text": c.text or "",
            "trans": c.trans or "",
        }
        for c in cues
    ]


@app.get("/task/{task_id}/subtitles/cues")
async def get_task_subtitle_cues(
    task_id: str,
    client_user_id: str | None = Query(None),
):
    """返回可编辑的字幕条（cue）列表：来自强制对齐模型的真实音画对齐时间轴。

    每条含 start/end（秒）+ text（原文）+ trans（译文），供前端时间轴编辑器使用。
    """
    if not await task_manager.verify_access(task_id, client_user_id):
        raise HTTPException(status_code=403, detail="无权访问该任务")

    cues, max_end = await asyncio.get_running_loop().run_in_executor(
        None,
        build_cue_list_for_session,
        task_id,
    )
    return {"cues": cues, "duration": max_end}


@app.post("/task/{task_id}/subtitles/cues/preview.vtt")
async def preview_task_subtitle_cues_vtt(
    task_id: str,
    payload: SubtitleCuesRequest,
    client_user_id: str | None = Query(None),
):
    """时间轴编辑器草稿的 WebVTT 实时预览：按传入 cue 列表渲染，不落盘、不刻印。"""
    if not await task_manager.verify_access(task_id, client_user_id):
        raise HTTPException(status_code=403, detail="无权访问该任务")

    cues = _cues_payload(payload.cues)
    content = await asyncio.get_running_loop().run_in_executor(
        None,
        export_cues_vtt_draft,
        task_id,
        cues,
    )
    if not content:
        content = "WEBVTT\n\n"
    return Response(
        content=content.encode("utf-8"),
        media_type="text/vtt; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/task/{task_id}/subtitles/cues/save")
async def save_task_subtitle_cues(
    task_id: str,
    payload: SubtitleCuesRequest,
    client_user_id: str | None = Query(None),
):
    """保存时间轴编辑器的 cue 列表到 final_result.json 并重新刻印字幕视频。

    直接沿用用户设定的每条起止时间（严格按时间轴，而非按字数权重均摊）。
    """
    if not await task_manager.verify_access(task_id, client_user_id):
        raise HTTPException(status_code=403, detail="无权访问该任务")

    final_results_list = _load_final_results_list(task_id)
    cues = _cues_payload(payload.cues)
    if not cues:
        raise HTTPException(status_code=400, detail="字幕内容为空，无法保存")

    rebuilt = rebuild_data_with_cues(final_results_list, cues)
    _save_final_results_list(task_id, rebuilt)

    task_row = upload_task_store.get_task(UPLOAD_TASKS_DB_PATH, task_id)
    original_filename = (task_row or {}).get("file_name") or ""
    ui_language = payload.ui_language or "zh-CN"

    await task_manager.broadcast(task_id, {
        "type": "subtitle_reburn_progress",
        "message": "正在重新刻印字幕视频……",
    })

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None,
            auto_generate_subtitle_video_sync,
            task_id,
            original_filename,
            ui_language,
        )
    except Exception as e:
        server_log.logger.error(f"[{task_id}] 字幕重新刻印失败: {e}")
        await task_manager.broadcast(task_id, {
            "type": "error",
            "message": f"字幕视频重新刻印失败：{e}",
        })
        raise HTTPException(status_code=500, detail="字幕视频重新刻印失败")

    video_url = result.get("url")
    if not video_url:
        await task_manager.broadcast(task_id, {
            "type": "error",
            "message": "字幕视频重新刻印失败，请稍后重试。",
        })
        raise HTTPException(status_code=500, detail="字幕视频重新刻印失败")

    canonical_url = f"/media/{task_id}/subtitled"
    _update_upload_task_row(task_id, video_url=canonical_url, status="done")

    await task_manager.broadcast(task_id, {
        "type": "subtitle_updated",
        "video_url": canonical_url,
        "final_segments": _final_segments_payload(rebuilt),
        "message": "字幕已更新并重新刻印完成",
    })

    return {"ok": True, "video_url": canonical_url, "cue_count": len(cues)}


class ClientClickLogRequest(BaseModel):
    client_time: str | None = None
    client_user_id: str | None = None
    user_tag: str | None = None
    action: str
    label: str | None = None
    tab: str | None = None
    task_id: str | None = None
    file_name: str | None = None
    path: str | None = None
    meta: dict | None = None


class ScnetOAuthCallbackRequest(BaseModel):
    code: str


class ScnetSessionRequest(BaseModel):
    access_token: str


SCNET_OAUTH_SILENT_STATE = "silent_sso"
LOCAL_AUTH_DB_PATH = init_local_auth_db()
UPLOAD_TASKS_DB_PATH = upload_task_store.init_db(LOCAL_AUTH_DB_PATH)
server_log.logger.info(f"[Auth] Local auth DB ready: {LOCAL_AUTH_DB_PATH}")
server_log.logger.info(f"[Auth] Upload tasks DB ready: {UPLOAD_TASKS_DB_PATH}")


def owner_user_id_from_auth_user(user: dict) -> str:
    """Match frontend getOrCreateClientUserId for authenticated users: scnet:{userId}."""
    user_id = str((user or {}).get("userId") or "").strip()
    if not user_id:
        raise ValueError("missing userId")
    if user_id.startswith("scnet:"):
        return user_id
    return f"scnet:{user_id}"


def _extract_bearer_token(request: Request) -> str | None:
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        return token or None
    return None


async def resolve_request_auth(request: Request) -> tuple[dict, str]:
    """Validate Authorization bearer and return (auth_user, owner_user_id)."""
    token = _extract_bearer_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="未登录或缺少访问令牌")

    if token.startswith("local_"):
        user = validate_local_session(token)
        if not user:
            raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
        user = {**user, "isAdmin": is_admin_user(user)}
        _persist_user_profile(user)
        return user, owner_user_id_from_auth_user(user)

    try:
        user_payload = await asyncio.to_thread(fetch_scnet_userinfo, token)
        user = _normalize_scnet_user(user_payload)
    except HTTPException:
        raise
    except Exception as e:
        server_log.logger.warning(f"[Auth] Bearer token validation failed: {e}")
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录") from e
    _persist_user_profile(user)
    return user, owner_user_id_from_auth_user(user)


async def resolve_request_auth_optional(request: Request) -> tuple[dict | None, str | None]:
    """Like resolve_request_auth but允许匿名：无令牌时返回 (None, None)。

    有令牌但无效仍会抛 401（避免带着坏令牌被静默降级）。
    """
    if not _extract_bearer_token(request):
        return None, None
    return await resolve_request_auth(request)


def _upload_task_api_item(row: dict) -> dict:
    return {
        "task_id": row.get("task_id"),
        "file_name": row.get("file_name") or "",
        "status": row.get("status") or "pending",
        "message": row.get("message") or "",
        "created_at": row.get("created_at"),
        "file_url": row.get("file_url"),
        "original_file_url": row.get("original_file_url"),
        "video_url": row.get("video_url"),
        "media_duration_seconds": row.get("media_duration_seconds"),
        "detected_lang": row.get("detected_lang"),
        "detected_lang_name": row.get("detected_lang_name"),
        "speaker_stats": row.get("speaker_stats"),
    }


def _persist_upload_task_row(**kwargs):
    try:
        return upload_task_store.upsert_task(UPLOAD_TASKS_DB_PATH, **kwargs)
    except PermissionError as e:
        server_log.logger.warning(f"[UploadTasks] upsert denied: {e}")
        return None
    except Exception as e:
        server_log.logger.warning(f"[UploadTasks] upsert failed: {e}")
        return None


def _update_upload_task_row(task_id: str, **fields):
    try:
        owner = upload_task_store.get_task_owner(UPLOAD_TASKS_DB_PATH, task_id)
        if not owner:
            owner = session_owners.get(task_id)
        if not owner:
            return None
        return upload_task_store.update_task_fields(
            UPLOAD_TASKS_DB_PATH,
            task_id,
            user_id=owner,
            **fields,
        )
    except PermissionError as e:
        server_log.logger.warning(f"[UploadTasks] update denied task={task_id}: {e}")
        return None
    except Exception as e:
        server_log.logger.warning(f"[UploadTasks] update failed task={task_id}: {e}")
        return None


def _normalize_scnet_user(payload: dict) -> dict:
    user_id = payload.get("userId")
    if user_id is None:
        raise ValueError("missing userId")
    user = {
        "userId": str(user_id),
        "userName": payload.get("userName") or None,
        "fullName": payload.get("fullName") or None,
        "email": payload.get("email") or None,
        "mobile": payload.get("mobile") or None,
    }
    user["isAdmin"] = is_admin_user(user)
    return user


def _persist_user_profile(user: dict) -> None:
    try:
        owner_id = owner_user_id_from_auth_user(user)
    except ValueError:
        return
    try:
        upload_task_store.upsert_user_profile(
            UPLOAD_TASKS_DB_PATH,
            user_id=owner_id,
            user_name=user.get("userName"),
            full_name=user.get("fullName"),
            email=user.get("email"),
        )
    except Exception as e:
        server_log.logger.warning(f"[Auth] persist user profile failed: {e}")


@app.get("/api/auth/scnet/config")
async def scnet_auth_config(request: Request, silent: bool = Query(False)):
    redirect_uri = resolve_scnet_redirect_uri(
        GATEWAY_PUBLIC_URL,
        resolve_public_url_from_request(request),
    )
    return {
        "client_id": SCNET_OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "authorize_url": build_scnet_authorize_url(
            redirect_uri,
            silent=silent,
            state=SCNET_OAUTH_SILENT_STATE if silent else None,
        ),
    }


@app.post("/api/auth/scnet/callback")
async def scnet_auth_callback(req: ScnetOAuthCallbackRequest, request: Request):
    code = (req.code or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="缺少授权码")
    redirect_uri = resolve_scnet_redirect_uri(
        GATEWAY_PUBLIC_URL,
        resolve_public_url_from_request(request),
    )
    try:
        token_payload = await asyncio.to_thread(exchange_scnet_code_for_tokens, code, redirect_uri)
        access_token = str(token_payload.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError("SCNet token exchange returned empty access_token")
        user_payload = await asyncio.to_thread(fetch_scnet_userinfo, access_token)
        user = _normalize_scnet_user(user_payload)
    except HTTPException:
        raise
    except Exception as e:
        server_log.logger.warning(f"[Auth] SCNet OAuth callback failed: {e}")
        raise HTTPException(status_code=502, detail="超算互联网登录失败，请重试") from e

    server_log.logger.info(
        f"[Auth] SCNet login success userId={user.get('userId')} userName={user.get('userName')}"
    )
    _persist_user_profile(user)
    return {"status": "ok", "user": user, "access_token": access_token}


@app.post("/api/auth/scnet/session")
async def scnet_validate_session(req: ScnetSessionRequest):
    access_token = (req.access_token or "").strip()
    if not access_token:
        raise HTTPException(status_code=400, detail="缺少 access_token")
    try:
        user_payload = await asyncio.to_thread(fetch_scnet_userinfo, access_token)
        user = _normalize_scnet_user(user_payload)
    except HTTPException:
        raise
    except Exception as e:
        server_log.logger.warning(f"[Auth] SCNet session validation failed: {e}")
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录") from e
    _persist_user_profile(user)
    return {"status": "ok", "user": user}


class UploadTaskMigrateItem(BaseModel):
    task_id: str
    file_name: str | None = None
    status: str | None = None
    message: str | None = None
    created_at: str | None = None
    file_url: str | None = None
    original_file_url: str | None = None
    video_url: str | None = None
    media_duration_seconds: float | None = None
    detected_lang: str | None = None
    detected_lang_name: str | None = None
    speaker_stats: list | None = None


class UploadTaskMigrateRequest(BaseModel):
    tasks: list[UploadTaskMigrateItem] = []


@app.get("/api/me/upload-tasks")
async def api_list_my_upload_tasks(request: Request, limit: int = Query(200, ge=1, le=500)):
    _, owner_user_id = await resolve_request_auth(request)
    rows = await asyncio.to_thread(
        upload_task_store.list_tasks_for_user,
        UPLOAD_TASKS_DB_PATH,
        owner_user_id,
        limit=limit,
    )
    return {"tasks": [_upload_task_api_item(row) for row in rows]}


@app.delete("/api/me/upload-tasks/{task_id}")
async def api_delete_my_upload_task(task_id: str, request: Request):
    _, owner_user_id = await resolve_request_auth(request)
    deleted = await asyncio.to_thread(
        upload_task_store.delete_task,
        UPLOAD_TASKS_DB_PATH,
        task_id,
        user_id=owner_user_id,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="任务不存在或无权删除")
    return {"status": "ok", "task_id": task_id}


@app.post("/api/me/upload-tasks/migrate")
async def api_migrate_my_upload_tasks(req: UploadTaskMigrateRequest, request: Request):
    """One-time import of legacy localStorage task metadata for the current user."""
    _, owner_user_id = await resolve_request_auth(request)
    imported = 0
    skipped = 0
    for item in req.tasks or []:
        task_id = (item.task_id or "").strip()
        if not task_id or not split_session_id_parts(task_id):
            skipped += 1
            continue
        owner = session_owners.get(task_id) or upload_task_store.get_task_owner(
            UPLOAD_TASKS_DB_PATH, task_id
        )
        if owner and owner != owner_user_id:
            skipped += 1
            continue
        status = (item.status or "done").strip() or "done"
        if status not in {"pending", "processing", "done", "error"}:
            status = "done"
        try:
            upload_task_store.upsert_task(
                UPLOAD_TASKS_DB_PATH,
                task_id=task_id,
                user_id=owner_user_id,
                file_name=(item.file_name or task_id).strip() or task_id,
                status=status,
                message=item.message or "",
                created_at=item.created_at,
                file_url=item.file_url,
                original_file_url=item.original_file_url,
                video_url=item.video_url,
                media_duration_seconds=item.media_duration_seconds,
                detected_lang=item.detected_lang,
                detected_lang_name=item.detected_lang_name,
                speaker_stats=item.speaker_stats,
            )
            if task_id not in session_owners:
                session_owners[task_id] = owner_user_id
            imported += 1
        except PermissionError:
            skipped += 1
        except Exception as e:
            server_log.logger.warning(f"[UploadTasks] migrate failed task={task_id}: {e}")
            skipped += 1
    if imported:
        persist_session_owners()
    return {"status": "ok", "imported": imported, "skipped": skipped}


@app.get("/api/admin/stats")
async def api_admin_stats(request: Request):
    user, _ = await resolve_request_auth(request)
    if not is_admin_user(user):
        raise HTTPException(status_code=403, detail="无权访问运营数据")
    stats = await asyncio.to_thread(upload_task_store.get_usage_stats, UPLOAD_TASKS_DB_PATH)
    return stats


@app.post("/api/click_log")
async def api_click_log(req: ClientClickLogRequest):
    action = (req.action or "").strip()
    if not action:
        raise HTTPException(status_code=400, detail="action is required")
    record_client_click(
        action=action,
        client_user_id=req.client_user_id,
        client_time=req.client_time,
        label=req.label,
        tab=req.tab,
        task_id=req.task_id,
        file_name=req.file_name,
        path=req.path,
        meta=req.meta,
        source="frontend",
    )
    return {"status": "ok"}

@app.get("/stream_task/{task_id}")
async def stream_task_events(task_id: str, client_user_id: str | None = Query(None)):
    if not await task_manager.verify_access(task_id, client_user_id):
        async def forbidden_gen():
            yield f"data: {json.dumps({'type': 'error', 'message': '无权访问该任务'})}\n\n"
        return StreamingResponse(forbidden_gen(), media_type="text/event-stream")
    history, q = await task_manager.subscribe(task_id)
    if history is None:
        async def error_gen():
            yield f"data: {json.dumps({'type': 'error', 'message': 'Task not found'})}\n\n"
        return StreamingResponse(error_gen(), media_type="text/event-stream")

    async def event_generator():
        try:
            for msg_str in history:
                yield f"data: {msg_str}\n\n"
            while True:
                try:
                    data_str = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {data_str}\n\n"
                    if json.loads(data_str).get("type") in ["done", "error"]:
                        break
                except asyncio.TimeoutError:
                    # 长视频压字幕/转码阶段可能数分钟没有业务消息，发心跳防止 SSE 被代理或浏览器断开。
                    yield ": keep-alive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            await task_manager.unsubscribe(task_id, q)
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gateway Server")
    parser.add_argument("--port", type=int, default=GATEWAY_PORT, help="HTTP port for the gateway server")
    args = parser.parse_args()

    uvicorn.run(app, host="0.0.0.0", port=args.port)
