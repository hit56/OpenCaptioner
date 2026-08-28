"""Segment-level translation for ASR results and UI display."""

import asyncio
import time

from subtitles._runtime import get_logger
from subtitles.i18n import normalize_ui_language, should_translate_for_ui_language
from subtitles.llm_stages import (
    _fallback_translation_pairs,
    _retry_translate_segment,
    batch_translate_segments,
)
from subtitles.wrap import strip_display_terminal_period


def format_translation_from_pairs(pairs):
    tgt_text = "".join(
        (p.get("tgt") or "").strip() for p in pairs if isinstance(p, dict)
    )
    tgt_text = strip_display_terminal_period(tgt_text.strip())
    return tgt_text.strip("。，.,？！?!;；、：: ")


async def translate_segments_individually(segment_items, default_lang='en', target_language='zh-CN'):
    """Translate each ASR segment in isolation to preserve strict 1:1 alignment."""
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

    from subtitles.llm_stages import _llm_client_session

    results = {}
    async with _llm_client_session() as session:
        tasks = [
            _retry_translate_segment(session, item, target_language=item['ui_language'])
            for item in prepared_items
        ]
        translated = await asyncio.gather(*tasks, return_exceptions=True)
        for item, pairs in zip(prepared_items, translated):
            idx = item['index']
            if isinstance(pairs, Exception):
                get_logger().error(f"Segment translation failed for idx={idx}: {pairs}")
                pairs = []
            normalized = pairs or _fallback_translation_pairs(
                item['text'], item['lang'], item['ui_language'],
            )
            results[idx] = normalized
    return results


def attach_segment_translation(item, session_lang, ui_language):
    """
    Translate one finalized ASR segment and write item['translation'].
    Returns the translation text, or empty string when skipped/failed.
    """
    ui_language = normalize_ui_language(ui_language)
    item_text = (item.get("text") or "").strip()
    if not item_text:
        return ""

    if not should_translate_for_ui_language(session_lang, ui_language, item_text):
        item.pop("translation", None)
        return ""

    existing = (item.get("translation") or "").strip()
    if existing:
        return existing

    try:
        translated_map = asyncio.run(
            translate_segments_individually(
                [{
                    "index": item.get("index", 0),
                    "lang": session_lang,
                    "ui_language": ui_language,
                    "text": item_text,
                }],
                session_lang,
                ui_language,
            )
        )
    except Exception as e:
        get_logger().error(
            f"Immediate segment translation failed (idx={item.get('index')}, lang={session_lang}): {e}"
        )
        return ""

    pairs = translated_map.get(item.get("index", 0))
    tgt_text = format_translation_from_pairs(pairs or [])
    if tgt_text:
        item["translation"] = tgt_text
    return tgt_text


def needs_segment_translation(final_results_list, session_lang, ui_language):
    ui_language = normalize_ui_language(ui_language)
    for item in final_results_list or []:
        item_text = (item.get("text") or "").strip()
        if not item_text:
            continue
        if not should_translate_for_ui_language(session_lang, ui_language, item_text):
            continue
        if not (item.get("translation") or "").strip():
            return True
    return False


def count_translation_candidates(final_results_list, session_lang, ui_language):
    ui_language = normalize_ui_language(ui_language)
    count = 0
    for item in final_results_list or []:
        item_text = (item.get("text") or "").strip()
        if item_text and should_translate_for_ui_language(session_lang, ui_language, item_text):
            count += 1
    return count


def translate_results_list(final_results_list, session_lang, ui_language):
    """
    Translate segment text into item['translation'] one segment at a time.
    Returns elapsed seconds (0 if nothing to translate).
    """
    ui_language = normalize_ui_language(ui_language)
    candidates = []
    for item in final_results_list or []:
        item_text = (item.get("text") or "").strip()
        if not item_text:
            continue
        if not should_translate_for_ui_language(session_lang, ui_language, item_text):
            continue
        if (item.get("translation") or "").strip():
            continue
        candidates.append({
            "index": item.get("index", 0),
            "lang": session_lang,
            "ui_language": ui_language,
            "text": item_text,
        })

    if not candidates:
        return 0.0

    start = time.perf_counter()
    try:
        translated_map = asyncio.run(
            translate_segments_individually(candidates, session_lang, ui_language)
        )
    except Exception as e:
        get_logger().error(f"Segment translation failed: {e}")
        return time.perf_counter() - start

    for item in final_results_list:
        idx = item.get("index")
        pairs = translated_map.get(idx)
        if not pairs:
            continue
        tgt_text = format_translation_from_pairs(pairs)
        if tgt_text:
            item["translation"] = tgt_text

    return time.perf_counter() - start


async def batch_translate_subtitle_segments(
    segment_items,
    default_lang='en',
    target_language='zh-CN',
    progress_cb=None,
):
    """Batch translate subtitle-level chunks (second pass); not for ASR segment rows."""
    return await batch_translate_segments(
        segment_items,
        default_lang,
        target_language,
        progress_cb=progress_cb,
    )
