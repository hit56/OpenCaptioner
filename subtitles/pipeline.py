"""Video subtitle post-processing pipeline (runs on gateway)."""

import asyncio
import concurrent.futures
import json
import os
import re
import time

from subtitles.i18n import normalize_ui_language, should_translate_for_ui_language

from subtitles import config
from subtitles._runtime import get_logger
from subtitles.aligner import chunked_forced_align_segment, forced_align_segment
from subtitles.burn_in import auto_generate_subtitle_video_sync
from subtitles.format import split_segment_by_sentence
from subtitles.llm_stages import (
    batch_chunk_chinese_segments,
    check_noise_with_global_context,
)
from subtitles.segment_translation import (
    batch_translate_subtitle_segments,
    format_translation_from_pairs,
)
from subtitles.wrap import format_elapsed_time, strip_display_terminal_period

SUBTITLE_PUNC_TO_STRIP = config.SUBTITLE_PUNC_TO_STRIP


def _resolve_segment_audio_path(item, session_id, segment_dir):
    file_path = item.get("file_path") or ""
    if file_path and os.path.exists(file_path):
        return file_path
    seg_url = item.get("segment_url") or ""
    basename = os.path.basename(seg_url) if seg_url else ""
    if basename:
        candidate = os.path.join(segment_dir, basename)
        if os.path.exists(candidate):
            return candidate
    index = item.get("index")
    if index is not None:
        candidate = os.path.join(segment_dir, f"{index}.wav")
        if os.path.exists(candidate):
            return candidate
    return file_path or None


def _split_text_by_piece_duration(text, pieces):
    """按各片段时长占比切分文本，切点吸附到最近的词/标点边界。

    合并段只记录了片段的时间边界（piece_spans），没有文本归属，因此这里按时长
    比例估算，再把切点移到就近的空白或标点处，避免把单词/词组劈开。
    """
    total_dur = sum(max(p.get("end_ts", 0.0) - p.get("start_ts", 0.0), 0.0) for p in pieces)
    if total_dur <= 0 or not text:
        return None

    # 优先在标点或空白处断开
    boundaries = [m.end() for m in re.finditer(r'[。，、？！；：.,!?;:]\s*|\s+', text)]
    if not boundaries:
        return None

    parts = []
    cursor = 0
    acc_dur = 0.0
    for pi, p in enumerate(pieces):
        if pi == len(pieces) - 1:
            parts.append(text[cursor:])
            break
        acc_dur += max(p.get("end_ts", 0.0) - p.get("start_ts", 0.0), 0.0)
        target = int(len(text) * (acc_dur / total_dur))
        # 吸附到 target 右侧最近的边界，且必须前进
        cut = next((b for b in boundaries if b >= target and b > cursor), None)
        if cut is None:
            cut = len(text)
        parts.append(text[cursor:cut])
        cursor = cut
        if cursor >= len(text):
            break

    # 片段数与文本块数须一致，否则视为切分失败
    while len(parts) < len(pieces):
        parts.append("")
    return parts[: len(pieces)]


def _align_by_pieces(item, session_lang, session_id, max_chars):
    """对合并段逐原始片段做强制对齐，再按绝对时间轴拼接。

    合并段（*_merged.wav）把多个不连续片段拼成长音频，中间填充静音。
    ForcedAligner 在这类输入上会把 token 时间戳整体塌缩到一点（实测 167s 段
    16/18 条 span 零宽度），整段作废后退化为按字符平摊的比例法。
    改为拿各片段自己的 wav 单独对齐，每段都在模型的正常工况内。
    """
    pieces = [p for p in (item.get("piece_spans") or []) if isinstance(p, dict)]
    if len(pieces) < 2:
        return None

    text = (item.get("text") or "").strip()
    parts = _split_text_by_piece_duration(text, pieces)
    if not parts:
        return None

    all_subs = []
    aligned_pieces = 0
    for p, part_text in zip(pieces, parts):
        part_text = (part_text or "").strip()
        path = p.get("file_path") or ""
        start_ts = p.get("start_ts", 0.0)
        end_ts = p.get("end_ts", start_ts)
        if not part_text:
            continue
        if not path or not os.path.exists(path):
            # 该片段音频缺失：按整片时间兜底，保证不丢词
            all_subs.append({
                "start": start_ts, "end": end_ts,
                "text": part_text.rstrip(SUBTITLE_PUNC_TO_STRIP), "trans": "",
            })
            continue
        subs = forced_align_segment(
            path, part_text, session_lang, start_ts, max_chars=max_chars,
        )
        if not subs:
            # 整片对齐失败。直接退回"整片一条字幕"等于放弃时间轴：后续按字数
            # 比例平摊，而歌唱的字数与时长完全不成比例（"always love you" 会被
            # 拖唱数秒），字幕必然整片提前。
            # 先用更小的窗口重试——实测 50s 片段整体失败，切成 ~20s 后逐块成功。
            subs = chunked_forced_align_segment(
                path, part_text, session_lang, start_ts,
                max_chars=max_chars, chunk_target_s=20,
            )
            if subs:
                get_logger().info(
                    f"[{session_id}] Piece {start_ts:.1f}-{end_ts:.1f}s: whole-piece align failed, "
                    f"chunked align recovered {len(subs)} cues."
                )
        if subs:
            all_subs.extend(subs)
            aligned_pieces += 1
        else:
            # 连分块也失败：退回该片段自身时间范围，仍不丢词
            get_logger().warning(
                f"[{session_id}] Piece {start_ts:.1f}-{end_ts:.1f}s: align failed even chunked; "
                f"falling back to whole-piece span (timing will be approximate)."
            )
            all_subs.append({
                "start": start_ts, "end": end_ts,
                "text": part_text.rstrip(SUBTITLE_PUNC_TO_STRIP), "trans": "",
            })

    if not all_subs or aligned_pieces == 0:
        return None

    # 不排序：all_subs 已按文本顺序（逐片段、片段内逐句）排列，各片段时间范围本就
    # 互不重叠。按 start 排序会在个别 cue 时间偏移时把文本顺序打乱，让歌词错位。
    get_logger().info(
        f"[{session_id}] Per-piece align: {aligned_pieces}/{len(pieces)} pieces aligned, "
        f"{len(all_subs)} cues (was 1 merged {item.get('end_ts', 0) - item.get('start_ts', 0):.1f}s segment)"
    )
    return all_subs


def build_sub_segments_for_item(item, session_lang, session_id, fast_mode=False):
    """Generate sub_segments for one ASR segment (video mode)."""
    final_text_content = (item.get("text") or "").strip()
    item["sub_segments"] = []
    if not final_text_content or fast_mode:
        return item

    needs_ui_translation = should_translate_for_ui_language(
        session_lang,
        item.get("_ui_language", "zh-CN"),
        final_text_content,
    )
    is_long_chinese = (
        not needs_ui_translation
        and len(final_text_content) > 16
        and bool(re.search(r"[\u4e00-\u9fa5]", final_text_content))
    )

    segment_dir = os.path.join(config.SEGMENT_DIR, session_id)
    audio_path = _resolve_segment_audio_path(item, session_id, segment_dir)
    fa_duration_s = item.get("end_ts", 0.0) - item.get("start_ts", 0.0)
    fa_text_len = len(re.sub(r"\s+", "", final_text_content))
    use_forced_align = (
        final_text_content
        and audio_path
        and os.path.exists(audio_path)
        and fa_duration_s <= 300.0
        and fa_text_len <= 4000
    )

    if use_forced_align:
        _fa_is_word_level = session_lang in ("en", "fr", "de", "it", "pt", "ru", "es")
        _fa_max_chars = 24 if _fa_is_word_level else 11
        # 合并段（多片段拼接）优先逐片段对齐：单段越长，ForcedAligner 塌缩得越狠。
        # 早在上游仍能对齐的短片段上分别对齐，正确性远好于整段一次对齐或比例法。
        if len(item.get("piece_spans") or []) >= 2:
            piece_subs = _align_by_pieces(item, session_lang, session_id, _fa_max_chars)
            if piece_subs:
                item["sub_segments"] = piece_subs
                return item
        fa_subs = forced_align_segment(
            audio_path,
            final_text_content,
            session_lang,
            item.get("start_ts", 0.0),
            max_chars=_fa_max_chars,
        )
        if not fa_subs and fa_duration_s > 60:
            fa_subs = chunked_forced_align_segment(
                audio_path,
                final_text_content,
                session_lang,
                item.get("start_ts", 0.0),
                max_chars=_fa_max_chars,
            )
        if fa_subs:
            item["sub_segments"] = fa_subs
            return item
        get_logger().warning(
            f"[{session_id}] ForcedAligner fallback to ratio mode "
            f"(lang={session_lang}, len={len(final_text_content)})"
        )

    if needs_ui_translation:
        sentence_chunks = split_segment_by_sentence(
            final_text_content.rstrip(SUBTITLE_PUNC_TO_STRIP),
            item.get("start_ts", 0.0),
            item.get("end_ts", 0.0),
        )
        if sentence_chunks:
            for chunk in sentence_chunks:
                item["sub_segments"].append({
                    "start": chunk.get("start", item.get("start_ts")),
                    "end": chunk.get("end", item.get("end_ts")),
                    "text": chunk.get("text", "").rstrip(SUBTITLE_PUNC_TO_STRIP),
                    "trans": "",
                })
        else:
            item["sub_segments"].append({
                "start": item.get("start_ts"),
                "end": item.get("end_ts"),
                "text": final_text_content.rstrip(SUBTITLE_PUNC_TO_STRIP),
                "trans": "",
            })
    elif is_long_chinese:
        sentence_chunks = split_segment_by_sentence(
            final_text_content.rstrip(SUBTITLE_PUNC_TO_STRIP),
            item.get("start_ts", 0.0),
            item.get("end_ts", 0.0),
        )
        if sentence_chunks:
            for chunk in sentence_chunks:
                item["sub_segments"].append({
                    "start": chunk.get("start", item.get("start_ts")),
                    "end": chunk.get("end", item.get("end_ts")),
                    "text": chunk.get("text", "").rstrip(SUBTITLE_PUNC_TO_STRIP),
                    "trans": "",
                })
        else:
            item["sub_segments"].append({
                "start": item.get("start_ts"),
                "end": item.get("end_ts"),
                "text": final_text_content.rstrip(SUBTITLE_PUNC_TO_STRIP),
                "trans": "",
            })
    else:
        item["sub_segments"].append({
            "start": item.get("start_ts"),
            "end": item.get("end_ts"),
            "text": final_text_content.rstrip(SUBTITLE_PUNC_TO_STRIP),
            "trans": "",
        })
    return item


def run_video_subtitle_pipeline_sync(
    session_id: str,
    original_filename: str,
    ui_language: str,
    session_lang: str,
    *,
    progress_cb=None,
    fast_mode: bool = False,
    asr_stage_timings: dict | None = None,
):
    """Synchronous subtitle pipeline; call via asyncio.to_thread from gateway."""
    ui_language = normalize_ui_language(ui_language)
    logger = get_logger()
    stage_timings = dict(asr_stage_timings or {})
    session_seg_dir = os.path.join(config.SEGMENT_DIR, session_id)
    result_json_path = os.path.join(session_seg_dir, "final_result.json")

    if not os.path.exists(result_json_path):
        raise FileNotFoundError(f"final_result.json not found for session {session_id}")

    with open(result_json_path, "r", encoding="utf-8") as f:
        final_results_list = json.load(f)

    if not final_results_list:
        return {"video_url": None, "stage_timings": stage_timings}

    def _progress(message):
        if progress_cb:
            progress_cb(message)

    if not fast_mode:
        _progress("正在对齐字幕时间轴...")
        align_start = time.perf_counter()
        for item in final_results_list:
            item["_ui_language"] = ui_language
            build_sub_segments_for_item(item, session_lang, session_id, fast_mode=fast_mode)
            item.pop("_ui_language", None)
        stage_timings["forced_align_s"] = time.perf_counter() - align_start

        _progress("正在进行全局噪音检测...")
        global_denoise_start = time.perf_counter()
        indices_to_check = []
        for i, item in enumerate(final_results_list):
            txt = item.get("text", "").strip()
            clean_len = len(re.sub(r"[^\w\u4e00-\u9fa5]", "", txt))
            has_chinese = bool(re.search(r"[\u4e00-\u9fa5]", txt))
            if has_chinese and clean_len <= 10:
                indices_to_check.append(i)

        if indices_to_check:
            keep_mask = [True] * len(final_results_list)
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                future_map = {}
                for idx in indices_to_check:
                    ctx_lines = []
                    prevs = final_results_list[max(0, idx - 4):idx]
                    if prevs:
                        for pi, p in enumerate(prevs):
                            ctx_lines.append(f"{pi + 1}. {p['text']}")
                    else:
                        ctx_lines.append("无")
                    up_context_str = "\n".join(ctx_lines)

                    ctx_lines = []
                    nexts = final_results_list[idx + 1:min(len(final_results_list), idx + 5)]
                    if nexts:
                        for ni, n in enumerate(nexts):
                            ctx_lines.append(f"{ni + 1}. {n['text']}")
                    else:
                        ctx_lines.append("无")
                    down_context_str = "\n".join(ctx_lines)
                    fut = executor.submit(
                        check_noise_with_global_context,
                        final_results_list[idx]["text"],
                        up_context_str,
                        down_context_str,
                    )
                    future_map[fut] = idx

                for fut in concurrent.futures.as_completed(future_map):
                    idx = future_map[fut]
                    try:
                        if fut.result():
                            keep_mask[idx] = False
                    except Exception as e:
                        logger.error(f"Global check error at {idx}: {e}")

            if not all(keep_mask):
                final_results_list[:] = [
                    item for i, item in enumerate(final_results_list) if keep_mask[i]
                ]
        stage_timings["global_denoise_s"] = time.perf_counter() - global_denoise_start

        chunking_candidates = []
        for item in final_results_list:
            item_text = (item.get("text") or "").strip()
            if not item_text:
                continue
            needs_chunking = (
                not should_translate_for_ui_language(session_lang, ui_language, item_text)
                and len(item_text) > 16
                and bool(re.search(r"[\u4e00-\u9fa5]", item_text))
            )
            if needs_chunking:
                existing_subs = item.get("sub_segments", [])
                if len(existing_subs) > 1:
                    continue
                chunking_candidates.append({"index": item.get("index", 0), "text": item_text})

        if chunking_candidates:
            _progress("正在优化字幕断句与时间轴...")
            chunking_start = time.perf_counter()
            try:
                chunked_map = asyncio.run(batch_chunk_chinese_segments(chunking_candidates))
                for item in final_results_list:
                    chunks = chunked_map.get(item.get("index"))
                    if not chunks or len(chunks) <= 1:
                        continue
                    valid_chunks = [c for c in chunks if str(c.get("text", "") or "").strip()]
                    if not valid_chunks:
                        continue
                    total_duration = max(item.get("end_ts", 0.0) - item.get("start_ts", 0.0), 0.0)
                    total_chars = sum(len(str(c.get("text", "") or "").strip()) for c in valid_chunks)
                    current_offset = item.get("start_ts", 0.0)
                    rebuilt = []
                    for cidx, chunk in enumerate(valid_chunks):
                        chunk_text = str(chunk.get("text", "") or "").strip().rstrip(SUBTITLE_PUNC_TO_STRIP)
                        if not chunk_text:
                            continue
                        if cidx == len(valid_chunks) - 1 or total_chars <= 0 or total_duration <= 0:
                            seg_end = item.get("end_ts", current_offset)
                        else:
                            ratio = len(chunk_text) / total_chars
                            seg_end = current_offset + total_duration * ratio
                        rebuilt.append({
                            "start": current_offset,
                            "end": seg_end,
                            "text": chunk_text,
                            "trans": "",
                        })
                        current_offset = seg_end
                    if rebuilt:
                        rebuilt[-1]["end"] = item.get("end_ts", rebuilt[-1]["end"])
                        item["sub_segments"] = rebuilt
            except Exception as e:
                logger.error(f"[{session_id}] Batch Chinese chunking failed: {e}")
            stage_timings["text_chunking_s"] = time.perf_counter() - chunking_start

        sub_translation_candidates = []
        for item in final_results_list:
            item_text = (item.get("text") or "").strip()
            if not item_text:
                continue
            if not should_translate_for_ui_language(session_lang, ui_language, item_text):
                continue
            subs = item.get("sub_segments")
            if not subs:
                subs = [{
                    "start": item.get("start_ts", 0.0),
                    "end": item.get("end_ts", 0.0),
                    "text": item_text.rstrip(SUBTITLE_PUNC_TO_STRIP),
                    "trans": "",
                }]
                item["sub_segments"] = subs
            seg_idx = item.get("index", 0)
            existing_translation = (item.get("translation") or "").strip()
            normalized_item_text = item_text.rstrip(SUBTITLE_PUNC_TO_STRIP)
            for si, sub in enumerate(subs):
                sub_text = (sub.get("text") or "").strip()
                if not sub_text:
                    continue
                if existing_translation and (
                    len(subs) == 1 or sub_text == normalized_item_text
                ):
                    sub["trans"] = existing_translation
                    continue
                sub_translation_candidates.append({
                    "index": seg_idx * 10000 + si,
                    "_seg_idx": seg_idx,
                    "_sub_idx": si,
                    "lang": session_lang,
                    "ui_language": ui_language,
                    "text": sub_text,
                })

        if sub_translation_candidates:
            _progress(f"正在后台批量翻译 {len(sub_translation_candidates)} 条字幕...")
            translation_start = time.perf_counter()
            try:
                translated_map = asyncio.run(
                    batch_translate_subtitle_segments(
                        sub_translation_candidates,
                        session_lang,
                        ui_language,
                        progress_cb=_progress,
                    )
                )
                for cand in sub_translation_candidates:
                    pairs = translated_map.get(cand["index"])
                    if not pairs:
                        continue
                    tgt_text = format_translation_from_pairs(pairs)
                    if tgt_text:
                        for item in final_results_list:
                            if item.get("index") == cand["_seg_idx"]:
                                subs = item.get("sub_segments", [])
                                if cand["_sub_idx"] < len(subs):
                                    subs[cand["_sub_idx"]]["trans"] = tgt_text
                                break
            except Exception as e:
                logger.error(f"[{session_id}] Batch translation failed: {e}")
            stage_timings["translation_s"] = time.perf_counter() - translation_start

    with open(result_json_path, "w", encoding="utf-8") as f:
        json.dump(final_results_list, f, ensure_ascii=False, indent=2)

    generated_video_url = None
    _progress("正在生成字幕视频（长视频可能需要几分钟）...")
    video_result = auto_generate_subtitle_video_sync(session_id, original_filename, ui_language=ui_language)
    generated_video_url = video_result.get("url")
    for stage_name, duration in video_result.get("timings", {}).items():
        stage_timings[stage_name] = stage_timings.get(stage_name, 0.0) + duration

    return {"video_url": generated_video_url, "stage_timings": stage_timings}


async def run_video_subtitle_pipeline(
    session_id: str,
    original_filename: str,
    ui_language: str,
    session_lang: str,
    *,
    progress_cb=None,
    fast_mode: bool = False,
    asr_stage_timings: dict | None = None,
):
    return await asyncio.to_thread(
        run_video_subtitle_pipeline_sync,
        session_id,
        original_filename,
        ui_language,
        session_lang,
        progress_cb=progress_cb,
        fast_mode=fast_mode,
        asr_stage_timings=asr_stage_timings,
    )
