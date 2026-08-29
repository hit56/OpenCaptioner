import os
import re
import threading

import torch


def _ensure_transformers_kernel_compat():
    try:
        from transformers import integrations as hf_integrations
    except Exception:
        return

    if hasattr(hf_integrations, "use_kernel_forward_from_hub"):
        return

    def use_kernel_forward_from_hub(_kernel_name):
        def decorator(target):
            return target

        return decorator

    hf_integrations.use_kernel_forward_from_hub = use_kernel_forward_from_hub


_ensure_transformers_kernel_compat()

from qwen_asr import Qwen3ASRModel


QWEN3_OFFLINE_MODEL_PATH = os.environ.get("QWEN3_OFFLINE_MODEL_PATH", "").strip()
QWEN3_OFFLINE_MAX_BATCH_SIZE = int(os.environ.get("QWEN3_OFFLINE_MAX_BATCH_SIZE", "8"))
QWEN3_OFFLINE_MAX_NEW_TOKENS = int(os.environ.get("QWEN3_OFFLINE_MAX_NEW_TOKENS", "256"))

LANG_NAME_TO_CODE = {
    "Chinese": "zh",
    "English": "en",
    "Cantonese": "yue",
    "Arabic": "ar",
    "German": "de",
    "French": "fr",
    "Spanish": "es",
    "Portuguese": "pt",
    "Indonesian": "id",
    "Italian": "it",
    "Korean": "ko",
    "Russian": "ru",
    "Thai": "th",
    "Vietnamese": "vi",
    "Japanese": "ja",
    "Turkish": "tr",
    "Hindi": "hi",
    "Malay": "ms",
    "Dutch": "nl",
    "Swedish": "sv",
    "Danish": "da",
    "Finnish": "fi",
    "Polish": "pl",
    "Czech": "cs",
    "Filipino": "fil",
    "Persian": "fa",
    "Greek": "el",
    "Romanian": "ro",
    "Hungarian": "hu",
    "Macedonian": "mk",
}
LANG_CODE_TO_NAME = {code: name for name, code in LANG_NAME_TO_CODE.items()}

LANG_CODE_TO_DISPLAY_ZH = {
    "zh": "普通话", "yue": "粤语", "en": "英语", "ar": "阿拉伯语",
    "de": "德语", "fr": "法语", "es": "西班牙语", "pt": "葡萄牙语",
    "id": "印尼语", "it": "意大利语", "ko": "韩语", "ru": "俄语",
    "th": "泰语", "vi": "越南语", "ja": "日语", "tr": "土耳其语",
    "hi": "印地语", "ms": "马来语", "nl": "荷兰语", "sv": "瑞典语",
    "da": "丹麦语", "fi": "芬兰语", "pl": "波兰语", "cs": "捷克语",
    "fil": "菲律宾语", "fa": "波斯语", "el": "希腊语", "ro": "罗马尼亚语",
    "hu": "匈牙利语", "mk": "马其顿语",
}


def qwen_language_display_name_zh(language_code):
    return LANG_CODE_TO_DISPLAY_ZH.get(language_code, LANG_CODE_TO_NAME.get(language_code, language_code))
FIRERED_ROUTE_LANGS = {"zh", "en", "yue"}


def normalize_qwen_language(raw_language, raw_text=""):
    language = (raw_language or "").strip()
    text = (raw_text or "").strip()
    if not text:
        return "nospeech"
    if language in LANG_NAME_TO_CODE:
        return LANG_NAME_TO_CODE[language]

    lower_language = language.lower()
    if lower_language in {"chinese", "zh", "mandarin"}:
        return "zh"
    if lower_language in {"english", "en"}:
        return "en"
    if lower_language in {"cantonese", "yue"}:
        return "yue"
    if lower_language in {"japanese", "ja"}:
        return "ja"
    if lower_language in {"korean", "ko"}:
        return "ko"
    if lower_language in {"thai", "th"}:
        return "th"
    if lower_language in {"vietnamese", "vi"}:
        return "vi"
    if any(token in lower_language for token in ["dialect", "wu", "minnan", "mandarin"]):
        return "zh"
    if lower_language in LANG_NAME_TO_CODE.values():
        return lower_language
    return "unknown"


def qwen_language_name_from_code(language_code):
    return LANG_CODE_TO_NAME.get(language_code)


def should_use_firered_asr(language_code):
    return language_code in FIRERED_ROUTE_LANGS


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
    return bool(re.search(r'[\u4e00-\u9fff]', text or ""))


def _text_has_latin(text):
    return bool(re.search(r'[A-Za-z]', text or ""))


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


def should_translate_to_chinese(language_code, text=""):
    return should_translate_for_ui_language(language_code, "zh-CN", text)


def _default_device_map():
    return "cuda:0" if torch.cuda.is_available() else "cpu"


class QwenOfflineASR:
    def __init__(self, logger=None):
        self.logger = logger
        self._model = None
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()

    def preload(self):
        self._ensure_model()

    def _ensure_model(self):
        if self._model is not None:
            return self._model

        with self._load_lock:
            if self._model is not None:
                return self._model

            if not QWEN3_OFFLINE_MODEL_PATH:
                raise RuntimeError(
                    "QWEN3_OFFLINE_MODEL_PATH is not set. Copy .env.example to .env.local and set the model path."
                )

            init_kwargs = {
                "dtype": torch.bfloat16 if torch.cuda.is_available() else torch.float32,
                "device_map": _default_device_map(),
                "max_inference_batch_size": QWEN3_OFFLINE_MAX_BATCH_SIZE,
                "max_new_tokens": QWEN3_OFFLINE_MAX_NEW_TOKENS,
            }
            attn_impl = os.environ.get("QWEN3_OFFLINE_ATTN_IMPL", "").strip()
            if attn_impl:
                init_kwargs["attn_implementation"] = attn_impl

            if self.logger:
                self.logger.info(f"Loading Qwen3 offline ASR model from {QWEN3_OFFLINE_MODEL_PATH}...")

            self._model = Qwen3ASRModel.from_pretrained(QWEN3_OFFLINE_MODEL_PATH, **init_kwargs)
            return self._model

    def transcribe(self, audio, language=None):
        model = self._ensure_model()
        with self._infer_lock:
            return model.transcribe(audio=audio, language=language)

    def detect_language(self, audio):
        results = self.transcribe(audio=audio, language=None)
        if not results:
            return "unknown", "", ""

        first = results[0]
        raw_language = getattr(first, "language", "") or ""
        raw_text = getattr(first, "text", "") or ""
        language_code = normalize_qwen_language(raw_language, raw_text)
        return language_code, raw_language, raw_text

