"""UI language helpers for subtitle pipeline (no qwen_asr dependency)."""

import re

UI_LANGUAGE_ALIASES = {
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-sg": "zh-CN",
    "zh-hans": "zh-CN",
    "zh-tw": "zh-TW",
    "zh-hk": "zh-TW",
    "zh-mo": "zh-TW",
    "zh-hant": "zh-TW",
    "en-us": "en",
    "en-gb": "en",
    "ja-jp": "ja",
    "ko-kr": "ko",
    "fr-fr": "fr",
    "de-de": "de",
    "es-es": "es",
    "ru-ru": "ru",
    "pt-br": "pt",
    "pt-pt": "pt",
    "th-th": "th",
}
UI_LANGUAGE_TO_PROMPT_NAME = {
    "zh-CN": "简体中文",
    "zh-TW": "繁體中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "fr": "Français",
    "de": "Deutsch",
    "es": "Español",
    "ru": "Русский",
    "pt": "Português",
    "th": "ไทย",
}


def normalize_ui_language(ui_language):
    raw = str(ui_language or "").strip()
    if not raw:
        return "zh-CN"

    normalized = raw.replace("_", "-")
    lowered = normalized.lower()
    if lowered in UI_LANGUAGE_ALIASES:
        return UI_LANGUAGE_ALIASES[lowered]

    base = lowered.split("-", 1)[0]
    if base == "zh":
        return "zh-CN"
    if base in UI_LANGUAGE_TO_PROMPT_NAME:
        return base
    return "zh-CN"


def ui_language_name_from_code(ui_language):
    normalized = normalize_ui_language(ui_language)
    return UI_LANGUAGE_TO_PROMPT_NAME.get(normalized, UI_LANGUAGE_TO_PROMPT_NAME["zh-CN"])


def _text_has_cjk(text):
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def _text_has_latin(text):
    return bool(re.search(r"[A-Za-z]", text or ""))


def infer_language_from_text(text):
    """Guess content language from transcript when audio detection is unreliable."""
    normalized = (text or "").strip()
    if not normalized:
        return "nospeech"

    thai_count = len(re.findall(r"[\u0e00-\u0e7f]", normalized))
    kana_count = len(re.findall(r"[\u3040-\u309f\u30a0-\u30ff]", normalized))
    hangul_count = len(re.findall(r"[\uac00-\ud7af]", normalized))
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", normalized))
    latin_count = len(re.findall(r"[A-Za-z]", normalized))

    # Kana / Hangul are decisive even when singing ASR mixed in a translation.
    if kana_count >= 3 and kana_count >= max(hangul_count, thai_count, 1):
        return "ja"
    if hangul_count >= 3 and hangul_count >= max(kana_count, thai_count, 1):
        return "ko"
    if thai_count > 0 and thai_count >= max(cjk_count, latin_count, kana_count, hangul_count, 1):
        return "th"
    if latin_count == 0 and cjk_count == 0 and kana_count == 0 and hangul_count == 0:
        return "unknown"
    if latin_count > 0 and cjk_count == 0 and kana_count == 0 and hangul_count == 0:
        return "en"
    if cjk_count > 0 and latin_count == 0:
        return "zh"
    if latin_count > cjk_count:
        return "en"
    if cjk_count > latin_count:
        return "zh"
    return "unknown"


# 用户指定的内容语种（ASR 锁定）。None = 自动检测。
ASR_LANGUAGE_ALIASES = {
    "auto": None,
    "detect": None,
    "none": None,
    "unknown": None,
    "zh": "zh",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "chinese": "zh",
    "mandarin": "zh",
    "yue": "yue",
    "cantonese": "yue",
    "zh-hk": "yue",
    "en": "en",
    "english": "en",
    "ja": "ja",
    "jp": "ja",
    "ja-jp": "ja",
    "japanese": "ja",
    "ko": "ko",
    "kr": "ko",
    "ko-kr": "ko",
    "korean": "ko",
    "th": "th",
    "th-th": "th",
    "thai": "th",
    "vi": "vi",
    "vietnamese": "vi",
    "id": "id",
    "indonesian": "id",
    "es": "es",
    "spanish": "es",
    "fr": "fr",
    "french": "fr",
    "de": "de",
    "german": "de",
    "ru": "ru",
    "russian": "ru",
    "pt": "pt",
    "portuguese": "pt",
    "ar": "ar",
    "arabic": "ar",
    "it": "it",
    "italian": "it",
}

ASR_LANGUAGE_CODES = {
    "zh", "yue", "en", "ja", "ko", "th", "vi", "id", "es", "fr", "de",
    "ru", "pt", "ar", "it", "tr", "hi", "ms", "nl", "sv", "da", "fi",
    "pl", "cs", "fil", "fa", "el", "ro", "hu", "mk",
}


def normalize_asr_language(raw_language):
    """Map user/API asr_language to an ASR code, or None for auto-detect."""
    raw = str(raw_language or "").strip()
    if not raw:
        return None
    lowered = raw.replace("_", "-").lower()
    if lowered in ASR_LANGUAGE_ALIASES:
        return ASR_LANGUAGE_ALIASES[lowered]
    if lowered in ASR_LANGUAGE_CODES:
        return lowered
    return None


ASR_LANGUAGE_DISPLAY_ZH = {
    "zh": "普通话",
    "yue": "粤语",
    "en": "英语",
    "ja": "日语",
    "ko": "韩语",
    "th": "泰语",
    "vi": "越南语",
}


def asr_language_display_name_zh(language_code):
    code = (language_code or "").strip().lower()
    return ASR_LANGUAGE_DISPLAY_ZH.get(code, language_code or "")


def should_translate_for_ui_language(language_code, ui_language="zh-CN", text=""):
    target = normalize_ui_language(ui_language)
    source = (language_code or "").strip().lower()
    normalized_text = (text or "").strip()

    if not normalized_text:
        return False

    if source in ("nospeech", "unknown"):
        inferred = infer_language_from_text(normalized_text)
        if inferred in ("unknown", "nospeech"):
            return False
        source = inferred

    if target == "zh-CN":
        if source == "zh":
            return False
        if source == "unknown":
            return not _text_has_cjk(normalized_text) or _text_has_latin(normalized_text)
        return True

    if target == "zh-TW":
        if source == "zh-tw":
            return False
        return True

    if source == target:
        return False

    if source == "unknown":
        if target == "en":
            return not (_text_has_latin(normalized_text) and not _text_has_cjk(normalized_text))
        return True

    return True
