import os
import re
import tempfile
import threading

import soundfile as sf
import torch

from subtitles import config
from subtitles._runtime import get_logger

_forced_aligner = None
_forced_aligner_lock = threading.Lock()

# ── 断行禁忌词：行尾若是冠词/介词/连词/助动词/限定词/关系代词，不宜断行 ──
# Netflix 断行规范要求在语法单元处断开，不能把 "what you're" / "a man" /
# "of the" 这类结构从中间劈开，留下 dangling 词在行尾。
# 列表可扩展；基础集为常见单音节介词、冠词、连词、助动词、物主代词、关系代词。
FORBIDDEN_LINE_END_WORDS = frozenset(
    # 冠词
    "a an the "
    # 单音节介词/连词（几乎不可能作短语末尾）
    # 注意：不含 up/down/off/out/over/through 等——它们常作副词/小品词
    # 合法结尾（"a man down" / "figure it out" / "game over"），列入会误禁。
    "of in on at to from by with for about into toward upon onto "
    "and but or nor so yet "
    # 助动词/be动词
    "am is are was were be been being do does did have has had "
    "will would shall should can could may might must "
    # 物主代词/指示代词/关系代词
    "my your his her its our their this these those that which who whom whose "
    # 比较连词
    "than".split()
)


def line_end_is_forbidden(text: str) -> bool:
    """text 的最后一个实词是否不宜作为行尾（避免 dangling 冠词/介词等）。"""
    words = re.findall(r"[A-Za-z][A-Za-z'\-]*", text or "")
    if not words:
        return False
    return words[-1].lower() in FORBIDDEN_LINE_END_WORDS


def _get_forced_aligner():
    """Lazy-load the Qwen3 ForcedAligner model (thread-safe singleton)."""
    global _forced_aligner
    if _forced_aligner is not None:
        return _forced_aligner
    with _forced_aligner_lock:
        if _forced_aligner is not None:
            return _forced_aligner
        try:
            from qwen_asr import Qwen3ForcedAligner
            if not config.FORCED_ALIGNER_MODEL_PATH:
                raise RuntimeError(
                    "FORCED_ALIGNER_MODEL_PATH is not set. Copy .env.example to .env.local."
                )
            get_logger().info(f"Loading Qwen3 ForcedAligner from {config.FORCED_ALIGNER_MODEL_PATH} ...")
            _forced_aligner = Qwen3ForcedAligner.from_pretrained(
                config.FORCED_ALIGNER_MODEL_PATH,
                dtype=torch.bfloat16,
                device_map="cuda:0",
            )
            get_logger().info("Qwen3 ForcedAligner loaded.")
        except Exception as e:
            get_logger().error(f"ForcedAligner load error: {e}")
    return _forced_aligner


def forced_align_segment(audio_path, text, lang_code, abs_start_ts, max_chars=14):
    """
    使用 Qwen3-ForcedAligner 对一个音频片段做强制对齐，
    得到字/词级别的精确时间戳后，按子句边界聚合成字幕行。

    核心策略：**只在子句/句子边界处断行**，max_chars 仅作为柔性参考。
    确保每条字幕的开头都是句子或子句的起始，而非句中截断。

    Returns:
        list[dict]  每项 {"start": float, "end": float, "text": str, "trans": str}
        或 None（对齐失败时）
    """
    lang_name = config.FORCED_ALIGNER_LANG_MAP.get(lang_code)
    if not lang_name or not text.strip():
        return None

    aligner = _get_forced_aligner()
    if aligner is None:
        return None

    try:
        results = aligner.align(audio=audio_path, text=text.strip(), language=lang_name)
        if not results or not results[0]:
            return None

        aligned_tokens = results[0]
        if not aligned_tokens:
            return None

        # 词级(英法德等) vs 字级(中日韩等)
        is_word_level = lang_code in ("en", "fr", "de", "it", "pt", "ru", "es")

        punc_strip_chars = "。，.,？！?!;；、：: \n"
        hard_punc = {"。", "？", "！", ".", "?", "!"}
        soft_punc = {"，", ",", "、", "；", ";", "：", ":"}
        all_punc = hard_punc | soft_punc
        safe_gap_chars = set("%％+-—~/()（）[]【】{}<>《》\"'“”‘’·&@#*_=|:;,，。？！?!、；： \t\r\n")

        # 硬上限：单条字幕可见字符绝对不超过此值（防止无标点的极端长文本）
        # 词级语言取 1.4 倍（max_chars=24 → 33），留出余量避免行尾贴到 42 字符上限，
        # 也便于软标点子句完整收尾而非被硬切在词中间。
        hard_max = int(max_chars * 1.4) if is_word_level else int(max_chars * 1.5)

        # 对齐质量兜底：如果 token 与原文映射之间出现大段“有字内容”被跳过，
        # 说明 ForcedAligner / 文本归一化发生了漂移；此时宁可回退比例法，
        # 也不要把整段文本错误挂到某个瞬时 cue 上，造成字幕跳闪。
        suspicious_gap_count = 0
        suspicious_gap_examples = []

        def _extract_gap_suffix(raw_gap: str):
            raw_gap = (raw_gap or "").strip()
            if not raw_gap:
                return "", 0

            lexical_parts = re.findall(r'[\u4e00-\u9fffA-Za-z0-9]+', raw_gap)
            lexical_len = sum(len(part) for part in lexical_parts)

            # 短数字/缩写（如 0-1 / BP）允许原样保留，避免过度丢字。
            if lexical_len and lexical_len <= 4 and len(raw_gap) <= 8:
                return raw_gap, lexical_len

            safe_suffix = "".join(
                ch for ch in raw_gap
                if ch in all_punc or ch.isspace() or ch in safe_gap_chars
            ).strip()
            return safe_suffix, lexical_len

        # ── 预计算子句边界 ──
        # ForcedAligner 通常不返回标点 token（标点无声学对应），
        # 因此需要将原始加标点文本的标点位置映射到 aligned token 索引上。
        clause_start_flags = [False] * len(aligned_tokens)
        clause_punc_type = [None] * len(aligned_tokens)  # 'hard' / 'soft' / None
        token_trailing_punc = [""] * len(aligned_tokens)  # 每个 token 后要补回的原文标点
        token_display_override = {}  # token_index -> 原文中的实际文本（当 ForcedAligner 归一化导致 token 与原文不一致时使用）
        _next_is_clause_start = True
        _pending_punc_type = 'hard'  # 文本起始视为硬边界
        _text_ptr = 0
        _orig = text.strip()

        def _fuzzy_find_span(token_text, orig, start_pos):
            """
            当 token 文本经过 ForcedAligner 内部归一化（去除标点/运算符）导致
            无法在原文中精确匹配时，用逐字符子序列匹配找到原文中对应的跨度。
            例如: token "tricks615" 匹配原文 "tricks.6-1=5" → span (14, 26)
            返回 (span_start, span_end) 或 (-1, -1) 匹配失败。
            """
            pos = start_pos
            first_pos = -1
            last_pos = -1
            for ch in token_text:
                found = orig.find(ch, pos)
                if found < 0:
                    return -1, -1
                if first_pos < 0:
                    first_pos = found
                last_pos = found
                pos = found + 1
            return first_pos, last_pos + 1

        for _ti, _tok in enumerate(aligned_tokens):
            _t = _tok.text
            if _t in all_punc:
                _next_is_clause_start = True
                _pending_punc_type = 'hard' if _t in hard_punc else 'soft'
                clause_start_flags[_ti] = False
                _match_pos = _orig.find(_t, _text_ptr)
                if _match_pos >= 0:
                    _text_ptr = _match_pos + len(_t)
            else:
                # 先定位当前 token 在原文中的精确位置
                _match_pos = _orig.find(_t, _text_ptr)
                _token_end = _match_pos + len(_t) if _match_pos >= 0 else -1

                if _match_pos < 0:
                    # 精确匹配失败 —— ForcedAligner 可能对文本做了归一化
                    # (如 "tricks.6-1=5" → "tricks615")，尝试模糊子序列匹配
                    _fuzzy_start, _fuzzy_end = _fuzzy_find_span(_t, _orig, _text_ptr)
                    if _fuzzy_start >= 0:
                        _match_pos = _fuzzy_start
                        _token_end = _fuzzy_end
                        # 用原文中的实际文本替代归一化后的 token 文本，保留标点/运算符
                        _orig_span = _orig[_fuzzy_start:_fuzzy_end]
                        if _orig_span != _t:
                            token_display_override[_ti] = _orig_span
                    else:
                        _match_pos = _text_ptr  # 最终兜底：找不到就假设紧接着
                        _token_end = _text_ptr + len(_t)

                # _text_ptr.._match_pos 之间的内容大多应当只是标点/空白。
                # 若夹带大段词汇，说明对齐漂移，不能再整段挂到上一个 token 尾部。
                _between = _orig[_text_ptr:_match_pos]
                if _between and _ti > 0:
                    _gap_suffix, _gap_lexical_len = _extract_gap_suffix(_between)
                    if _gap_lexical_len > max(12, len(_t) * 4):
                        suspicious_gap_count += 1
                        if len(suspicious_gap_examples) < 3:
                            suspicious_gap_examples.append(_between[:48].replace("\n", " "))
                    if _gap_suffix:
                        token_trailing_punc[_ti - 1] = _gap_suffix

                # 从间隔中提取标点信息，用于子句边界判断
                for _ch in _between:
                    if _ch in all_punc:
                        _next_is_clause_start = True
                        _pending_punc_type = 'hard' if _ch in hard_punc else 'soft'

                clause_start_flags[_ti] = _next_is_clause_start
                if _next_is_clause_start:
                    clause_punc_type[_ti] = _pending_punc_type
                _next_is_clause_start = False
                _text_ptr = _token_end

        # ── 辅助：计算可见字符数 ──
        def _visible_count(tokens):
            if is_word_level:
                return sum(len(t[0]) for t in tokens if t[0] not in all_punc and t[0].strip())
            return sum(1 for t in tokens if t[0] not in all_punc and t[0].strip())

        subtitles = []
        current_tokens = []  # list of (text, start_time, end_time, global_index)

        for tok_idx, token in enumerate(aligned_tokens):
            t_text = token.text
            t_start = token.start_time + abs_start_ts
            t_end = token.end_time + abs_start_ts
            current_tokens.append((t_text, t_start, t_end, tok_idx))

            visible_len = _visible_count(current_tokens)

            # ── 子句边界优先的断行策略 ──
            # 判断当前 token 是否处于子句末尾（即下一个 token 是新子句起始）
            at_clause_end = (
                tok_idx + 1 >= len(aligned_tokens)
                or clause_start_flags[tok_idx + 1]
                or t_text in all_punc
            )

            should_break = False

            if is_word_level:
                # ── 英文等词级语言：偏好子句边界（逗号等软标点）处断行 ──
                # 目标：一行一句/一个子句，避免长句（尤其歌词）累积成一大坨。
                # 多行字幕在屏幕上同时显示时，字幕会自动折行，但折行点不理想。
                # 设置为在子句边界主动断行，让每行都是完整的语义单元。
                if at_clause_end and visible_len >= 4:
                    next_ptype = clause_punc_type[tok_idx + 1] if tok_idx + 1 < len(aligned_tokens) else 'hard'
                    is_hard = (next_ptype == 'hard' or t_text in hard_punc)
                    if is_hard:
                        # 句末（句号/问号/感叹号）：累积了内容就断行
                        should_break = True
                    elif visible_len >= max_chars:
                        # 子句边界（逗号/分号等）且已到目标长度：断行，
                        # 让子句独立成行，而不是等堆到 hard_max 才被迫断开。
                        should_break = True
                elif visible_len >= hard_max:
                    # 安全阀：完全无标点的超长文本，按词数硬断
                    should_break = True

                # 语法断行：若断点会把冠词/介词/助动词留在行尾，且尚未超出硬上限，
                # 则推迟到下一个 token 再断，避免 "what you're" 被劈开。
                if should_break and visible_len < hard_max:
                    _tail = " ".join(t[0] for t in current_tokens)
                    if line_end_is_forbidden(_tail):
                        should_break = False
            else:
                # ── 中日韩等字级语言：更积极的断行策略 ──
                if at_clause_end and visible_len >= 4:
                    if visible_len >= max_chars:
                        # 已达到/超过目标长度，在子句边界处断行
                        should_break = True
                    elif visible_len >= int(max_chars * 0.6):
                        # 超过60%：如果边界前是硬标点(句号/问号/感叹号)则断行
                        next_ptype = clause_punc_type[tok_idx + 1] if tok_idx + 1 < len(aligned_tokens) else 'hard'
                        if next_ptype == 'hard' or t_text in hard_punc:
                            should_break = True
                    elif visible_len >= int(max_chars * 0.7):
                        # 超过70%：软标点也可以断行
                        should_break = True
                elif visible_len >= hard_max:
                    # 安全阀：单个子句超长（极少触发），强制在此处断行
                    should_break = True

            if should_break and current_tokens:
                if is_word_level:
                    display = " ".join(token_display_override.get(t[3], t[0]) + token_trailing_punc[t[3]] for t in current_tokens)
                else:
                    display = "".join(token_display_override.get(t[3], t[0]) + token_trailing_punc[t[3]] for t in current_tokens)
                clean = display.rstrip(punc_strip_chars)
                if clean:
                    subtitles.append({
                        "start": current_tokens[0][1],
                        "end": current_tokens[-1][2],
                        "text": clean,
                        "trans": ""
                    })
                current_tokens = []

        # 冲刷剩余
        if current_tokens:
            if is_word_level:
                display = " ".join(token_display_override.get(t[3], t[0]) + token_trailing_punc[t[3]] for t in current_tokens)
            else:
                display = "".join(token_display_override.get(t[3], t[0]) + token_trailing_punc[t[3]] for t in current_tokens)
            clean = display.rstrip(punc_strip_chars)
            if clean:
                subtitles.append({
                    "start": current_tokens[0][1],
                    "end": current_tokens[-1][2],
                    "text": clean,
                    "trans": ""
                })

        if not subtitles:
            return None

        def _subtitle_visible_len(val: str):
            return len(re.findall(r'[\u4e00-\u9fffA-Za-z0-9]', val or ""))

        # \u2500\u2500 \u4fee\u590d\u584c\u7f29\u7684\u65f6\u95f4\u6233\uff0c\u800c\u975e\u6574\u6bb5\u5426\u51b3 \u2500\u2500
        # \u5386\u53f2 bug\uff1aForcedAligner \u5728\u957f\u97f3\u9891\u4e0a\u4f1a\u628a token \u65f6\u95f4\u6233\u584c\u7f29\u5230\u540c\u4e00\u70b9\uff0c\u4ea7\u751f
        # \u96f6\u5bbd\u5ea6 span\uff08\u5b9e\u6d4b 16/18 \u6761 start==end\uff09\u3002\u65e7\u903b\u8f91\u53ea\u8981\u53d1\u73b0 \u22651 \u6761\u5f02\u5e38\u5c31
        # return None \u56de\u9000\u6bd4\u4f8b\u6cd5\uff0c\u5bfc\u81f4\u6574\u6bb5\u4f5c\u5e9f\u3001\u540e\u9762\u7684\u65ad\u884c\u903b\u8f91\u4ece\u672a\u6267\u884c\u3002
        #
        # \u8fd9\u91cc\u4e0d\u4e22\u5f03\u6761\u76ee\u2014\u2014\u4e22\u5f03\u7b49\u4e8e\u4e22\u8bcd\uff0c\u6b63\u662f\u8981\u907f\u514d\u7684\u95ee\u9898\u2014\u2014\u800c\u662f\u628a\u584c\u7f29\u6761\u76ee\u7684\u65f6\u95f4
        # \u6309\u90bb\u5c45\u63d2\u503c\u4fee\u590d\uff1a\u5728\u524d\u4e00\u6761\u7ed3\u675f\u4e0e\u540e\u4e00\u6761\u5f00\u59cb\u4e4b\u95f4\uff0c\u6309\u53ef\u89c1\u5b57\u7b26\u6570\u5206\u914d\u65f6\u957f\u3002
        # \u53ea\u6709\u5f53\u5f02\u5e38\u5360\u6bd4\u8fc7\u9ad8\uff08\u5bf9\u9f50\u6574\u4f53\u6f02\u79fb\uff0c\u63d2\u503c\u4e5f\u4e0d\u53ef\u4fe1\uff09\u624d\u56de\u9000\u6bd4\u4f8b\u6cd5\u3002
        cps_limit = 22 if is_word_level else 14
        bad_idx = []
        for _i, _sub in enumerate(subtitles):
            _dur = max(_sub['end'] - _sub['start'], 1e-3)
            _vis = _subtitle_visible_len(_sub['text'])
            _cps = _vis / _dur
            is_collapsed = (_sub['end'] - _sub['start']) < 1e-3
            if (
                (_vis > hard_max * 2 and _cps > cps_limit)
                or (_dur < 0.20 and _vis >= 10)
                or is_collapsed
            ):
                bad_idx.append(_i)

        bad_ratio = len(bad_idx) / len(subtitles) if subtitles else 0.0

        # \u5f02\u5e38\u8fc7\u591a\u6216\u5bf9\u9f50\u6f02\u79fb\u660e\u663e\uff1a\u63d2\u503c\u4e5f\u6551\u4e0d\u56de\u6765\uff0c\u56de\u9000\u6bd4\u4f8b\u6cd5
        if bad_ratio > 0.6 or suspicious_gap_count >= 2:
            get_logger().warning(
                "ForcedAligner sanity check failed; fallback to ratio mode. "
                f"gap_count={suspicious_gap_count}, gap_examples={suspicious_gap_examples}, "
                f"bad_cues={len(bad_idx)}/{len(subtitles)}"
            )
            return None

        if bad_idx:
            # \u9010\u4e2a\u8fde\u7eed\u5f02\u5e38\u533a\u95f4\u505a\u63d2\u503c\uff1a\u951a\u70b9\u53d6\u524d\u4e00\u6761\u6b63\u5e38\u6761\u76ee\u7684 end \u4e0e\u540e\u4e00\u6761\u6b63\u5e38\u6761\u76ee\u7684 start
            bad_set = set(bad_idx)
            n = len(subtitles)
            i = 0
            while i < n:
                if i not in bad_set:
                    i += 1
                    continue
                j = i
                while j + 1 < n and (j + 1) in bad_set:
                    j += 1
                # [i..j] \u4e3a\u4e00\u6bb5\u8fde\u7eed\u5f02\u5e38\uff0c\u53d6\u5de6\u53f3\u951a\u70b9
                left = subtitles[i - 1]['end'] if i > 0 else subtitles[i]['start']
                right = subtitles[j + 1]['start'] if j + 1 < n else max(
                    subtitles[j]['end'], left
                )
                if right <= left:
                    right = left
                weights = [
                    max(_subtitle_visible_len(subtitles[k]['text']), 1)
                    for k in range(i, j + 1)
                ]
                total_w = sum(weights)
                span = right - left
                cursor = left
                for off, k in enumerate(range(i, j + 1)):
                    share = span * (weights[off] / total_w) if total_w else 0.0
                    subtitles[k]['start'] = cursor
                    subtitles[k]['end'] = cursor + share
                    cursor += share
                i = j + 1
            get_logger().warning(
                "ForcedAligner: repaired collapsed cue timings by interpolation. "
                f"repaired={len(bad_idx)}/{len(subtitles)}, "
                f"gap_count={suspicious_gap_count}"
            )

        return subtitles

    except Exception as e:
        get_logger().error(f"ForcedAligner error: {e}")
        return None


def _split_text_by_silence_chunks(audio_path, text, audio_duration, sr, min_silence_s=0.6):
    """按音频静音处的数量把文本切成同样多块（无标点长文本的兜底）。

    歌词常常整段没有任何标点，按句/软标点都切不开。这里用 RMS 能量谷找出真实
    停顿，得到块数 N，再把文本按词序列均分为 N 块，使每块大致对应一个乐句。
    返回 list[str]，失败返回 None。
    """
    try:
        data, _ = sf.read(audio_path, dtype='float32')
        if data.ndim > 1:
            data = data[:, 0]
        if data.size == 0:
            return None

        win = max(int(sr * 0.05), 1)  # 50ms 帧
        n_frames = data.size // win
        if n_frames < 4:
            return None
        frames = data[: n_frames * win].reshape(n_frames, win)
        rms = (frames ** 2).mean(axis=1) ** 0.5

        # 静音阈值：取整体能量的低分位，避免被整体音量影响
        import numpy as _np
        thresh = max(float(_np.percentile(rms, 20)) * 1.2, 1e-5)
        min_frames = max(int(min_silence_s / 0.05), 1)

        # 找出连续低能量区间（静音），其数量决定分块数
        silences = 0
        run = 0
        for v in rms:
            if v < thresh:
                run += 1
            else:
                if run >= min_frames:
                    silences += 1
                run = 0
        if run >= min_frames:
            silences += 1

        # 块数 = 静音段数 + 1，并限制在合理范围内
        n_chunks = min(max(silences + 1, 2), 12)
        if n_chunks < 2:
            return None

        # 按词序列均分文本（拉丁语按空白，CJK 按字）
        tokens = text.split() if ' ' in text else list(text)
        if len(tokens) < n_chunks:
            return None
        joiner = ' ' if ' ' in text else ''
        per = len(tokens) / n_chunks
        parts = []
        for i in range(n_chunks):
            lo = int(round(i * per))
            hi = int(round((i + 1) * per)) if i < n_chunks - 1 else len(tokens)
            if hi > lo:
                parts.append(joiner.join(tokens[lo:hi]))
        return parts if len(parts) >= 2 else None
    except Exception as e:
        get_logger().warning(f"silence-based text split failed: {e}")
        return None


def chunked_forced_align_segment(audio_path, text, lang_code, abs_start_ts, max_chars=14, chunk_target_s=90):
    """
    分段 ForcedAligner：将长音频+文本按句切成 ~chunk_target_s 秒小块，
    分别对齐后合并。当整段对齐失败时用作中间兜底，效果远优于纯比例估算。
    """
    info = sf.info(audio_path)
    audio_duration = info.duration
    sr = info.samplerate

    if audio_duration < 10 or not text.strip():
        return None

    # 按句号/问号/感叹号切句子
    parts = re.split(r'(?<=[。？！.?!])', text.strip())
    parts = [p for p in parts if p.strip()]
    if len(parts) < 2:
        # 无句末标点（典型如歌词）：退化为按软标点/空白切分，
        # 否则这里直接 return None，长段只能走比例法，歌词时间轴必然错乱。
        parts = [p for p in re.split(r'(?<=[，,、；;：:])|\n+', text.strip()) if p and p.strip()]
    if len(parts) < 2:
        # 连软标点也没有：按静音切分音频，再按块数均分文本词序列。
        # 这样至少让每块落在真实停顿处，而不是整段硬套比例。
        parts = _split_text_by_silence_chunks(audio_path, text.strip(), audio_duration, sr)
    if not parts or len(parts) < 2:
        return None

    total_chars = sum(len(re.sub(r'\s+', '', p)) for p in parts)
    if total_chars == 0:
        return None

    # 按字数比例估算每句的音频时段，累积到 ~chunk_target_s 一组
    chunks = []
    buf_sentences = []
    buf_chars = 0
    prev_end_ratio = 0.0

    for i, p in enumerate(parts):
        p_chars = len(re.sub(r'\s+', '', p))
        buf_sentences.append(p)
        buf_chars += p_chars
        estimated_dur = audio_duration * buf_chars / total_chars

        if estimated_dur >= chunk_target_s or i == len(parts) - 1:
            end_ratio = prev_end_ratio + buf_chars / total_chars
            chunks.append({
                'sentences': list(buf_sentences),
                'text': ''.join(buf_sentences),
                'start_ratio': prev_end_ratio,
                'end_ratio': end_ratio,
            })
            prev_end_ratio = end_ratio
            buf_sentences = []
            buf_chars = 0

    if not chunks:
        return None

    # ── 文本重叠：每块把上一块的最后一句一起送进去对齐 ──
    # 关键点：模型是"强制"对齐的，窗口里每一段音频都会被指派给某段文本。
    # 若窗口开头的音频属于上一句、而文本里没有这句，模型只能把本块首句硬套
    # 上去——实测 "Goodbye" 被钉到 73.60（真实约 75.2），后续整块提前，
    # "Please don't cry" 提前 7.5 秒。
    # 带上一句后，那段音频有了正确归属，本块首句才落到真正的位置：
    # "Please don't cry" 由 74.56 修正到 82.78（真值 82.12）。
    # 重叠句的对齐结果只用于定位，最后丢弃（它已由上一块产出）。
    for ci in range(1, len(chunks)):
        prev_sents = chunks[ci - 1]['sentences']
        if not prev_sents:
            continue
        overlap = prev_sents[-1]
        chunks[ci]['overlap_text'] = overlap
        chunks[ci]['text'] = overlap + ''.join(chunks[ci]['sentences'])

    punc_strip_chars = "。，.,？！?!;；、：: \n"
    all_subs = []
    import tempfile

    # 逐块顺序推进：每块的读取窗口从"上一块实际对齐到的结束时间"开始，
    # 而不是按字数比例估算的位置。
    #
    # 为什么必须这样：字数比例在歌唱上严重失真（"always love you" 字少但拖唱数秒），
    # 用它算出的窗口起点会落在上一句的中间。此时窗口开头那段音频属于上一句，
    # 但文本里没有对应内容，模型只能把本块首句硬套到这段音频上——实测
    # "Goodbye" 被钉到 69.22（真实约 75.2），之后整块顺延，"Please don't cry"
    # 提前 7.5 秒出现。改为链式推进后该句落到 82.78（真值 82.12）。
    cursor_rel = 0.0  # 上一块结束位置（相对本段音频起点，秒）
    prev_last_start_rel = None  # 上一块最后一句的起点（相对，秒）

    for ci, chunk in enumerate(chunks):
        est_start = audio_duration * chunk['start_ratio']
        est_end = audio_duration * chunk['end_ratio']

        # 窗口起点：本块带了上一句文本，因此窗口必须回退到"上一句的起点"，
        # 让那句有对应音频可对。prev_start_rel 由上一块对齐结果给出（最可靠）。
        if ci == 0:
            read_start = 0.0
        elif prev_last_start_rel is not None:
            read_start = max(0.0, prev_last_start_rel)
        else:
            read_start = max(0.0, min(cursor_rel, audio_duration) - 0.3)

        # 窗口终点：比例估算的结束位置 + 余量。起点已右移，这里需相应放宽，
        # 否则窗口会短于本块实际所需音频而截断末句。
        drift = max(0.0, read_start - max(0.0, est_start - 3.0))
        read_end = min(audio_duration, est_end + 3.0 + drift)
        if read_end - read_start < 0.5:
            read_end = min(audio_duration, read_start + 0.5)

        start_frame = int(read_start * sr)
        end_frame = int(read_end * sr)
        if end_frame <= start_frame:
            continue

        data, _ = sf.read(audio_path, start=start_frame, stop=end_frame, dtype='float32')

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
                sf.write(tmp.name, data, sr)
                tmp_path = tmp.name

            chunk_abs_start = abs_start_ts + read_start
            chunk_subs = forced_align_segment(
                tmp_path, chunk['text'], lang_code, chunk_abs_start, max_chars
            )

            if chunk_subs:
                # 丢弃重叠句产生的 cue：它只用于把窗口开头的音频"占住"，
                # 正文已由上一块输出，保留会重复。
                # 重叠句可能被拆成多条 cue，故按可见字符数逐条抵扣。
                kept = chunk_subs
                ov = chunk.get('overlap_text')
                if ov:
                    ov_budget = len(re.findall(r'[一-鿿A-Za-z0-9]', ov))
                    idx = 0
                    while idx < len(kept) and ov_budget > 0:
                        c_len = len(re.findall(r'[一-鿿A-Za-z0-9]', kept[idx]['text'] or ""))
                        # 只在明显仍属于重叠句时丢弃，避免把正文首句误删
                        if c_len <= ov_budget + 2:
                            ov_budget -= c_len
                            idx += 1
                        else:
                            break
                    kept = kept[idx:]
                if not kept:
                    # 全被判为重叠（本块正文极短）：本块不产出 cue。
                    # 不能走下面的比例兜底——那会把已由上一块输出的内容再写一遍。
                    get_logger().warning(
                        f"chunked_forced_align chunk {ci}: all cues consumed by overlap; skipped."
                    )
                else:
                    # 只做下界约束：本块 cue 不得早于已推进到的位置。
                    lo = abs_start_ts + cursor_rel if ci > 0 else abs_start_ts + read_start
                    fixed = []
                    for s in kept:
                        st = max(s['start'], lo)
                        en = max(s['end'], st)
                        fixed.append({**s, 'start': st, 'end': en})
                    all_subs.extend(fixed)
                    cursor_rel = max(cursor_rel, max(s['end'] for s in fixed) - abs_start_ts)
                    prev_last_start_rel = fixed[-1]['start'] - abs_start_ts
            else:
                # 此块对齐失败，该块退回比例估算（但起点不早于已推进位置）
                chunk_start = max(abs_start_ts + cursor_rel,
                                  abs_start_ts + audio_duration * chunk['start_ratio'])
                chunk_end = max(chunk_start,
                                abs_start_ts + audio_duration * chunk['end_ratio'])
                all_subs.append({
                    'start': chunk_start,
                    'end': chunk_end,
                    'text': chunk['text'].strip().rstrip(punc_strip_chars),
                    'trans': ''
                })
                cursor_rel = max(cursor_rel, chunk_end - abs_start_ts)
        except Exception as e:
            get_logger().warning(f"chunked_forced_align chunk {ci} error: {e}")
            chunk_start = max(abs_start_ts + cursor_rel,
                              abs_start_ts + audio_duration * chunk['start_ratio'])
            chunk_end = max(chunk_start,
                            abs_start_ts + audio_duration * chunk['end_ratio'])
            all_subs.append({
                'start': chunk_start,
                'end': chunk_end,
                'text': chunk['text'].strip().rstrip(punc_strip_chars),
                'trans': ''
            })
            cursor_rel = max(cursor_rel, chunk_end - abs_start_ts)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    if not all_subs:
        return None

    # ── 单调化：cue 的时间必须与文本顺序一致 ──
    # all_subs 已按文本顺序排列（逐块、块内逐句），绝不能重新排序——
    # 那只会把错位的时间"合理化"，让歌词配到别人的位置上。
    # 正确做法是：以文本顺序为准，修掉不单调/缺失的时间。
    n = len(all_subs)

    # 1) 缺时间的条目（被判定落在窗口外）先标记，稍后按邻居插值
    # 2) 强制 start 单调不减；越界的条目一并标记为待插值
    prev_end = None
    for s in all_subs:
        if s['start'] is None:
            continue
        if prev_end is not None and s['start'] < prev_end - 1e-6:
            # 与前一条冲突：若整体落在前面，视为错位，交给插值
            if s['end'] <= prev_end + 1e-6:
                s['start'] = s['end'] = None
                continue
            s['start'] = prev_end
        if s['end'] < s['start']:
            s['end'] = s['start']
        prev_end = s['end']

    # 3) 对连续的缺失区间按可见字数插值（与 forced_align_segment 的修复同策略）
    i = 0
    repaired = 0
    while i < n:
        if all_subs[i]['start'] is not None:
            i += 1
            continue
        j = i
        while j + 1 < n and all_subs[j + 1]['start'] is None:
            j += 1
        left = all_subs[i - 1]['end'] if i > 0 and all_subs[i - 1]['end'] is not None else abs_start_ts
        right = None
        for k in range(j + 1, n):
            if all_subs[k]['start'] is not None:
                right = all_subs[k]['start']
                break
        if right is None:
            right = abs_start_ts + audio_duration
        if right < left:
            right = left
        weights = [max(len(re.findall(r'[一-鿿A-Za-z0-9]', all_subs[k]['text'] or "")), 1)
                   for k in range(i, j + 1)]
        total_w = sum(weights)
        span = right - left
        cursor = left
        for off, k in enumerate(range(i, j + 1)):
            share = span * (weights[off] / total_w) if total_w else 0.0
            all_subs[k]['start'] = cursor
            all_subs[k]['end'] = cursor + share
            cursor += share
            repaired += 1
        i = j + 1

    if repaired:
        get_logger().warning(
            f"chunked_forced_align: repaired {repaired}/{n} out-of-window cues by interpolation."
        )

    return all_subs
