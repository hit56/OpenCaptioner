import ast
import asyncio
import json
import re
import time

import aiohttp
import requests

from subtitles.i18n import (
    normalize_ui_language,
    should_translate_for_ui_language,
    ui_language_name_from_code,
)
from subtitles._runtime import get_logger, resolve_llm_url
from subtitles import config

TRANSLATION_BATCH_MAX_ITEMS = config.TRANSLATION_BATCH_MAX_ITEMS
TRANSLATION_BATCH_MAX_CHARS = config.TRANSLATION_BATCH_MAX_CHARS
CHUNK_BATCH_MAX_ITEMS = config.CHUNK_BATCH_MAX_ITEMS
CHUNK_BATCH_MAX_CHARS = config.CHUNK_BATCH_MAX_CHARS
TRANSLATION_BATCH_MAX_ATTEMPTS = 3
TRANSLATION_CONNECT_TIMEOUT_S = 30
_THAI_SCRIPT_RE = re.compile(r"[\u0e00-\u0e7f]")
_HANGUL_SCRIPT_RE = re.compile(r"[\uac00-\ud7af\u1100-\u11ff]")


def _llm_client_session():
    # Respect HTTP(S)_PROXY so aiohttp can reach remote vLLM like requests/httpx.
    return aiohttp.ClientSession(trust_env=True)


def parse_llm_json(text):
    if text is None:
        return None
    raw_text = str(text).strip()
    if not raw_text:
        return None
    candidates = [raw_text]
    match = re.search(r'```(?:json)?\s*(.*?)\s*```', raw_text, re.DOTALL | re.IGNORECASE)
    if match:
        candidates.append(match.group(1).strip())
    for pattern in (r'(\{[\s\S]*\})', r'(\[[\s\S]*\])'):
        match = re.search(pattern, raw_text)
        if match:
            candidates.append(match.group(1).strip())
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except Exception:
            pass
        try:
            return ast.literal_eval(candidate)
        except Exception:
            pass
    return None


def check_noise_with_global_context(current_text, up_context_str, down_context_str):
    if not current_text:
        return True
    try:
        res = requests.post(
            resolve_llm_url(route_source="noise_check"),
            headers={'Content-Type': 'application/json'},
            json={
                "model": "llm",
                "messages": [
                    {"role": "system", "content": "判断是否为ASR噪音，结合上下文。返回 {\"is_noise\": true/false}"},
                    {"role": "user", "content": f"上文：{up_context_str}\n目标：{current_text}\n下文：{down_context_str}"},
                ],
                "max_tokens": 1024,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=30,
        )
        parsed = parse_llm_json(res.json()['choices'][0]['message']['content'].strip())
        return parsed.get("is_noise", False) if parsed else False
    except Exception:
        return False


async def chunk_chinese_text(text):
    if not text.strip(): return []
    try:
        async with _llm_client_session() as session:
            async with session.post(resolve_llm_url(route_source="chunk_chinese_text"), headers={'Content-Type': 'application/json'}, json={"model": "llm", "messages": [{"role": "system", "content": "你是一个专业的短视频字幕时间轴切分专家。根据语义停顿切分，每段10-16字，去除末尾标点，返回JSON [{'text': '...'}]"}, {"role": "user", "content": text}], "max_tokens": 4096, "temperature": 0.1, "chat_template_kwargs": {"enable_thinking": False}}, timeout=60) as resp:
                res = parse_llm_json((await resp.json())['choices'][0]['message']['content'].strip())
                return res if res and isinstance(res, list) and 'text' in res[0] else [{"text": text}]
    except: return [{"text": text}]

async def translate_and_align(text, lang='en', target_language='zh-CN'):
    if not text.strip():
        return []
    try:
        async with _llm_client_session() as session:
            retried = await _retry_translate_segment(session, {
                "index": 0,
                "lang": lang,
                "text": text,
                "ui_language": target_language,
            }, target_language=target_language)
            return retried or _fallback_translation_pairs(text, lang, target_language)
    except Exception:
        return _fallback_translation_pairs(text, lang, target_language)


def _normalize_translation_pairs(pairs, fallback_text=""):
    normalized = []
    if isinstance(pairs, list):
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            src = str(pair.get('src', '') or '').strip()
            tgt = str(pair.get('tgt', '') or '').strip()
            if src or tgt:
                normalized.append({"src": src, "tgt": tgt})
    if normalized:
        return normalized
    fallback_text = (fallback_text or "").strip()
    return [{"src": fallback_text, "tgt": fallback_text}] if fallback_text else []


def _collapse_match_text(text):
    return re.sub(r'\s+', ' ', str(text or '')).strip().lower()


def _translation_pairs_match_source(pairs, expected_text):
    """Return True/False when src can be checked; None if src is absent."""
    expected = _collapse_match_text(expected_text)
    if not expected:
        return False

    src_parts = [
        _collapse_match_text(pair.get('src', ''))
        for pair in (pairs or [])
        if isinstance(pair, dict) and _collapse_match_text(pair.get('src', ''))
    ]
    if not src_parts:
        return None

    combined_src = ' '.join(src_parts)
    if combined_src == expected:
        return True

    probe_len = min(len(expected), 120)
    probe = expected[:probe_len]
    if probe and probe in combined_src:
        return True
    if combined_src[:probe_len] and combined_src[:probe_len] in expected:
        return True

    expected_words = expected.split()[:8]
    src_words = combined_src.split()[:8]
    if expected_words and src_words:
        overlap = len(set(expected_words) & set(src_words))
        if overlap >= max(2, min(len(expected_words), len(src_words)) // 2):
            return True
    return False


def _extract_parsed_translation_items(parsed):
    parsed_items = []
    if isinstance(parsed, dict):
        parsed_items = parsed.get('items') or parsed.get('translations') or parsed.get('results') or []
    elif isinstance(parsed, list):
        parsed_items = parsed
    if not isinstance(parsed_items, list):
        return []

    normalized = []
    for item in parsed_items:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get('index'))
        except (TypeError, ValueError):
            idx = None
        pairs = item.get('pairs') or item.get('aligned_pairs') or item.get('segments') or []
        normalized.append({
            "index": idx,
            "pairs": _normalize_translation_pairs(pairs),
        })
    return normalized


def _build_batch_translation_map(batch, parsed_items):
    """Resolve batch translations by segment index, with positional fallback."""
    if not batch:
        return {}

    ordered_pairs = [item['pairs'] for item in parsed_items]
    ordered_indices = [item['index'] for item in parsed_items]
    batch_indices = [entry['index'] for entry in batch]
    indexed = {
        item['index']: item['pairs']
        for item in parsed_items
        if item['index'] is not None
    }

    resolved = {}

    if len(ordered_pairs) == len(batch):
        seq_0 = list(range(len(batch)))
        seq_1 = list(range(1, len(batch) + 1))
        if ordered_indices in (seq_0, seq_1) and batch_indices != ordered_indices:
            for pos, entry in enumerate(batch):
                resolved[entry['index']] = ordered_pairs[pos]
            return resolved

    for entry in batch:
        resolved[entry['index']] = indexed.get(entry['index']) or []

    if len(ordered_pairs) != len(batch):
        return resolved

    for pos, entry in enumerate(batch):
        idx = entry['index']
        pairs = resolved.get(idx) or []
        src_status = _translation_pairs_match_source(pairs, entry['text'])
        if src_status is not False and pairs:
            continue
        candidate = ordered_pairs[pos]
        if _translation_pairs_match_source(candidate, entry['text']) is not False:
            resolved[idx] = candidate

    return resolved


def _batch_translation_needs_retry(pairs, expected_text, ui_language):
    if _needs_translation_retry(pairs, expected_text, ui_language):
        return True
    src_status = _translation_pairs_match_source(pairs, expected_text)
    return src_status is False


def _text_has_thai_script(text):
    return bool(_THAI_SCRIPT_RE.search(text or ""))


def _text_has_hangul_script(text):
    return bool(_HANGUL_SCRIPT_RE.search(text or ""))


def _looks_like_untranslated_target(src_text, tgt_text, target_language='zh-CN'):
    src = re.sub(r'\s+', ' ', str(src_text or '')).strip()
    tgt = re.sub(r'\s+', ' ', str(tgt_text or '')).strip()
    target = normalize_ui_language(target_language)
    if not tgt:
        return True

    src_norm = src.lower()
    tgt_norm = tgt.lower()
    if src_norm and src_norm == tgt_norm:
        return True

    has_cjk = bool(re.search(r'[\u4e00-\u9fff]', tgt))
    has_latin = bool(re.search(r'[A-Za-z]', tgt))
    has_thai = _text_has_thai_script(tgt)
    has_hangul = _text_has_hangul_script(tgt)

    if target in {'zh-CN', 'zh-TW'}:
        if has_thai or has_hangul:
            return True
        return has_latin and not has_cjk
    if target in {'en', 'fr', 'de', 'es', 'pt'}:
        if has_thai or has_hangul:
            return True
        return has_cjk and not has_latin
    return False


def _needs_translation_retry(pairs, fallback_text="", target_language='zh-CN'):
    normalized = _normalize_translation_pairs(pairs)
    if not normalized:
        return True
    return all(
        _looks_like_untranslated_target(
            pair.get('src') or fallback_text,
            pair.get('tgt'),
            target_language,
        )
        for pair in normalized
    )


def _fallback_translation_pairs(text, lang='en', target_language='zh-CN'):
    fallback_text = str(text or '').strip()
    if not fallback_text:
        return []
    if should_translate_for_ui_language(lang, target_language, fallback_text):
        return [{"src": fallback_text, "tgt": ""}]
    return [{"src": fallback_text, "tgt": fallback_text}]


async def _retry_translate_segment(session, item, target_language='zh-CN'):
    parsed = None
    idx = item.get('index')
    lang = item.get('lang', 'auto')
    text = str(item.get('text', '') or '').strip()
    ui_language = normalize_ui_language(item.get('ui_language') or target_language)
    target_lang_name = ui_language_name_from_code(ui_language)
    if not text:
        return []

    try:
        async with session.post(
            resolve_llm_url(route_source=f"retry_translate_segment.{lang}.{ui_language}"),
            headers={'Content-Type': 'application/json'},
            json={
                "model": "llm",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            f"你是专业字幕翻译助手。你会收到一条外语、方言或不同书写系统的字幕，请翻译成自然、口语化的{target_lang_name}。"
                            "严格只翻译 user 提供的 text 字段内容，不得补充、预测或翻译输入中不存在的后续语句。"
                            "返回的 pairs 中 tgt 必须完整对应 src，不得添加原文没有的信息。"
                            "即便原文里有口头禅、语气词、停顿词，也必须翻译出来，不得原样照抄原文。"
                            "如果目标语言是简体中文或繁體中文，务必使用对应字形。"
                            "严格返回 JSON 对象：{\"pairs\":[{\"src\":\"原文\",\"tgt\":\"译文\"}]}，不要输出任何解释或 Markdown。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"lang": lang, "text": text, "target_language": ui_language},
                            ensure_ascii=False,
                        ),
                    },
                ],
                "max_tokens": 1024,
                "temperature": 0.05,
                "response_format": {"type": "json_object"},
                "chat_template_kwargs": {"enable_thinking": False},
            },
            timeout=aiohttp.ClientTimeout(total=60, connect=TRANSLATION_CONNECT_TIMEOUT_S),
        ) as resp:
            parsed = parse_llm_json((await resp.json())['choices'][0]['message']['content'].strip())
    except Exception as e:
        get_logger().error(
            f"Single translation retry error (idx={idx}, lang={lang}, ui_language={ui_language}): {e}"
        )
        return []

    parsed_pairs = []
    if isinstance(parsed, dict):
        parsed_pairs = parsed.get('pairs') or parsed.get('items') or parsed.get('segments') or []
    elif isinstance(parsed, list):
        parsed_pairs = parsed

    normalized = _normalize_translation_pairs(parsed_pairs)
    if _needs_translation_retry(normalized, text, ui_language):
        return []
    return normalized


async def batch_translate_segments(
    segment_items,
    default_lang='en',
    target_language='zh-CN',
    progress_cb=None,
):
    """按整段/整文件批量翻译，减少逐段小请求带来的 vLLM 排队与开销。"""
    target_language = normalize_ui_language(target_language)
    prepared_items = []
    for item in segment_items or []:
        text = str(item.get('text', '') or '').strip()
        if not text:
            continue
        prepared_items.append({
            "index": int(item.get('index', 0)),
            "lang": item.get('lang', default_lang),
            "text": text,
            "ui_language": normalize_ui_language(item.get('ui_language') or target_language),
        })

    if not prepared_items:
        return {}

    batches = []
    current_batch = []
    current_chars = 0
    for item in prepared_items:
        item_chars = len(item['text'])
        if current_batch and (
            len(current_batch) >= TRANSLATION_BATCH_MAX_ITEMS
            or current_chars + item_chars > TRANSLATION_BATCH_MAX_CHARS
        ):
            batches.append(current_batch)
            current_batch = []
            current_chars = 0
        current_batch.append(item)
        current_chars += item_chars
    if current_batch:
        batches.append(current_batch)

    results = {}
    total_batches = len(batches)
    async with _llm_client_session() as session:
        for batch_idx, batch in enumerate(batches):
            if progress_cb:
                progress_cb(f"正在批量翻译字幕，第 {batch_idx + 1}/{total_batches} 批次...")
            batch_target_language = normalize_ui_language(batch[0].get('ui_language') or target_language)
            batch_target_lang_name = ui_language_name_from_code(batch_target_language)
            payload_items = [
                {
                    "index": item["index"],
                    "lang": item["lang"],
                    "text": item["text"],
                    "target_language": item["ui_language"],
                }
                for item in batch
            ]
            parsed = None
            batch_request_payload = {
                "model": "llm",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            f"你是专业字幕翻译助手。你会收到一个 JSON 数组，每项包含 index、lang、text、target_language。"
                            f"请逐项翻译成自然、口语化的{batch_target_lang_name}，并在每个 index 内按语义切成较短片段。"
                            "每个 index 必须独立翻译其 text，禁止引用、合并或预测其它 index 的内容。"
                            "即便原文里有口头禅、语气词、停顿词，也必须翻译，不得原样照抄原文。"
                            "如果目标语言是简体中文或繁體中文，务必使用对应字形。"
                            "严格返回 JSON 对象：{\"items\":[{\"index\":0,\"pairs\":[{\"src\":\"原文片段\",\"tgt\":\"译文片段\"}]}]}。"
                            "不要输出任何解释、Markdown 或额外字段；不得合并不同 index。"
                        ),
                    },
                    {"role": "user", "content": json.dumps(payload_items, ensure_ascii=False)},
                ],
                "max_tokens": 4096,
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
                "chat_template_kwargs": {"enable_thinking": False},
            }
            batch_timeout = aiohttp.ClientTimeout(total=120, connect=TRANSLATION_CONNECT_TIMEOUT_S)
            for attempt in range(TRANSLATION_BATCH_MAX_ATTEMPTS):
                try:
                    t0 = time.perf_counter()
                    async with session.post(
                        resolve_llm_url(route_source=f"batch_translate_segments.{batch_target_language}"),
                        headers={'Content-Type': 'application/json'},
                        json=batch_request_payload,
                        timeout=batch_timeout,
                    ) as resp:
                        parsed = parse_llm_json((await resp.json())['choices'][0]['message']['content'].strip())
                    get_logger().info(
                        f"Batch translation completed in {time.perf_counter()-t0:.1f}s, "
                        f"items={len(batch)}, attempt={attempt + 1}"
                    )
                    break
                except (
                    aiohttp.ClientConnectorError,
                    aiohttp.ServerDisconnectedError,
                    aiohttp.ServerTimeoutError,
                    asyncio.TimeoutError,
                    ConnectionRefusedError,
                    OSError,
                ) as e:
                    if attempt + 1 < TRANSLATION_BATCH_MAX_ATTEMPTS:
                        wait_s = min(2 ** attempt, 8)
                        get_logger().warning(
                            f"Batch translation connection failed (ui_language={batch_target_language}, "
                            f"attempt={attempt + 1}/{TRANSLATION_BATCH_MAX_ATTEMPTS}): {e}; retry in {wait_s}s"
                        )
                        await asyncio.sleep(wait_s)
                        continue
                    get_logger().error(
                        f"Batch translation connection failed (ui_language={batch_target_language}): {e}"
                    )
                except Exception as e:
                    get_logger().error(f"Batch translation error (ui_language={batch_target_language}): {e}")
                    break

            parsed_items = _extract_parsed_translation_items(parsed)
            parsed_map = _build_batch_translation_map(batch, parsed_items)

            retry_items = []
            for item in batch:
                idx = item['index']
                translated_pairs = parsed_map.get(idx) or []
                if _batch_translation_needs_retry(translated_pairs, item['text'], item['ui_language']):
                    get_logger().warning(
                        f"Batch translation fallback triggered: idx={idx}, lang={item['lang']}, "
                        f"ui_language={item['ui_language']}, text={item['text'][:120]}"
                    )
                    retry_items.append(item)
                else:
                    results[idx] = translated_pairs

            # 并发执行所有重试请求
            if retry_items:
                get_logger().info(f"Retrying {len(retry_items)} items concurrently")
                retry_tasks = [
                    _retry_translate_segment(session, item, target_language=item['ui_language'])
                    for item in retry_items
                ]
                retry_results = await asyncio.gather(*retry_tasks, return_exceptions=True)
                for item, retry_result in zip(retry_items, retry_results):
                    idx = item['index']
                    if isinstance(retry_result, Exception):
                        get_logger().error(f"Retry failed for idx={idx}: {retry_result}")
                        retry_result = []
                    results[idx] = retry_result or _fallback_translation_pairs(
                        item['text'], item['lang'], item['ui_language'],
                    )

    return results


def _normalize_chunk_items(chunks, fallback_text=""):
    normalized = []
    if isinstance(chunks, list):
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            text = str(chunk.get('text', '') or chunk.get('src', '') or '').strip()
            if text:
                normalized.append({"text": text})
    if normalized:
        return normalized
    fallback_text = (fallback_text or "").strip()
    return [{"text": fallback_text}] if fallback_text else []


async def batch_chunk_chinese_segments(segment_items):
    """按整段/整文件批量切分长中文字幕，减少逐段小请求造成的等待。"""
    prepared_items = []
    for item in segment_items or []:
        text = str(item.get('text', '') or '').strip()
        if not text:
            continue
        prepared_items.append({
            "index": int(item.get('index', 0)),
            "text": text,
        })

    if not prepared_items:
        return {}

    batches = []
    current_batch = []
    current_chars = 0
    for item in prepared_items:
        item_chars = len(item['text'])
        if current_batch and (
            len(current_batch) >= CHUNK_BATCH_MAX_ITEMS
            or current_chars + item_chars > CHUNK_BATCH_MAX_CHARS
        ):
            batches.append(current_batch)
            current_batch = []
            current_chars = 0
        current_batch.append(item)
        current_chars += item_chars
    if current_batch:
        batches.append(current_batch)

    results = {}
    async with _llm_client_session() as session:
        for batch in batches:
            payload_items = [
                {"index": item["index"], "text": item["text"]}
                for item in batch
            ]
            parsed = None
            try:
                async with session.post(
                    resolve_llm_url(route_source="batch_chunk_chinese_segments"),
                    headers={'Content-Type': 'application/json'},
                    json={
                        "model": "llm",
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "你是专业字幕切分助手。你会收到一个 JSON 数组，每项包含 index 和 text。"
                                    "请逐项根据语义停顿切分，每段10-16字，去除末尾标点。"
                                    "严格返回 JSON 对象：{\"items\":[{\"index\":0,\"chunks\":[{\"text\":\"...\"}]}]}。"
                                    "不要输出任何解释、Markdown 或额外字段；不得合并不同 index。"
                                ),
                            },
                            {"role": "user", "content": json.dumps(payload_items, ensure_ascii=False)},
                        ],
                        "max_tokens": 4096,
                        "temperature": 0.1,
                        "response_format": {"type": "json_object"},
                        "chat_template_kwargs": {"enable_thinking": False},
                    },
                    timeout=180,
                ) as resp:
                    parsed = parse_llm_json((await resp.json())['choices'][0]['message']['content'].strip())
            except Exception as e:
                get_logger().error(f"Batch Chinese chunking error: {e}")

            parsed_items = []
            if isinstance(parsed, dict):
                parsed_items = parsed.get('items') or parsed.get('results') or parsed.get('chunks') or []
            elif isinstance(parsed, list):
                parsed_items = parsed

            parsed_map = {}
            if isinstance(parsed_items, list):
                for item in parsed_items:
                    if not isinstance(item, dict):
                        continue
                    try:
                        idx = int(item.get('index'))
                    except (TypeError, ValueError):
                        continue
                    chunks = item.get('chunks') or item.get('items') or item.get('segments') or []
                    parsed_map[idx] = _normalize_chunk_items(chunks)

            for item in batch:
                idx = item['index']
                results[idx] = parsed_map.get(idx) or [{"text": item['text']}]

    return results
