"""视频转写摘要 + RAG 问答。

复用 subtitles/llm_stages.py 的风格：aiohttp + resolve_*_url + parse_llm_json。
向量检索仅用 numpy 做余弦（无 faiss / sentence-transformers 依赖）。
摘要与向量索引落盘缓存于 CACHE_DIR/rag/{task_id}/，以 final_result.json 的 mtime 作失效判据。
"""

import asyncio
import json
import os

import aiohttp
import numpy as np

from subtitles import config
from subtitles._runtime import get_logger, resolve_embed_url, resolve_llm_url
from subtitles.i18n import normalize_ui_language, ui_language_name_from_code
from subtitles.llm_stages import parse_llm_json

# ---- 可调参数 ----
RAG_CHUNK_CHARS = int(os.environ.get("RAG_CHUNK_CHARS", "500"))
RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "5"))
RAG_EMBED_BATCH = int(os.environ.get("RAG_EMBED_BATCH", "32"))
# 全文短于该阈值时跳过检索，直接把整段转写塞进提示
RAG_STUFF_ALL_CHARS = int(os.environ.get("RAG_STUFF_ALL_CHARS", "12000"))
# 回退塞全文时的安全上限（防止超出 LLM 上下文）
RAG_MAX_CONTEXT_CHARS = int(os.environ.get("RAG_MAX_CONTEXT_CHARS", "24000"))
# 摘要一次性可直接喂入的最大字符数，超出则按块 map-reduce
SUMMARY_MAX_STUFF_CHARS = int(os.environ.get("SUMMARY_MAX_STUFF_CHARS", "20000"))

EMBED_MODEL = os.environ.get("EMBED_MODEL", "embed")
LLM_MODEL = os.environ.get("LLM_MODEL", "llm")

_RAG_CACHE_ROOT = os.path.join(config.CACHE_DIR, "rag")

# per-task 构建锁，避免并发重复构建索引/摘要
_task_locks: dict[str, asyncio.Lock] = {}
_task_locks_guard = asyncio.Lock()


class EmbedUnavailable(Exception):
    """embedding 服务不可用（未配置或请求失败），上层据此回退塞全文。"""


# ---- Prompt 常量 ----
SUMMARY_SYSTEM_PROMPT = (
    "你是专业的视频内容分析助手。请阅读下面的视频转写文本，输出结构化的{lang}摘要，"
    "包含：【核心主题】用一两句话概括；【关键要点】分条列出主要内容；"
    "【结论/建议】如文本中有则给出，否则省略。只依据文本，不要编造未提及的信息。"
)
SUMMARY_REDUCE_SYSTEM_PROMPT = (
    "你是专业的视频内容分析助手。下面是同一视频若干片段的分段摘要，请将它们整合成一份"
    "连贯的{lang}整体摘要，包含：【核心主题】【关键要点】（分条）【结论/建议】（如有）。"
    "只依据给定内容，不要编造。"
)
RAG_SYSTEM_PROMPT = (
    "你是视频问答助手。请仅根据下面提供的【转写片段】回答用户问题；"
    "若片段中没有相关信息，请明确说明未在视频中找到相关内容，不要编造。"
    "用{lang}回答，必要时可引用片段中的时间点。"
)


# ============================================================
# 分块
# ============================================================
def _iter_segment_items(final_results_list):
    for item in final_results_list or []:
        if not isinstance(item, dict):
            continue
        text = (item.get("text") or "").strip()
        if not text:
            continue
        yield item, text


def full_text_of(final_results_list) -> str:
    return "".join(text for _, text in _iter_segment_items(final_results_list))


def _fmt_ts(seconds) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "00:00"
    return f"{total // 60:02d}:{total % 60:02d}"


def _build_chunks(final_results_list) -> list[dict]:
    """把连续段落拼成 ~RAG_CHUNK_CHARS 字的块，不切断单段，块间重叠 1 段。"""
    items = list(_iter_segment_items(final_results_list))
    chunks: list[dict] = []
    cur_texts: list[str] = []
    cur_indices: list[int] = []
    cur_start = None
    cur_end = None
    cur_len = 0

    def flush():
        nonlocal cur_texts, cur_indices, cur_start, cur_end, cur_len
        if not cur_texts:
            return
        chunks.append({
            "text": "".join(cur_texts),
            "start_ts": cur_start,
            "end_ts": cur_end,
            "seg_indices": list(cur_indices),
        })

    for pos, (item, text) in enumerate(items):
        if not cur_texts:
            cur_start = item.get("start_ts")
        cur_texts.append(text)
        cur_indices.append(item.get("index", pos))
        cur_end = item.get("end_ts")
        cur_len += len(text)
        if cur_len >= RAG_CHUNK_CHARS:
            flush()
            # 重叠：保留最后一段作为下一块的开头
            cur_texts = [text]
            cur_indices = [item.get("index", pos)]
            cur_start = item.get("start_ts")
            cur_end = item.get("end_ts")
            cur_len = len(text)
    # 最后一块：若与上一块重叠段完全相同则跳过（仅剩重叠段）
    if cur_texts and not (len(cur_texts) == 1 and chunks and chunks[-1]["seg_indices"][-1:] == cur_indices):
        flush()
    return chunks


def _format_chunks_for_prompt(chunks: list[dict]) -> str:
    lines = []
    for ch in chunks:
        ts = f"[{_fmt_ts(ch.get('start_ts'))}-{_fmt_ts(ch.get('end_ts'))}]"
        lines.append(f"{ts} {ch['text']}")
    return "\n".join(lines)


# ============================================================
# LLM / embedding 客户端
# ============================================================
def _client_session():
    # 复用 llm_stages 的做法：trust_env=True 以尊重 HTTP(S)_PROXY，可达远端 vLLM
    return aiohttp.ClientSession(trust_env=True)


async def _embed_texts(texts: list[str], *, session_id: str = "") -> np.ndarray:
    """调用 OpenAI 兼容 /v1/embeddings，返回按行 L2 归一化的 float32 矩阵。"""
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    try:
        url = resolve_embed_url(session_id=session_id, route_source="rag.embed")
    except Exception as exc:  # EMBED_URL 未配置
        raise EmbedUnavailable(str(exc)) from exc

    vectors: list[list[float]] = []
    try:
        async with _client_session() as session:
            for start in range(0, len(texts), RAG_EMBED_BATCH):
                batch = texts[start:start + RAG_EMBED_BATCH]
                async with session.post(
                    url,
                    headers={"Content-Type": "application/json"},
                    json={"model": EMBED_MODEL, "input": batch},
                    timeout=aiohttp.ClientTimeout(total=60, connect=30),
                ) as resp:
                    if resp.status != 200:
                        raise EmbedUnavailable(f"embed HTTP {resp.status}")
                    payload = await resp.json()
                data = payload.get("data") or []
                if len(data) != len(batch):
                    raise EmbedUnavailable("embed 返回条数与输入不一致")
                for row in data:
                    vectors.append(row.get("embedding") or [])
    except EmbedUnavailable:
        raise
    except Exception as exc:
        raise EmbedUnavailable(str(exc)) from exc

    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[0] == 0:
        raise EmbedUnavailable("embed 返回为空")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


async def _llm_complete(system_prompt: str, user_prompt: str, *, session_id: str = "",
                        max_tokens: int = 800, temperature: float = 0.3) -> str:
    async with _client_session() as session:
        async with session.post(
            resolve_llm_url(session_id=session_id, route_source="rag.summary"),
            headers={"Content-Type": "application/json"},
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": temperature,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=aiohttp.ClientTimeout(total=180, connect=30),
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()
    return (payload["choices"][0]["message"]["content"] or "").strip()


# ============================================================
# 缓存
# ============================================================
def _task_cache_dir(task_id: str) -> str:
    return os.path.join(_RAG_CACHE_ROOT, task_id)


def _source_path(task_id: str) -> str:
    return os.path.join(config.SEGMENT_DIR, task_id, "final_result.json")


def _source_mtime(task_id: str) -> float:
    try:
        return os.path.getmtime(_source_path(task_id))
    except OSError:
        return 0.0


async def _get_task_lock(task_id: str) -> asyncio.Lock:
    async with _task_locks_guard:
        lock = _task_locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            _task_locks[task_id] = lock
        return lock


def invalidate_task_cache(task_id: str) -> None:
    """删除某任务的 RAG 缓存（摘要 + 索引）。字幕编辑会刷新 mtime 自动失效，
    此函数供需要主动清理的场景使用。对话历史 chat.json 不随索引失效清理。"""
    cache_dir = _task_cache_dir(task_id)
    for name in ("index.npz", "index.meta.json", "summary.json"):
        try:
            os.remove(os.path.join(cache_dir, name))
        except OSError:
            pass


# ============================================================
# 对话历史（按任务持久化，跨会话可见）
# ============================================================
_CHAT_MAX_MESSAGES = int(os.environ.get("RAG_CHAT_MAX_MESSAGES", "100"))


def _chat_path(task_id: str) -> str:
    return os.path.join(_task_cache_dir(task_id), "chat.json")


def _normalize_chat_messages(raw) -> list[dict]:
    messages: list[dict] = []
    if not isinstance(raw, list):
        return messages
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant"):
            continue
        if not isinstance(content, str):
            continue
        # 允许空 assistant 占位被过滤；持久化时只保留有内容的消息
        if not content.strip() and role == "assistant":
            continue
        messages.append({"role": role, "content": content})
    if len(messages) > _CHAT_MAX_MESSAGES:
        messages = messages[-_CHAT_MAX_MESSAGES:]
    return messages


def load_chat_history(task_id: str) -> list[dict]:
    path = _chat_path(task_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return []
    if isinstance(data, dict):
        return _normalize_chat_messages(data.get("messages"))
    return _normalize_chat_messages(data)


def save_chat_history(task_id: str, messages) -> list[dict]:
    normalized = _normalize_chat_messages(messages)
    cache_dir = _task_cache_dir(task_id)
    os.makedirs(cache_dir, exist_ok=True)
    path = _chat_path(task_id)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({"messages": normalized}, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
    return normalized


def append_chat_turn(task_id: str, question: str, answer: str) -> list[dict]:
    """追加一轮完整问答并写盘。"""
    question = (question or "").strip()
    answer = answer or ""
    if not question:
        return load_chat_history(task_id)
    messages = load_chat_history(task_id)
    messages.append({"role": "user", "content": question})
    messages.append({"role": "assistant", "content": answer})
    return save_chat_history(task_id, messages)


# ============================================================
# 摘要
# ============================================================
def load_cached_summary(task_id: str, *, ui_language: str | None = None) -> str:
    """读取已落盘摘要，不触发生成。mtime 失效或没有缓存时返回空串。

    ui_language 为 None 时不限制语言（封面等场景只需主题，不关心界面语言）。
    """
    mtime = _source_mtime(task_id)
    cache_path = os.path.join(_task_cache_dir(task_id), "summary.json")
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError, TypeError):
        return ""
    if not isinstance(meta, dict):
        return ""
    if meta.get("source_mtime") != mtime:
        return ""
    if ui_language is not None and meta.get("lang") != normalize_ui_language(ui_language):
        return ""
    return str(meta.get("summary") or "").strip()


async def _generate_summary(final_results_list, *, ui_language: str, session_id: str) -> str:
    lang_name = ui_language_name_from_code(ui_language)
    text = full_text_of(final_results_list)
    if not text.strip():
        return ""

    if len(text) <= SUMMARY_MAX_STUFF_CHARS:
        system = SUMMARY_SYSTEM_PROMPT.format(lang=lang_name)
        return await _llm_complete(system, text, session_id=session_id,
                                   max_tokens=800, temperature=0.3)

    # 超长：按块分段摘要后 reduce
    chunks = _build_chunks(final_results_list)
    partials: list[str] = []
    system = SUMMARY_SYSTEM_PROMPT.format(lang=lang_name)
    group: list[str] = []
    group_len = 0
    for ch in chunks:
        group.append(ch["text"])
        group_len += len(ch["text"])
        if group_len >= SUMMARY_MAX_STUFF_CHARS:
            partials.append(await _llm_complete(system, "".join(group),
                                                session_id=session_id, max_tokens=500))
            group, group_len = [], 0
    if group:
        partials.append(await _llm_complete(system, "".join(group),
                                            session_id=session_id, max_tokens=500))
    if len(partials) == 1:
        return partials[0]
    reduce_system = SUMMARY_REDUCE_SYSTEM_PROMPT.format(lang=lang_name)
    joined = "\n\n".join(f"片段摘要{i + 1}：\n{p}" for i, p in enumerate(partials))
    return await _llm_complete(reduce_system, joined, session_id=session_id, max_tokens=900)


async def get_or_build_summary(task_id: str, final_results_list, *, ui_language: str = "zh-CN") -> dict:
    ui_language = normalize_ui_language(ui_language)
    mtime = _source_mtime(task_id)
    cache_path = os.path.join(_task_cache_dir(task_id), "summary.json")

    def _read_valid_cache():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            return None
        if meta.get("source_mtime") == mtime and meta.get("lang") == ui_language and meta.get("summary"):
            return meta.get("summary")
        return None

    cached = _read_valid_cache()
    if cached is not None:
        return {"summary": cached, "cached": True}

    lock = await _get_task_lock(task_id)
    async with lock:
        # 取锁后复查：可能已被并发请求构建
        mtime = _source_mtime(task_id)
        cached = _read_valid_cache()
        if cached is not None:
            return {"summary": cached, "cached": True}

        summary = await _generate_summary(final_results_list, ui_language=ui_language, session_id=task_id)
        os.makedirs(_task_cache_dir(task_id), exist_ok=True)
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"source_mtime": mtime, "lang": ui_language, "summary": summary},
                          f, ensure_ascii=False)
        except OSError as exc:
            get_logger().warning(f"[{task_id}] 写摘要缓存失败: {exc}")
        return {"summary": summary, "cached": False}


# ============================================================
# 索引 + 检索
# ============================================================
async def get_or_build_index(task_id: str, final_results_list) -> tuple[np.ndarray, list[dict]]:
    """返回 (归一化向量矩阵, chunks)。embedding 不可用时抛 EmbedUnavailable。"""
    mtime = _source_mtime(task_id)
    cache_dir = _task_cache_dir(task_id)
    npz_path = os.path.join(cache_dir, "index.npz")
    meta_path = os.path.join(cache_dir, "index.meta.json")

    def _read_valid_cache():
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            return None
        if meta.get("source_mtime") != mtime:
            return None
        try:
            data = np.load(npz_path, allow_pickle=True)
        except (OSError, ValueError):
            return None
        return data["matrix"], list(data["chunks"])

    cached = _read_valid_cache()
    if cached is not None:
        return cached

    lock = await _get_task_lock(task_id)
    async with lock:
        mtime = _source_mtime(task_id)
        cached = _read_valid_cache()
        if cached is not None:
            return cached

        chunks = _build_chunks(final_results_list)
        if not chunks:
            raise EmbedUnavailable("无可索引内容")
        matrix = await _embed_texts([c["text"] for c in chunks], session_id=task_id)
        os.makedirs(cache_dir, exist_ok=True)
        try:
            np.savez(npz_path, matrix=matrix, chunks=np.asarray(chunks, dtype=object))
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump({"source_mtime": mtime, "chunk_count": len(chunks),
                           "dim": int(matrix.shape[1])}, f, ensure_ascii=False)
        except OSError as exc:
            get_logger().warning(f"[{task_id}] 写索引缓存失败: {exc}")
        return matrix, chunks


async def _retrieve(task_id: str, final_results_list, question: str) -> list[dict]:
    matrix, chunks = await get_or_build_index(task_id, final_results_list)
    qvec = await _embed_texts([question], session_id=task_id)
    scores = matrix @ qvec[0]
    top_idx = np.argsort(scores)[::-1][:RAG_TOP_K]
    return [chunks[i] for i in top_idx]


# ============================================================
# 流式问答
# ============================================================
async def answer_question_stream(task_id: str, final_results_list, question: str,
                                 history=None, *, ui_language: str = "zh-CN"):
    """异步生成器，逐个 yield token 增量文本。"""
    ui_language = normalize_ui_language(ui_language)
    lang_name = ui_language_name_from_code(ui_language)
    system = RAG_SYSTEM_PROMPT.format(lang=lang_name)

    text = full_text_of(final_results_list)
    context = ""
    if len(text) <= RAG_STUFF_ALL_CHARS:
        context = _format_chunks_for_prompt(_build_chunks(final_results_list))
    else:
        try:
            top_chunks = await _retrieve(task_id, final_results_list, question)
            context = _format_chunks_for_prompt(top_chunks)
        except EmbedUnavailable as exc:
            get_logger().warning(f"[{task_id}] embedding 不可用，回退塞全文: {exc}")
            context = text[:RAG_MAX_CONTEXT_CHARS]

    messages = [{"role": "system", "content": system}]
    for turn in (history or []):
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({
        "role": "user",
        "content": f"【转写片段】\n{context}\n\n【问题】{question}",
    })

    async with _client_session() as session:
        async with session.post(
            resolve_llm_url(session_id=task_id, route_source="rag.chat"),
            headers={"Content-Type": "application/json"},
            json={
                "model": LLM_MODEL,
                "messages": messages,
                "max_tokens": 1024,
                "temperature": 0.3,
                "stream": True,
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=aiohttp.ClientTimeout(total=180, connect=30),
        ) as resp:
            resp.raise_for_status()
            async for raw_line in resp.content:
                line = raw_line.decode("utf-8", "ignore").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                    delta = obj["choices"][0]["delta"].get("content")
                except (ValueError, KeyError, IndexError):
                    continue
                if delta:
                    yield delta
