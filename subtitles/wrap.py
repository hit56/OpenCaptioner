import re

from subtitles import config
from subtitles._runtime import get_logger

def fix_english_spacing(text): return re.sub(r'([a-zA-Z])([.?!,;])([a-zA-Z])', r'\1\2 \3', text) if text else text

def strip_display_terminal_period(text):
    """Remove only sentence-final full stops from on-screen translated subtitles."""
    if not text:
        return text
    cleaned = str(text).rstrip()
    return re.sub(r'([。]|(?<!\.)\.(?!\.))(?=[\s"”’）】》」』]*$)', '', cleaned)


# 行首若出现左半且本行内存在右半，则视为成对标点，不得删除行首这一字符
_SUBTITLE_LEAD_BRACKET_PAIRS = (
    ("(", ")"),
    ("（", "）"),
    ("[", "]"),
    ("【", "】"),
    ("{", "}"),
    ("「", "」"),
    ("『", "』"),
    ("《", "》"),
    ("〈", "〉"),
    ("〔", "〕"),
    ("［", "］"),
    ("＜", "＞"),
    ("«", "»"),
    ("\u201c", "\u201d"),  # “ ”
    ("\u2018", "\u2019"),  # ‘ ’
    ('"', '"'),
)


def _subtitle_line_leading_is_paired_opener(line):
    if not line:
        return False
    ch, rest = line[0], line[1:]
    if not rest:
        return False
    for op, cl in _SUBTITLE_LEAD_BRACKET_PAIRS:
        if ch != op:
            continue
        if op == cl:
            return cl in rest
        return cl in rest
    return False


def strip_leading_video_subtitle_punct(text):
    """
    Remove leading punctuation on each visible line (per-language block).
    Avoids on-screen cues starting with ，。、等；保留行首字母/数字/CJK/泰文辅音等正文。
    成对出现的括号、书名号、弯引号等：若行首为左半且本行内存在右半，则不删除该行首字符。
    """
    if not text:
        return text
    import unicodedata as ud

    def _strip_one_line(line):
        ln = line.lstrip(" \t\u200b\u200c")
        while ln:
            ch = ln[0]
            cat = ud.category(ch)
            # 正文起始：字母、数字、中日韩、泰语辅音区常见起始
            if ch.isalnum():
                break
            if "\u4e00" <= ch <= "\u9fff":
                break
            if "\u0e00" <= ch <= "\u0e7f":
                if cat[0] == "P":
                    if _subtitle_line_leading_is_paired_opener(ln):
                        break
                    ln = ln[1:].lstrip(" \t\u200b\u200c")
                    continue
                break
            # 英文缩略行首 'word — 保留作省略/所有格
            if ch in "'\u2019" and len(ln) > 1 and (ln[1].isalpha() or ("\u4e00" <= ln[1] <= "\u9fff")):
                break
            if cat[0] == "P":
                if _subtitle_line_leading_is_paired_opener(ln):
                    break
                ln = ln[1:].lstrip(" \t\u200b\u200c")
                continue
            break
        return ln

    return "\n".join(_strip_one_line(line) for line in str(text).split("\n"))


def is_english_text(text): return len(re.findall(r'[a-zA-Z]', text or '')) > len(re.findall(r'[\u4e00-\u9fa5]', text or ''))
def format_timestamp(s): return f"{int(divmod(s, 60)[0]):02d}:{divmod(s, 60)[1]:05.2f}"
def format_srt_time(s): return f"{int(s//3600):02d}:{int((s%3600)//60):02d}:{int(s%60):02d},{int((s-int(s))*1000):03d}"

_SOFT_WRAP_CHARS = frozenset(" \t，,、。．.!?？!；;:：、…\u200b\u200c")

_THAI_SCRIPT_RE = re.compile(r"[\u0e00-\u0e7f]")


def _text_contains_thai_script(text):
    return bool(_THAI_SCRIPT_RE.search(text or ""))


def _cjk_wrap_max_columns(play_res_x, mlr, line_frac, fill_legacy):
    """
    Heuristic CJK column count for wrapping: scales with inner PlayResX width
    (≈ video aspect), calibrated near legacy 14 cols @ narrow inner / 22 @ ~462 inner,
    then scaled by line_frac vs fill_legacy (default 0.80 vs 0.85).
    """
    inner = max(float(play_res_x) - 2.0 * float(mlr), float(play_res_x) * 0.45)
    lo_i, lo_z = 146.0, 14.0
    hi_i, hi_z = 462.0, 22.0
    if inner <= lo_i:
        z_ref = max(8.0, lo_z * max(inner, 80.0) / lo_i)
    elif inner >= hi_i:
        extra = inner - hi_i
        z_ref = hi_z + min(extra / 28.0, 14.0)
    else:
        z_ref = lo_z + (hi_z - lo_z) * (inner - lo_i) / (hi_i - lo_i)
    z_raw = z_ref * (line_frac / max(float(fill_legacy), 0.5))
    return max(8, min(72, int(round(z_raw))))


def _latin_wrap_max_columns(z_cjk):
    """English/Latin wrap width vs CJK (legacy ratio ~45:22)."""
    return max(16, min(100, int(round(float(z_cjk) * (45.0 / 22.0)))))


def _subtitle_play_metrics(w, h):
    """Geometry shared by SRT force_style and bilingual ASS (matches legacy get_dynamic_style_conf)."""
    play_res_x = 288 * (w / h)
    mv, mlr = max(int(288 * 0.11), 25), max(int(play_res_x * 0.05), 8)
    play_res_x_i = int(round(play_res_x)) or 384
    z_max = _cjk_wrap_max_columns(play_res_x, mlr, config.SUBTITLE_LINE_WIDTH_FRAC, config.SUBTITLE_FONT_SIZE_FILL_FRAC)
    e_max = _latin_wrap_max_columns(z_max)
    # 字号按未截断的 z_max 计算，保持既有视觉比例（截断只用于限制每行字符数）
    fs = min(max(int(play_res_x * config.SUBTITLE_FONT_SIZE_FILL_FRAC / max(z_max, 8)), 9), 18)
    # Netflix 合规：单行字符硬上限。原值随分辨率自适应（拉丁可达 ~45+），
    # 宽屏视频会产出超过 42 字符的长行，这里统一收口。
    z_max = min(z_max, config.SUBTITLE_MAX_LINE_CHARS_CJK)
    e_max = min(e_max, config.SUBTITLE_MAX_LINE_CHARS)
    return fs, mv, mlr, play_res_x_i, z_max, e_max


def format_ass_time(seconds):
    """ASS/SSA dialogue time: H:MM:SS.cc (centiseconds)."""
    try:
        tcs = int(round(float(seconds) * 100.0))
    except (TypeError, ValueError):
        tcs = 0
    if tcs < 0:
        tcs = 0
    h, tcs = divmod(tcs, 3600 * 100)
    m, tcs = divmod(tcs, 60 * 100)
    sec, cs = divmod(tcs, 100)
    return f"{int(h)}:{int(m):02d}:{int(sec):02d}.{int(cs):02d}"


def _ass_escape_dialogue(text):
    if not text:
        return ""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\n", "\\N")
    )


def _pick_primary_subtitle_font(primary_text):
    if _text_contains_thai_script(primary_text):
        return config.SUBTITLE_THAI_FONTNAME
    return config.SUBTITLE_CJK_FONTNAME


def _ass_dialogue_body(primary_text, trans_text):
    """Inline per-line fonts so CJK/Latin and Thai translation both render."""
    p_fn = _pick_primary_subtitle_font(primary_text)
    t_fn = config.SUBTITLE_THAI_FONTNAME
    p_esc = _ass_escape_dialogue(primary_text)
    trans_text = (trans_text or "").strip()
    if not trans_text:
        return f"{{\\fn{p_fn}}}{p_esc}"
    t_esc = _ass_escape_dialogue(trans_text)
    return f"{{\\fn{p_fn}}}{p_esc}\\N{{\\fn{t_fn}}}{t_esc}"


def _build_bilingual_thai_ass(entries, w, h):
    fs, mv, mlr, prx, _z, _e = _subtitle_play_metrics(w, h)
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "ScaledBorderAndShadow: yes\n"
        f"PlayResX: {prx}\n"
        "PlayResY: 288\n"
        "WrapStyle: 1\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{config.SUBTITLE_CJK_FONTNAME},{fs},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
        f"0,0,0,0,100,100,0,0,1,2,1,2,{mlr},{mlr},{mv},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [header]
    for ent in entries:
        st, ed = ent["start"], ent["end"]
        body = _ass_dialogue_body(ent.get("primary") or "", ent.get("trans") or "")
        lines.append(
            f"Dialogue: 0,{format_ass_time(st)},{format_ass_time(ed)},Default,,0,0,0,,{body}\n"
        )
    return "".join(lines)


def _translations_need_thai_font(entries):
    for ent in entries:
        if _text_contains_thai_script(ent.get("primary")) or _text_contains_thai_script(ent.get("trans")):
            return True
    return False


def format_elapsed_time(seconds):
    try:
        total_seconds = max(int(round(float(seconds))), 0)
    except (TypeError, ValueError):
        return "0s"

    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def _subtitle_line_slack(max_chars):
    """Small extra codepoint budget so lines can absorb orphans without exceeding typical libass width."""
    max_chars = max(int(max_chars), 8)
    return max(2, min(4, int(max_chars * 0.12) + 1))


def _adjust_script_wrap_cut(cur, cut, max_chars):
    """
    Extend cut forward so Thai / Unicode combining marks are not orphaned at line start.
    Allows at most a small slack beyond max_chars (matches on-screen headroom vs heuristic width).
    """
    if cut <= 0 or cut >= len(cur):
        return cut
    import unicodedata as ud

    max_chars = max(int(max_chars), 8)
    slack = _subtitle_line_slack(max_chars)
    c = cut
    while c < len(cur):
        ch = cur[c]
        oc = ord(ch)
        if ud.combining(ch) != 0:
            c += 1
            continue
        # Thai vowels / tones / signs often need to stay with the preceding consonant
        if 0x0E31 <= oc <= 0x0E3A or 0x0E47 <= oc <= 0x0E4E:
            c += 1
            continue
        break
    if c > cut and c <= max_chars + slack:
        return c
    return cut


def _finalize_cjk_orphan_lines(wrapped, max_chars):
    """
    If the last line is only 1–2 Han characters (e.g. '西'), merge onto the previous line when
    len(prev)+len(last) <= max_chars + slack — avoids a lone character on a second line when
    the renderer still has horizontal room (heuristic max_chars is slightly conservative).
    """
    if not wrapped:
        return wrapped
    max_chars = max(int(max_chars), 8)
    slack = _subtitle_line_slack(max_chars)
    lines = wrapped.split("\n")
    changed = True
    while changed and len(lines) >= 2:
        changed = False
        prev, last = lines[-2], lines[-1]
        p, lnorm = prev.rstrip(), last.strip()
        if (
            len(lnorm) <= 2
            and lnorm
            and re.fullmatch(r"[\u4e00-\u9fff]+", lnorm)
            and len(p) + len(lnorm) <= max_chars + slack
        ):
            lines[-2] = p + lnorm
            lines.pop()
            changed = True
    return "\n".join(lines)


def _hard_wrap_single_paragraph(line, max_chars):
    """Greedy wrap so no output line exceeds max_chars (codepoints); prefers soft breaks."""
    if not line:
        return ""
    max_chars = max(int(max_chars), 8)
    s = line.strip(" \t\u200b\u200c")
    if len(s) <= max_chars:
        return s
    out = []
    cur = s
    strip_lead = lambda t: t.lstrip(" \t\u200b\u200c")
    while len(cur) > max_chars:
        cut = max_chars
        low = max(1, max_chars // 2)
        for pos in range(max_chars - 1, low - 1, -1):
            if pos < len(cur) and cur[pos - 1] in _SOFT_WRAP_CHARS:
                cut = pos
                break
            if pos < len(cur) and cur[pos] in _SOFT_WRAP_CHARS:
                cut = pos + (0 if cur[pos] in "\u200b\u200c" else 1)
                break
        cut = _adjust_script_wrap_cut(cur, cut, max_chars)
        piece = cur[:cut].rstrip(" \t\u200b\u200c")
        if not piece:
            cut = max_chars
            cut = _adjust_script_wrap_cut(cur, cut, max_chars)
            piece = cur[:cut].rstrip(" \t\u200b\u200c")
            if not piece:
                piece = cur[:max_chars]
                cut = max_chars
        out.append(piece)
        cur = strip_lead(cur[cut:])
    if cur:
        out.append(cur.strip())
    return _finalize_cjk_orphan_lines("\n".join(x for x in out if x), max_chars)


def _enforce_max_line_width(text, max_chars):
    """Apply per-line width limit to each paragraph (split by newline)."""
    if not text or max_chars < 8:
        return text
    blocks = []
    for para in str(text).split("\n"):
        wrapped = _hard_wrap_single_paragraph(para, max_chars)
        if wrapped:
            blocks.append(wrapped)
    return "\n".join(blocks)


def _limit_wrap_lines(text, max_chars, max_lines):
    """Keep at most max_lines lines; overflow merges into the last line with optional ellipsis."""
    if max_lines is None or max_lines < 1 or not text:
        return text
    max_chars = max(int(max_chars), 8)
    raw_lines = str(text).split("\n")
    if len(raw_lines) <= max_lines:
        return text
    head = "\n".join(raw_lines[: max_lines - 1])
    tail_flat = "".join(raw_lines[max_lines - 1 :])
    if len(tail_flat) <= max_chars:
        last = tail_flat
    else:
        last = tail_flat[: max_chars - 1].rstrip() + "…"
    return f"{head}\n{last}" if head else last


def smart_wrap(text, max_chars=22, max_lines=None):
    if not text:
        return ""
    if "\n" not in text and len(text) <= max_chars:
        if max_lines is None:
            return text
        return _limit_wrap_lines(text, max_chars, max_lines)
    if is_english_text(text):
        import textwrap
        lines = textwrap.wrap(text, width=max_chars, break_long_words=True, break_on_hyphens=False)
        if len(lines) <= 2:
            result = "\n".join(lines)
        else:
            # 文本超过2行宽度时，在中点附近的空格处分两行，避免 [:2] 截断丢字
            mid = len(text) // 2
            space_positions = [i for i, c in enumerate(text) if c == " "]
            if space_positions:
                best = min(space_positions, key=lambda x: abs(x - mid))
                result = text[:best] + "\n" + text[best + 1 :]
            else:
                result = "\n".join(lines)
    else:
        mid = len(text) // 2
        best_break = min(
            [i for i, c in enumerate(text) if c in "。，、？！.?!,;:]}”’）】》 "],
            key=lambda x: abs(x - mid),
            default=-1,
        )
        if best_break != -1 and best_break <= max_chars and (len(text) - best_break - 1) <= max_chars:
            result = text[: best_break + 1].strip() + "\n" + text[best_break + 1 :].strip()
        elif best_break != -1:
            # 两侧不完全等长也在标点处拆分，避免硬截断丢字
            result = text[: best_break + 1].strip() + "\n" + text[best_break + 1 :].strip()
        else:
            # 无标点可拆时在中点硬拆，避免截断丢字
            result = text[:mid] + "\n" + text[mid:]
    result = _enforce_max_line_width(result, max_chars)
    if max_lines is not None:
        result = _limit_wrap_lines(result, max_chars, max_lines)
    return result


def _rebalance_forbidden_line_ends(lines, max_chars):
    """把行尾的冠词/介词/助动词下移到下一行开头（Netflix 语法断行）。

    textwrap 只按宽度断，会留下 "...what you're" / "...of the" 这类 dangling
    行尾。这里在不超出 max_chars 的前提下，把违规行尾词移到下一行。
    """
    from subtitles.aligner import line_end_is_forbidden

    out = list(lines)
    for i in range(len(out) - 1):
        # 可能连续多个禁忌词（如 "of the"），最多下移 2 个，避免把行掏空
        for _ in range(2):
            words = out[i].split()
            if len(words) < 2 or not line_end_is_forbidden(out[i]):
                break
            moved = words[-1]
            candidate_next = (moved + " " + out[i + 1]).strip()
            if len(candidate_next) > max_chars:
                break  # 下一行放不下，保持原样
            out[i] = " ".join(words[:-1])
            out[i + 1] = candidate_next
    return [ln for ln in out if ln.strip()]


def _wrap_text_unbounded_for_burn(text, max_chars, is_english):
    """Wrap text to arbitrary line count; each line is at most max_chars (codepoints)."""
    if not text:
        return ""
    max_chars = max(int(max_chars), 8)
    if is_english:
        import textwrap

        blocks = []
        for para in str(text).split("\n"):
            p = para.strip()
            if not p:
                continue
            lines = textwrap.wrap(p, width=max_chars, break_long_words=True, break_on_hyphens=False)
            if len(lines) > 1:
                lines = _rebalance_forbidden_line_ends(lines, max_chars)
            blocks.append("\n".join(lines) if lines else p)
        return "\n".join(blocks)
    return _enforce_max_line_width(str(text).strip(), max_chars)


def _wrapped_line_count_for_burn(wrapped):
    if not wrapped:
        return 0
    return len(str(wrapped).split("\n"))


def _longest_prefix_within_line_budget(rest, max_chars, max_lines, is_english):
    """Longest prefix of rest whose unbounded wrap uses <= max_lines lines."""
    if not rest:
        return 0
    max_lines = max(1, int(max_lines))
    lo, hi = 1, len(rest)
    best = 1
    while lo <= hi:
        mid = (lo + hi) // 2
        prefix = rest[:mid]
        w = _wrap_text_unbounded_for_burn(prefix, max_chars, is_english)
        if _wrapped_line_count_for_burn(w) <= max_lines:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _extend_split_to_soft_boundary(rest, cut, max_chars, max_lines, is_english):
    """Extend cut forward while wrap still fits within max_lines (prefers longer prefixes)."""
    best = cut
    max_sl = max(1, int(max_lines))
    scan_end = min(len(rest), cut + max(max_chars * 3, 36))
    for pos in range(cut + 1, scan_end + 1):
        w = _wrap_text_unbounded_for_burn(rest[:pos], max_chars, is_english)
        if _wrapped_line_count_for_burn(w) <= max_sl:
            best = pos
        else:
            break
    for pos in range(best, cut, -1):
        if pos <= 0 or pos > len(rest):
            continue
        if rest[pos - 1] in _SOFT_WRAP_CHARS:
            w = _wrap_text_unbounded_for_burn(rest[:pos], max_chars, is_english)
            if _wrapped_line_count_for_burn(w) <= max_sl:
                return pos
    return best


def _chunk_text_for_video_pages(text, max_chars, max_lines, is_english):
    """Split text into consecutive chunks, each wrapping to at most max_lines when burned."""
    chunks = []
    rest = (text or "").strip()
    if not rest:
        return chunks
    max_lines = max(1, int(max_lines))
    safety = 0
    while rest:
        safety += 1
        if safety > 10000:
            chunks.append(rest)
            break
        cut = _longest_prefix_within_line_budget(rest, max_chars, max_lines, is_english)
        cut = _extend_split_to_soft_boundary(rest, cut, max_chars, max_lines, is_english)
        # 英文分页不得切在单词内部（会产出 "life tre" / "ats you kind" 这类断词）。
        # _extend_split_to_soft_boundary 只向前找软边界，找不到就返回按宽度算出的
        # 硬切点；这里再向后回退到最近的空白处。
        if is_english and 0 < cut < len(rest) and rest[cut - 1].isalnum() and rest[cut].isalnum():
            back = rest.rfind(" ", 0, cut)
            if back > 0:
                cut = back + 1
        chunk = rest[:cut].strip()
        if not chunk:
            cut = max(1, _longest_prefix_within_line_budget(rest, max_chars, max_lines, is_english))
            chunk = rest[:cut].strip()
            if not chunk:
                chunk = rest[0]
                cut = 1
        chunks.append(chunk)
        rest = rest[cut:].lstrip(" \t\u200b\u200c")
    return chunks


def _split_text_into_n_parts(text, n, is_english):
    """把一段文本尽量均分为 n 份，切点吸附到就近的词/标点边界。

    用于时长驱动的再分页：一页字幕跨越的语音时间过长时，需要拆成多页跟着
    人物讲话推进，而不是让一页停很久（或被截断）。

    文本太短时不强行切碎——返回的份数可能少于 n，调用方需按实际份数排时间。
    """
    src = (text or "").strip()
    if not src:
        return []

    # 每份至少要有这么多可见字符。阈值必须够大：文本少而跨时长的情况
    # （唱腔拖长音、慢速吟唱）若强行按时长切份，会切出 "And I" / "hope that"
    # 这种两词碎片各停留数秒，比一条长字幕更难读。宁可让这类整条保留。
    min_part_chars = 12 if is_english else 6
    vis_total = len(re.findall(r"[一-鿿A-Za-z0-9]", src))
    n = min(int(n), max(1, vis_total // min_part_chars))
    if n <= 1:
        return [src]

    total = len(src)
    # 允许的吸附范围：不超过等分间距的一半，避免把切点拉到隔壁份里
    window = max(4, total // (n * 2))

    cuts = []
    prev = 0
    for i in range(1, n):
        target = int(round(total * i / n))
        best = None
        for delta in range(0, window + 1):
            for cand in (target - delta, target + delta):
                if prev < cand < total and src[cand - 1] in _SOFT_WRAP_CHARS:
                    best = cand
                    break
            if best is not None:
                break
        if best is None and is_english:
            # 英文找不到软边界时回退到最近的空白，绝不切在单词内部
            back = src.rfind(" ", prev + 1, target)
            best = back + 1 if back > prev else None
        if best is None:
            best = target if target > prev else None
        if best is not None and best > prev:
            cuts.append(best)
            prev = best

    parts = []
    start = 0
    for cut in cuts:
        piece = src[start:cut].strip()
        if piece:
            parts.append(piece)
        start = cut
    tail = src[start:].strip()
    if tail:
        parts.append(tail)
    return parts or [src]


def _fit_parts_to_count(parts, n, is_english):
    """把 parts 规整为恰好 n 份：不足补空串，超出则把尾部并入最后一份。"""
    out = [p for p in (parts or [])]
    if n <= 0:
        return []
    if len(out) > n:
        joiner = " " if is_english else ""
        merged = joiner.join(out[n - 1:]).strip()
        out = out[: n - 1] + [merged]
    while len(out) < n:
        out.append("")
    return out[:n]


def _split_translation_to_match(t_src, p_chunks, t_en):
    """把译文按各主语言分页的字数占比切成同样多块，切点吸附到就近标点/空白。

    原实现把主语言与译文各自按宽度独立分页，页数虽被强行凑成一致，但第 i 页的
    中文未必对应第 i 页的英文——这正是"一行英文配错行中文"的来源。
    这里改为以主语言的分页比例为准来切译文，保证逐页语义对应。
    """
    n = len(p_chunks)
    t_src = (t_src or "").strip()
    if n <= 1 or not t_src:
        return [t_src] + [""] * (n - 1) if n else []

    def _vis(s):
        return max(len(re.findall(r"[一-鿿A-Za-z0-9]", s or "")), 1)

    weights = [_vis(c) for c in p_chunks]
    total_w = sum(weights)
    total_len = len(t_src)

    cuts = []
    acc = 0
    for w in weights[:-1]:
        acc += w
        cuts.append(int(round(total_len * (acc / total_w))))

    # 切点吸附到最近的软边界（标点/空白），最大偏移不超过 4 个字符
    snapped = []
    prev = 0
    for target in cuts:
        best = target
        for delta in range(0, 5):
            for cand in (target - delta, target + delta):
                if prev < cand < total_len and t_src[cand - 1] in _SOFT_WRAP_CHARS:
                    best = cand
                    break
            else:
                continue
            break
        best = max(best, prev)
        snapped.append(best)
        prev = best

    out = []
    start = 0
    for cut in snapped:
        out.append(t_src[start:cut].strip())
        start = cut
    out.append(t_src[start:].strip())
    while len(out) < n:
        out.append("")
    return out[:n]


def _balance_bilingual_burn_chunks(primary_raw, trans_raw, p_max, t_max, max_lines, is_p_en):
    """
    Split primary / translation into the same number of cue-sized chunks (<= max_lines each),
    adjusting wrap widths / line budget so neither side is truncated with ellipsis.
    """
    max_lines = max(1, int(max_lines))
    p_src = fix_english_spacing(primary_raw) if is_p_en else (primary_raw or "")
    p_src = (p_src or "").strip()
    t_src = (trans_raw or "").strip()
    t_en = is_english_text(t_src) if t_src else False

    if not p_src and not t_src:
        return [], [], p_max, t_max

    if not p_src:
        t_chunks = _chunk_text_for_video_pages(t_src, t_max, max_lines, t_en)
        return [""] * len(t_chunks), t_chunks, p_max, t_max

    if not t_src:
        p_chunks = _chunk_text_for_video_pages(p_src, p_max, max_lines, is_p_en)
        return p_chunks, [""] * len(p_chunks), p_max, t_max

    p_w, t_w = p_max, t_max
    ml = max_lines
    p_chunks = _chunk_text_for_video_pages(p_src, p_w, ml, is_p_en)

    # 译文按主语言的分页比例切分，保证第 i 页中英语义对应。
    # （旧实现两侧各自按宽度独立分页，页数虽凑齐但内容会错位。）
    if len(p_chunks) > 1:
        t_chunks = _split_translation_to_match(t_src, p_chunks, t_en)

        def _overflows(chunks, width):
            return any(
                _wrapped_line_count_for_burn(_wrap_text_unbounded_for_burn(tc, width, t_en)) > ml
                for tc in chunks
            )

        # 译文页装不下时，先小幅放宽宽度（上限仍受单行规范约束），
        # 不能无限放宽——放到 72 会产出 70+ 字符的超长行。
        t_cap = config.SUBTITLE_MAX_LINE_CHARS if t_en else config.SUBTITLE_MAX_LINE_CHARS_CJK
        while t_w < t_cap and _overflows(t_chunks, t_w):
            t_w += 2
        t_w = min(t_w, t_cap)

        # 仍装不下：改为让主语言分更多页（页数增加，每页更短），
        # 而不是把某一页撑成超长行。
        guard = 0
        while _overflows(t_chunks, t_w) and p_w > 8 and guard < 12:
            guard += 1
            p_w -= 2
            p_chunks = _chunk_text_for_video_pages(p_src, p_w, ml, is_p_en)
            t_chunks = _split_translation_to_match(t_src, p_chunks, t_en)

        if not _overflows(t_chunks, t_w):
            return p_chunks, t_chunks, p_w, t_w

        # 极端情况（译文远长于原文）：按比例切分无法同时满足行数与宽度约束。
        # 此时退回按各自宽度独立分页的旧路径——逐页语义对应会略差，
        # 但排版仍然合规；宁可牺牲对应精度，也不产出超宽/超行的字幕。
        p_w, t_w = p_max, t_max
        p_chunks = _chunk_text_for_video_pages(p_src, p_w, ml, is_p_en)
        t_chunks = _chunk_text_for_video_pages(t_src, t_w, ml, t_en)
        get_logger().warning(
            "Bilingual burn: proportional translation split could not satisfy line budget; "
            f"fell back to independent chunking (primary_pages={len(p_chunks)}, trans_pages={len(t_chunks)})."
        )
        if len(t_chunks) < len(p_chunks):
            t_chunks = t_chunks + [""] * (len(p_chunks) - len(t_chunks))
        elif len(t_chunks) > len(p_chunks):
            join_sep = " " if t_en else ""
            tail = join_sep.join(t_chunks[len(p_chunks) - 1:])
            t_chunks = t_chunks[: len(p_chunks) - 1] + [tail]
        return p_chunks, t_chunks, p_w, t_w

    t_chunks = _chunk_text_for_video_pages(t_src, t_w, ml, t_en)

    if len(t_chunks) <= len(p_chunks):
        t_chunks = t_chunks + [""] * (len(p_chunks) - len(t_chunks))
        return p_chunks, t_chunks, p_w, t_w

    mc = p_w
    while mc > 8 and len(_chunk_text_for_video_pages(p_src, mc, ml, is_p_en)) < len(t_chunks):
        mc -= 1
    p_chunks = _chunk_text_for_video_pages(p_src, mc, ml, is_p_en)
    p_w = mc

    if len(p_chunks) < len(t_chunks):
        tmc = t_w
        while len(t_chunks) > len(p_chunks) and tmc < 72:
            tmc += 2
            t_chunks = _chunk_text_for_video_pages(t_src, tmc, ml, t_en)
        t_w = tmc

    if len(p_chunks) < len(t_chunks) and ml > 1:
        ml2 = ml - 1
        mc = p_max
        while mc > 8 and len(_chunk_text_for_video_pages(p_src, mc, ml2, is_p_en)) < len(t_chunks):
            mc -= 1
        pc2 = _chunk_text_for_video_pages(p_src, mc, ml2, is_p_en)
        if len(pc2) >= len(t_chunks):
            p_chunks, p_w, ml = pc2, mc, ml2
            t_chunks = _chunk_text_for_video_pages(t_src, t_w, ml, t_en)

    if len(p_chunks) < len(t_chunks):
        join_sep = " " if t_en else ""
        tail = join_sep.join(t_chunks[len(p_chunks) - 1 :])
        t_chunks = t_chunks[: len(p_chunks) - 1] + [tail]
        get_logger().warning(
            "Bilingual subtitle burn: merged trailing translation chunks to match primary page count "
            f"(primary_pages={len(p_chunks)}, trans_pages_was>{len(p_chunks)})."
        )

    if len(t_chunks) < len(p_chunks):
        t_chunks = t_chunks + [""] * (len(p_chunks) - len(t_chunks))

    return p_chunks, t_chunks, p_w, t_w


def _time_slices_weighted(st, ed, weights):
    """Partition [st, ed] by positive weights; last slice ends exactly at ed."""
    st0, ed0 = float(st or 0.0), float(ed or 0.0)
    if ed0 < st0:
        ed0 = st0
    n = len(weights)
    if n == 0:
        return []
    tot = sum(max(1.0, float(w)) for w in weights)
    span = ed0 - st0
    out, acc, cur = [], 0.0, st0
    for i, w in enumerate(weights):
        acc += max(1.0, float(w))
        t_end = st0 + span * (acc / tot) if i < n - 1 else ed0
        out.append((cur, t_end))
        cur = t_end
    return out


