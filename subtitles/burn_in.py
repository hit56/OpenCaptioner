import json
import os
import re
import shutil
import subprocess
import time
import copy

from subtitles.i18n import normalize_ui_language
from subtitles import config
from subtitles._runtime import get_logger
from subtitles.format import get_dynamic_style_conf, get_video_dimensions
from subtitles.wrap import (
    _balance_bilingual_burn_chunks,
    _build_bilingual_thai_ass,
    _fit_parts_to_count,
    _split_text_into_n_parts,
    _subtitle_play_metrics,
    _time_slices_weighted,
    _translations_need_thai_font,
    _wrap_text_unbounded_for_burn,
    fix_english_spacing,
    format_srt_time,
    is_english_text,
    strip_display_terminal_period,
    strip_leading_video_subtitle_punct,
)

_NVENC_AVAILABLE: bool | None = None


def _probe_nvenc_available() -> bool:
    """Detect NVENC once per process; ffmpeg may list the encoder without a working GPU driver."""
    global _NVENC_AVAILABLE
    if _NVENC_AVAILABLE is not None:
        return _NVENC_AVAILABLE
    try:
        enc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if "h264_nvenc" not in (enc.stdout or ""):
            _NVENC_AVAILABLE = False
            return False
        smi = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        _NVENC_AVAILABLE = smi.returncode == 0 and bool((smi.stdout or "").strip())
    except Exception:
        _NVENC_AVAILABLE = False
    return _NVENC_AVAILABLE


def _video_encode_args(use_nvenc: bool) -> list[str]:
    if use_nvenc:
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll", "-rc", "vbr", "-cq", "23"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]


def _run_subtitle_burn_ffmpeg(session_id: str, video_path: str, vf_arg: str, out_path: str) -> None:
    # 必须保留 .mp4 后缀，否则 ffmpeg 无法推断输出封装格式。
    tmp_path = f"{os.path.splitext(out_path)[0]}.tmp.mp4"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    base_cmd = ["ffmpeg", "-y", "-i", video_path, "-vf", vf_arg]
    tail_cmd = ["-movflags", "+faststart", "-c:a", "copy", tmp_path]
    logger = get_logger()

    def _finalize_output() -> None:
        os.replace(tmp_path, out_path)

    try:
        if _probe_nvenc_available():
            try:
                subprocess.run(
                    base_cmd + _video_encode_args(True) + tail_cmd,
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                _finalize_output()
                logger.info(f"[{session_id}] 字幕压制使用 h264_nvenc")
                return
            except subprocess.CalledProcessError as exc:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                err = (exc.stderr or b"").decode("utf-8", errors="replace")[-500:]
                logger.warning(f"[{session_id}] h264_nvenc 失败，回退 libx264: {err}")

        subprocess.run(
            base_cmd + _video_encode_args(False) + tail_cmd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _finalize_output()
        logger.info(f"[{session_id}] 字幕压制使用 libx264")
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _upload_name_matches_session(name: str, session_id: str) -> bool:
    if name.startswith(f"{session_id}_"):
        return True
    parts = session_id.rsplit("_", 1)
    if len(parts) != 2:
        return False
    ts_raw, uuid8 = parts
    if len(ts_raw) == 15 and ts_raw[8] == "_":
        ts_tag = f"{ts_raw[:8]}{ts_raw[9:]}"
    else:
        ts_tag = ts_raw
    return name.startswith(f"{ts_tag}_") and f"_{uuid8}_" in name


def _find_upload_video_path(session_id: str, original_filename: str) -> str | None:
    video_exts = ('.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.webm')
    basename = os.path.basename(original_filename)
    candidate_paths = [
        os.path.join(config.UPLOAD_DIR, f"{session_id}_{basename}"),
        os.path.join(config.UPLOAD_DIR, basename),
        os.path.join(config.UPLOAD_DIR, original_filename),
    ]
    if os.path.isdir(config.UPLOAD_DIR):
        for name in os.listdir(config.UPLOAD_DIR):
            if not _upload_name_matches_session(name, session_id):
                continue
            path = os.path.join(config.UPLOAD_DIR, name)
            if name.endswith(f"_{basename}") and path.lower().endswith(video_exts):
                candidate_paths.insert(1, path)
    return next((p for p in candidate_paths if os.path.exists(p) and p.lower().endswith(video_exts)), None)


def _find_any_session_video_path(session_id: str) -> str | None:
    """扫描 uploads 目录，按 session_id 命名规则找到任意一个视频文件（无需 original_filename）。"""
    if not os.path.isdir(config.UPLOAD_DIR):
        return None
    video_exts = ('.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.webm')
    for name in os.listdir(config.UPLOAD_DIR):
        if not _upload_name_matches_session(name, session_id):
            continue
        path = os.path.join(config.UPLOAD_DIR, name)
        if path.lower().endswith(video_exts):
            return path
    return None


def _load_final_results_for_subtitles(session_id: str):
    """加载 final_result.json，返回 (res_path, data)；不存在或为空时返回 (None, None)。"""
    res_path = os.path.join(config.SEGMENT_DIR, session_id, "final_result.json")
    if not os.path.exists(res_path):
        return None, None
    try:
        with open(res_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return None, None
    if not data:
        return None, None
    return res_path, data


def apply_subtitle_edits(data: list, edits: list[dict]) -> list:
    """在内存副本上应用字幕编辑，返回新列表（不改动入参）。

    与 /subtitles/edit 落盘逻辑保持一致：仅覆盖传入片段的 text/translation，
    并把该段 sub_segments 塌缩为单条（时间跨度沿用整段 start_ts/end_ts），
    未传入的片段保留原有细粒度 sub_segments。edits 元素含 index/text/translation。
    """
    edits_by_index = {}
    for e in edits or []:
        idx = e.get("index")
        if idx is None:
            continue
        edits_by_index[idx] = e
    if not edits_by_index:
        return data
    result = copy.deepcopy(data)
    for item in result:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        if idx not in edits_by_index:
            continue
        edit = edits_by_index[idx]
        new_text = (edit.get("text") or "").strip()
        new_trans = (edit.get("translation") or "").strip()
        item["text"] = new_text
        item["translation"] = new_trans
        item["sub_segments"] = [{
            "start": item.get("start_ts"),
            "end": item.get("end_ts"),
            "text": new_text,
            "trans": new_trans,
        }]
    return result


def build_cue_list_for_session(session_id: str) -> tuple[list[dict], float]:
    """把 final_result.json 的 sub_segments 摊平成可编辑字幕条（cue）列表。

    每个 cue 含 start/end（秒，浮点）+ text（原文）+ trans（译文），按时间排序。
    这些时间来自强制对齐模型（ForcedAligner），是真正与音频对齐的时间轴。
    返回 (cues, max_end)；max_end 用于时间轴兜底时长。
    """
    _, data = _load_final_results_for_subtitles(session_id)
    if not data:
        return [], 0.0
    cues: list[dict] = []
    max_end = 0.0
    for seg in data:
        if not isinstance(seg, dict):
            continue
        fallback_trans = seg.get('trans') or seg.get('trans_text') or seg.get('translation') or ''
        subs = seg.get('sub_segments')
        if not subs:
            subs = [{
                "start": seg.get('start_ts'),
                "end": seg.get('end_ts'),
                "text": seg.get('text', ''),
                "trans": fallback_trans,
            }]
        for sub in subs:
            st = sub.get('start', seg.get('start_ts'))
            ed = sub.get('end', seg.get('end_ts'))
            st = float(st) if st is not None else 0.0
            ed = float(ed) if ed is not None else st
            text = (sub.get('text') or '').strip()
            trans = (sub.get('trans') or '').strip()
            if not text and not trans:
                continue
            cues.append({"start": st, "end": max(ed, st), "text": text, "trans": trans})
            max_end = max(max_end, max(ed, st))
    cues.sort(key=lambda c: (c["start"], c["end"]))
    return cues, max_end


def rebuild_data_with_cues(data: list, cues: list[dict]) -> list:
    """用编辑后的 cue 列表重建 final_result.json 结构（不改动入参）。

    与旧的 apply_subtitle_edits（按段塌缩、丢失对齐）不同：这里保留用户在时间轴上
    直接指定的每条 cue 起止时间，把 cue 按开始时间归入所属识别段的 sub_segments，
    使烧录/预览严格沿用用户设定的时间轴，而非按字数权重均摊。
    """
    result = copy.deepcopy(data) if data else []
    norm_cues: list[dict] = []
    for c in sorted(cues or [], key=lambda x: float(x.get("start") or 0.0)):
        text = (c.get("text") or "").strip()
        trans = (c.get("trans") or "").strip()
        if not text and not trans:
            continue
        st = float(c.get("start") or 0.0)
        ed = float(c.get("end") or 0.0)
        if st < 0:
            st = 0.0
        if ed < st:
            ed = st
        norm_cues.append({"start": st, "end": ed, "text": text, "trans": trans})

    segs = [item for item in result if isinstance(item, dict)]
    if not segs:
        if not norm_cues:
            return result
        synth = {
            "index": 0,
            "text": " ".join(c["text"] for c in norm_cues if c["text"]).strip(),
            "translation": " ".join(c["trans"] for c in norm_cues if c["trans"]).strip(),
            "start_ts": norm_cues[0]["start"],
            "end_ts": norm_cues[-1]["end"],
            "sub_segments": [dict(c) for c in norm_cues],
        }
        return [synth]

    for s in segs:
        s["sub_segments"] = []
    seg_bounds = sorted(
        [(float(s.get("start_ts") or 0.0), float(s.get("end_ts") or 0.0), s) for s in segs],
        key=lambda x: x[0],
    )
    for c in norm_cues:
        cs = c["start"]
        target = None
        for st, ed, s in seg_bounds:
            if st <= cs <= ed:
                target = s
                break
        if target is None:
            prior = [x for x in seg_bounds if x[0] <= cs]
            target = prior[-1][2] if prior else seg_bounds[0][2]
        target["sub_segments"].append(dict(c))
    return result


def export_cues_vtt_draft(session_id: str, cues: list[dict]) -> str | None:
    """把时间轴编辑器中的 cue 列表即时渲染为 WebVTT 预览（不落盘、不刻印）。

    走与最终烧录相同的 build_subtitle_entries 管线，保证预览形态与保存后一致。
    """
    try:
        _, data = _load_final_results_for_subtitles(session_id)
        edited = rebuild_data_with_cues(data or [], cues)
        if not edited:
            return "WEBVTT\n\n"
        w, h = 1280, 720
        video_path = _find_any_session_video_path(session_id)
        if video_path:
            try:
                dw, dh = get_video_dimensions(video_path)
                if dw and dh:
                    w, h = dw, dh
            except Exception:
                pass
        entries = build_subtitle_entries(session_id, w, h, data=edited)
        if not entries:
            return "WEBVTT\n\n"
        return build_subtitle_vtt(entries)
    except Exception as e:
        get_logger().error(f"Export cues VTT Error: {e}")
        return None


def _repage_long_span(pc, tc, st, ed, is_en, trans_is_en, max_dur):
    """把跨时过长的一页拆成多页，让字幕跟着人物讲话推进。

    Netflix 的最长时长规范意思是"拆成更多条"，不是"讲话没结束就把字幕清空"。
    这里按需要的页数把文本再切一次，时间按各页可见字数比例分配，
    整个 [st, ed] 区间仍被完整覆盖——end 绝不会被砍掉。

    返回 [(page_primary, page_trans, start, end), ...]。
    """
    span = ed - st
    if span <= max_dur + 1e-9 or max_dur <= 0:
        return [(pc, tc, st, ed)]

    want = int(span // max_dur) + (1 if span % max_dur > 1e-9 else 0)
    want = max(2, want)

    # 以有文本的一侧决定切成几份；两侧都有则以主语言为准
    p_has = bool((pc or "").strip())
    t_has = bool((tc or "").strip())
    if p_has:
        p_parts = _split_text_into_n_parts(pc, want, is_en)
        n = max(1, len(p_parts))
        t_parts = (
            _fit_parts_to_count(_split_text_into_n_parts(tc, n, trans_is_en), n, trans_is_en)
            if t_has else [""] * n
        )
        p_parts = _fit_parts_to_count(p_parts, n, is_en)
    elif t_has:
        t_parts = _split_text_into_n_parts(tc, want, trans_is_en)
        n = max(1, len(t_parts))
        t_parts = _fit_parts_to_count(t_parts, n, trans_is_en)
        p_parts = [""] * n
    else:
        return [(pc, tc, st, ed)]

    if n <= 1:
        # 文本切不开（如单个长词）：保留完整时间区间，不截断。
        # 超长单页优于中途消失的字幕。
        return [(pc, tc, st, ed)]

    weights = []
    for i in range(n):
        ref = p_parts[i] if (p_parts[i] or "").strip() else t_parts[i]
        weights.append(max(1, len(re.findall(r"[一-鿿A-Za-z0-9]", ref or ""))))
    ranges = _time_slices_weighted(st, ed, weights)
    return [(p_parts[i], t_parts[i], ranges[i][0], ranges[i][1]) for i in range(n)]


def _cue_required_duration(ent: dict, min_dur: float, max_dur: float) -> float:
    """条目按阅读速度所需的最短显示时长。

    双语字幕上下两行同时出现，观众需要读完两者，因此分别按各自语言的
    读速上限计算耗时再取较大值——用"字符总数 ÷ 单一上限"会把英文字符
    错按中文速率计，得到虚高的需求；只算主语言又会低估双语的阅读负担。
    """
    primary = (ent.get("primary") or "").strip()
    trans = (ent.get("trans") or "").strip()

    def _need(text):
        if not text:
            return 0.0
        vis = len(re.findall(r"[一-鿿A-Za-z0-9]", text))
        is_cjk = bool(re.search(r"[一-鿿]", text))
        limit = config.SUBTITLE_MAX_CPS_CJK if is_cjk else config.SUBTITLE_MAX_CPS
        return (vis / limit) if limit > 0 else 0.0

    need = max(_need(primary), _need(trans))
    return min(max(need, min_dur), max_dur)


def _enforce_netflix_timing(entries: list[dict]) -> list[dict]:
    """按 Netflix Timed Text 规范收敛字幕时序。

    规则与顺序（顺序很重要）：
      1. 规范化：修正 end < start
      2. 最短时长 + 阅读速度：向后延长，但不得越过下一条起点减最小间隔
      3. 最小间隔：相邻条目过近时收缩前一条结束时间，防止闪烁

    只调整时间，不改文本、不丢条目——丢条目等于丢字。

    注意：这里**不做**最长时长截断。超长条目已由 _repage_long_span 在分页阶段
    拆成多条；到这一步若仍有长条目，说明其文本无法再切分，截断只会让字幕在
    人物讲话未结束时消失，反而更糟。
    """
    if not entries:
        return entries

    min_dur = config.SUBTITLE_MIN_DURATION_S
    max_dur = config.SUBTITLE_MAX_DURATION_S
    min_gap = config.SUBTITLE_MIN_GAP_S

    out = sorted(entries, key=lambda e: (float(e.get("start") or 0.0), float(e.get("end") or 0.0)))
    n = len(out)

    # 每条目标时长：最短时长与阅读速度共同决定，但不超过最长时长
    needs = [_cue_required_duration(ent, min_dur, max_dur) for ent in out]

    # 规范化（保留原始时长，不做上限截断）
    for ent in out:
        st = float(ent.get("start") or 0.0)
        ed = float(ent.get("end") or 0.0)
        if ed < st:
            ed = st
        ent["start"] = st
        ent["end"] = ed

    # 先解决重叠/过近，再扩展时长；两者会互相影响，故迭代到收敛。
    # （单趟处理时，后续条目的位移会让前面刚扩好的条目重新变短。）
    for _ in range(4):
        changed = False

        # 最小间隔：优先收缩前一条；无收缩余地则整体后移后一条
        for i in range(n - 1):
            cur_st = float(out[i]["start"])
            cur_end = float(out[i]["end"])
            nxt_start = float(out[i + 1]["start"])
            if nxt_start - cur_end >= min_gap - 1e-9:
                continue
            shrunk = nxt_start - min_gap
            if shrunk - cur_st >= min(needs[i], min_dur) - 1e-9:
                out[i]["end"] = shrunk
            else:
                nxt_end = float(out[i + 1]["end"])
                shift = (cur_end + min_gap) - nxt_start
                out[i + 1]["start"] = nxt_start + shift
                out[i + 1]["end"] = max(nxt_end + shift, out[i + 1]["start"])
            changed = True

        # 最短时长 / 阅读速度：先向后延长，再向前借空间
        for i, ent in enumerate(out):
            st = float(ent["start"])
            ed = float(ent["end"])
            need = needs[i]
            if ed - st >= need - 1e-9:
                continue
            fwd_limit = (
                float(out[i + 1]["start"]) - min_gap if i + 1 < n else st + need
            )
            new_ed = min(st + need, max(fwd_limit, st))
            if new_ed - st < need - 1e-9:
                back_limit = float(out[i - 1]["end"]) + min_gap if i > 0 else 0.0
                new_st = max(back_limit, min(st, new_ed - need), 0.0)
            else:
                new_st = st
            if abs(new_st - st) > 1e-9 or abs(new_ed - ed) > 1e-9:
                ent["start"] = new_st
                ent["end"] = max(new_ed, new_st)
                changed = True

        if not changed:
            break

    return out


def build_subtitle_entries(session_id: str, w: int, h: int, data: list | None = None) -> list[dict]:
    """根据 final_result.json 构建后处理过的字幕条目（含 0.2s 延迟调整）。

    传入 data 时直接使用该内存数据（用于草稿预览，不读盘）；否则加载 final_result.json。
    """
    if data is None:
        _, data = _load_final_results_for_subtitles(session_id)
    if not data:
        return []
    _, _, _, _, z_max, e_max = _subtitle_play_metrics(w, h)
    srt_entries: list[dict] = []
    for seg in data:
        fallback_trans = seg.get('trans') or seg.get('trans_text') or seg.get('translation') or ''
        for sub in seg.get('sub_segments', [{"start": seg.get('start_ts'), "end": seg.get('end_ts'), "text": seg.get('text', ''), "trans": fallback_trans}]):
            txt = sub.get('text', '')
            trans = strip_display_terminal_period(sub.get('trans', ''))
            if trans:
                trans = trans.strip("。，.,？！?!;；、：: ")
            st, ed = sub.get('start', seg.get('start_ts')), sub.get('end', seg.get('end_ts'))
            is_en = is_english_text(txt)
            p_max = e_max if is_en else z_max
            p_chunks, t_chunks, p_w, t_w = _balance_bilingual_burn_chunks(
                txt, trans, p_max, z_max, config.SUBTITLE_VIDEO_MAX_LINES_PER_LANG, is_en,
            )
            if not p_chunks and not t_chunks:
                continue
            n = len(p_chunks)
            weights = []
            for i in range(n):
                pc = p_chunks[i] if i < len(p_chunks) else ""
                tc = t_chunks[i] if i < len(t_chunks) else ""
                if (pc or "").strip():
                    weights.append(max(1, len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", pc))))
                else:
                    weights.append(max(1, len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", tc))))
            ranges = _time_slices_weighted(st, ed, weights)
            trans_is_en = is_english_text(trans) if trans else False
            for i, (rs, re_) in enumerate(ranges):
                pc = p_chunks[i] if i < len(p_chunks) else ""
                tc = t_chunks[i] if i < len(t_chunks) else ""
                # 跨时过长的页再按时长拆分，避免后面被迫截断
                for ppc, ptc, prs, pre_ in _repage_long_span(
                    pc, tc, rs, re_, is_en, trans_is_en, config.SUBTITLE_MAX_DURATION_S
                ):
                    primary_display = strip_leading_video_subtitle_punct(
                        _wrap_text_unbounded_for_burn(ppc, p_w, is_en).strip()
                    )
                    trans_display = (
                        strip_leading_video_subtitle_punct(
                            _wrap_text_unbounded_for_burn(ptc, t_w, trans_is_en).strip()
                        )
                        if (ptc or "").strip()
                        else ""
                    )
                    if not primary_display and not trans_display:
                        continue
                    srt_entries.append({
                        "start": prs, "end": pre_,
                        "primary": primary_display, "trans": trans_display,
                    })

    # 必须先按时间排序：srt_entries 是按 final_result.json 的段落顺序构建的，
    # 段落之间可能时间重叠或乱序；不排序就取 [i+1] 会拿到错误的"下一条"，
    # 把某条的 end 拉到不相干的时间点上。
    srt_entries.sort(key=lambda e: (float(e.get("start") or 0.0), float(e.get("end") or 0.0)))

    # 后处理：字幕结束时间适当延迟，让话尾不被切掉。
    # 这里只允许延长、绝不缩短——原实现用 min(end + 0.2, next_start)，
    # 遇到重叠条目（next_start < end）会把 end 往回拽，反而截断字幕。
    SUBTITLE_LINGER_S = 0.2
    min_gap = config.SUBTITLE_MIN_GAP_S
    for i in range(len(srt_entries)):
        cur_end = float(srt_entries[i]["end"])
        if i + 1 < len(srt_entries):
            # 最多延长到下一条起点前 min_gap 处；若已越过则保持原样（不回拽）
            limit = float(srt_entries[i + 1]["start"]) - min_gap
            srt_entries[i]["end"] = max(cur_end, min(cur_end + SUBTITLE_LINGER_S, limit))
        else:
            srt_entries[i]["end"] = cur_end + SUBTITLE_LINGER_S
    # Netflix 合规：最短时长、阅读速度、相邻最小间隔（最长时长已在分页阶段处理）。
    srt_entries = _enforce_netflix_timing(srt_entries)
    return srt_entries


def build_subtitle_content(srt_entries: list[dict], use_thai: bool, w: int, h: int):
    """根据字幕条目生成文件内容，返回 (content_str, ext)。"""
    if use_thai:
        return _build_bilingual_thai_ass(srt_entries, w, h), "ass"
    srt_content = ""
    for idx, ent in enumerate(srt_entries, 1):
        parts = [ent["primary"]] if ent.get("primary") else []
        if ent.get("trans"):
            parts.append(ent["trans"].strip())
        dt = "\n".join(p for p in parts if p)
        if dt:
            srt_content += f"{idx}\n{format_srt_time(ent['start'])} --> {format_srt_time(ent['end'])}\n{dt}\n\n"
    return srt_content, "srt"


def _format_vtt_time(s: float) -> str:
    """WebVTT 时间戳：HH:MM:SS.mmm（毫秒用点分隔，与 SRT 的逗号不同）。"""
    if s is None or s < 0:
        s = 0.0
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(s % 60)
    ms = int(round((s - int(s)) * 1000))
    if ms == 1000:  # 四舍五入进位
        ms = 0
        sec += 1
    return f"{h:02d}:{m:02d}:{sec:02d}.{ms:03d}"


def build_subtitle_vtt(srt_entries: list[dict]) -> str:
    """把字幕条目渲染为 WebVTT，用于播放器 <track> 实时预览。

    与烧录走同一套 build_subtitle_entries 产出，保证预览与最终成片形态一致。
    """
    lines = ["WEBVTT", ""]
    for idx, ent in enumerate(srt_entries, 1):
        parts = [ent["primary"]] if ent.get("primary") else []
        if ent.get("trans"):
            parts.append(ent["trans"].strip())
        # burn 用 \n 换行；VTT 内一条 cue 的多行同样用换行分隔
        dt = "\n".join(p for p in parts if p)
        if not dt.strip():
            continue
        lines.append(str(idx))
        lines.append(f"{_format_vtt_time(ent['start'])} --> {_format_vtt_time(ent['end'])}")
        lines.append(dt)
        lines.append("")
    return "\n".join(lines)


def export_subtitle_vtt_for_session(session_id: str) -> str | None:
    """按当前 final_result.json（含用户编辑）即时生成 WebVTT 预览内容。

    返回 vtt 字符串或 None。用于播放器叠加字幕预览，不依赖已刻印视频。
    """
    try:
        w, h = 1280, 720
        video_path = _find_any_session_video_path(session_id)
        if video_path:
            try:
                dw, dh = get_video_dimensions(video_path)
                if dw and dh:
                    w, h = dw, dh
            except Exception:
                pass
        entries = build_subtitle_entries(session_id, w, h)
        if not entries:
            return None
        return build_subtitle_vtt(entries)
    except Exception as e:
        get_logger().error(f"Export VTT Error: {e}")
        return None


def export_subtitle_vtt_for_draft(session_id: str, edits: list[dict]) -> str | None:
    """把未保存的编辑草稿即时渲染为 WebVTT 预览，不落盘、不刻印。

    走与最终烧录完全相同的 apply_subtitle_edits + build_subtitle_entries 管线，
    保证草稿预览形态与保存后一致。edits 为空时等价于当前已保存内容。
    """
    try:
        _, data = _load_final_results_for_subtitles(session_id)
        if not data:
            return None
        edited = apply_subtitle_edits(data, edits)
        w, h = 1280, 720
        video_path = _find_any_session_video_path(session_id)
        if video_path:
            try:
                dw, dh = get_video_dimensions(video_path)
                if dw and dh:
                    w, h = dw, dh
            except Exception:
                pass
        entries = build_subtitle_entries(session_id, w, h, data=edited)
        if not entries:
            return None
        return build_subtitle_vtt(entries)
    except Exception as e:
        get_logger().error(f"Export draft VTT Error: {e}")
        return None


def export_subtitle_content_for_session(session_id: str, ui_language=None):
    """按当前 final_result.json（含用户编辑）即时生成字幕文件内容。

    返回 (content_str, ext) 或 (None, None)。用于旁路下载 SRT/ASS，不依赖已刻印的视频。
    """
    try:
        # 尝试用真实视频尺寸以获得准确的换行/排版；找不到则用默认尺寸
        w, h = 1280, 720
        video_path = _find_any_session_video_path(session_id)
        if video_path:
            try:
                dw, dh = get_video_dimensions(video_path)
                if dw and dh:
                    w, h = dw, dh
            except Exception:
                pass
        entries = build_subtitle_entries(session_id, w, h)
        if not entries:
            return None, None
        ui_norm = normalize_ui_language(ui_language)
        use_thai = ui_norm == "th" or _translations_need_thai_font(entries)
        return build_subtitle_content(entries, use_thai, w, h)
    except Exception as e:
        get_logger().error(f"Export SRT Error: {e}")
        return None, None


def auto_generate_subtitle_video_sync(session_id, original_filename, ui_language=None):
    video_stage_timings = {
        "subtitle_probe_s": 0.0,
        "srt_generate_s": 0.0,
        "ffmpeg_burn_s": 0.0,
    }
    try:
        video_path = _find_upload_video_path(session_id, original_filename)
        if not video_path:
            get_logger().warning(
                f"[{session_id}] 字幕视频压制跳过：未找到可用视频文件。"
                f"filename={original_filename}"
            )
            return {"url": None, "timings": video_stage_timings}

        res_path, data = _load_final_results_for_subtitles(session_id)
        out_path = os.path.join(config.CACHE_DIR, f"{session_id}_subtitled.mp4")
        if not data:
            shutil.copy2(video_path, out_path)
            return {"url": f"/cache/{session_id}_subtitled.mp4", "timings": video_stage_timings}

        probe_start = time.perf_counter()
        w, h = get_video_dimensions(video_path)
        video_stage_timings["subtitle_probe_s"] = time.perf_counter() - probe_start

        srt_start = time.perf_counter()
        srt_entries = build_subtitle_entries(session_id, w, h)

        ui_norm = normalize_ui_language(ui_language)
        use_thai_burn = ui_norm == "th" or _translations_need_thai_font(srt_entries)

        if not srt_entries:
            video_stage_timings["srt_generate_s"] = time.perf_counter() - srt_start
            shutil.copy2(video_path, out_path)
            return {"url": f"/cache/{session_id}_subtitled.mp4", "timings": video_stage_timings}

        srt_content, _ext = build_subtitle_content(srt_entries, use_thai_burn, w, h)
        if not (srt_content or "").strip():
            video_stage_timings["srt_generate_s"] = time.perf_counter() - srt_start
            shutil.copy2(video_path, out_path)
            return {"url": f"/cache/{session_id}_subtitled.mp4", "timings": video_stage_timings}

        if use_thai_burn:
            sub_path = os.path.join(config.CACHE_DIR, f"{session_id}.ass")
            with open(sub_path, "w", encoding="utf-8-sig") as f:
                f.write(srt_content)
            style_conf = ""
        else:
            sub_path = os.path.join(config.CACHE_DIR, f"{session_id}.srt")
            with open(sub_path, "w", encoding="utf-8") as f:
                f.write(srt_content)
            style_conf = get_dynamic_style_conf(dimensions=(w, h))
        video_stage_timings["srt_generate_s"] = time.perf_counter() - srt_start

        ffmpeg_start = time.perf_counter()
        rel_sub = os.path.relpath(sub_path).replace(chr(92), "/")
        vf_arg = f"subtitles={rel_sub}" + (f":{style_conf}" if style_conf else "")
        _run_subtitle_burn_ffmpeg(session_id, video_path, vf_arg, out_path)
        video_stage_timings["ffmpeg_burn_s"] = time.perf_counter() - ffmpeg_start
        return {"url": f"/cache/{session_id}_subtitled.mp4", "timings": video_stage_timings}
    except Exception as e:
        get_logger().error(f"Gen Video Error: {e}")
        return {"url": None, "timings": video_stage_timings}
