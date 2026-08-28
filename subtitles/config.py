import os

SAVE_ROOT = os.environ.get("SUBTITLE_SAVE_ROOT", "saved_data")
UPLOAD_DIR = os.path.join(SAVE_ROOT, "uploads")
SEGMENT_DIR = os.path.join(SAVE_ROOT, "segments")
CACHE_DIR = os.path.join(SAVE_ROOT, "cache")

FORCED_ALIGNER_MODEL_PATH = os.environ.get("FORCED_ALIGNER_MODEL_PATH", "").strip()

TRANSLATION_BATCH_MAX_ITEMS = int(os.environ.get("TRANSLATION_BATCH_MAX_ITEMS", "12"))
TRANSLATION_BATCH_MAX_CHARS = int(os.environ.get("TRANSLATION_BATCH_MAX_CHARS", "6000"))
CHUNK_BATCH_MAX_ITEMS = int(os.environ.get("CHUNK_BATCH_MAX_ITEMS", "12"))
CHUNK_BATCH_MAX_CHARS = int(os.environ.get("CHUNK_BATCH_MAX_CHARS", "6000"))

SUBTITLE_PUNC_TO_STRIP = "。，.,？！?!;；、 "

SUBTITLE_CJK_FONTNAME = os.environ.get("SUBTITLE_CJK_FONTNAME", "Microsoft YaHei").strip() or "Microsoft YaHei"
SUBTITLE_THAI_FONTNAME = os.environ.get("SUBTITLE_THAI_FONTNAME", "Noto Sans Thai").strip() or "Noto Sans Thai"
try:
    SUBTITLE_VIDEO_MAX_LINES_PER_LANG = max(1, int(os.environ.get("SUBTITLE_VIDEO_MAX_LINES_PER_LANG", "2")))
except ValueError:
    SUBTITLE_VIDEO_MAX_LINES_PER_LANG = 2
try:
    SUBTITLE_LINE_WIDTH_FRAC = float(os.environ.get("SUBTITLE_LINE_WIDTH_FRAC", "0.80"))
except ValueError:
    SUBTITLE_LINE_WIDTH_FRAC = 0.80
SUBTITLE_LINE_WIDTH_FRAC = min(max(SUBTITLE_LINE_WIDTH_FRAC, 0.50), 0.95)
try:
    SUBTITLE_FONT_SIZE_FILL_FRAC = float(os.environ.get("SUBTITLE_FONT_SIZE_FILL_FRAC", "0.85"))
except ValueError:
    SUBTITLE_FONT_SIZE_FILL_FRAC = 0.85
SUBTITLE_FONT_SIZE_FILL_FRAC = min(max(SUBTITLE_FONT_SIZE_FILL_FRAC, 0.55), 0.95)

# ── Netflix Timed Text Style Guide 合规参数 ──
# 单行字符上限：拉丁 42（Netflix 规范），CJK 取 20（全角字宽约为拉丁两倍）
def _env_int(name, default, lo, hi):
    try:
        val = int(os.environ.get(name, str(default)))
    except ValueError:
        val = default
    return min(max(val, lo), hi)


def _env_float(name, default, lo, hi):
    try:
        val = float(os.environ.get(name, str(default)))
    except ValueError:
        val = default
    return min(max(val, lo), hi)


SUBTITLE_MAX_LINE_CHARS = _env_int("SUBTITLE_MAX_LINE_CHARS", 42, 20, 60)
SUBTITLE_MAX_LINE_CHARS_CJK = _env_int("SUBTITLE_MAX_LINE_CHARS_CJK", 20, 10, 30)
# 最短显示时长 20 帧 @24fps；最长 7s
SUBTITLE_MIN_DURATION_S = _env_float("SUBTITLE_MIN_DURATION_S", 0.833, 0.2, 3.0)
SUBTITLE_MAX_DURATION_S = _env_float("SUBTITLE_MAX_DURATION_S", 7.0, 3.0, 15.0)
# 阅读速度上限（字符/秒）：成人 17，CJK 信息密度高故取约一半
SUBTITLE_MAX_CPS = _env_float("SUBTITLE_MAX_CPS", 17.0, 8.0, 30.0)
SUBTITLE_MAX_CPS_CJK = _env_float("SUBTITLE_MAX_CPS_CJK", 9.0, 4.0, 20.0)
# 相邻字幕最小间隔 2 帧 @24fps，防止闪烁
SUBTITLE_MIN_GAP_S = _env_float("SUBTITLE_MIN_GAP_S", 0.083, 0.0, 0.5)

FORCED_ALIGNER_LANG_MAP = {
    "zh": "Chinese", "en": "English", "yue": "Cantonese",
    "fr": "French", "de": "German", "it": "Italian",
    "ja": "Japanese", "ko": "Korean", "pt": "Portuguese",
    "ru": "Russian", "es": "Spanish",
}
