import hashlib
import json
import os
import re
import threading
import unicodedata

import requests

try:
    import ahocorasick
except ImportError:
    ahocorasick = None

PUNCT_FALLBACK_MODEL_PATH = os.environ.get(
    "PUNCT_FALLBACK_MODEL_PATH",
    "./pretrained_models/iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
)
PUNCT_FALLBACK_MODEL_REVISION = os.environ.get("PUNCT_FALLBACK_MODEL_REVISION", "v2.0.4")
PUNCT_FALLBACK_RESULT_LENGTH = int(os.environ.get("PUNCT_FALLBACK_RESULT_LENGTH", "100"))
_PUNCTUATION_RE = re.compile(r"[，。！？；：、,.?!;:]")
_punctuation_pipeline = None
_punctuation_pipeline_lock = threading.Lock()


class ServiceURLRouter:
    def __init__(self, urls):
        normalized = [str(url).strip() for url in (urls or []) if str(url).strip()]
        if not normalized:
            raise ValueError("ServiceURLRouter requires at least one URL")
        self.urls = normalized
        self._next_idx = 0
        self._lock = threading.Lock()

    def select(self, routing_key=None):
        if routing_key:
            digest = hashlib.md5(str(routing_key).encode("utf-8")).hexdigest()
            idx = int(digest[:8], 16) % len(self.urls)
            return {
                "url": self.urls[idx],
                "idx": idx,
                "mode": "hash",
                "seq": None,
                "total": len(self.urls),
            }

        with self._lock:
            idx = self._next_idx % len(self.urls)
            seq = self._next_idx + 1
            url = self.urls[idx]
            self._next_idx += 1

        return {
            "url": url,
            "idx": idx,
            "mode": "round_robin",
            "seq": seq,
            "total": len(self.urls),
        }


def parse_service_urls(list_env_name, single_env_name=None, default_url=""):
    raw_list = os.environ.get(list_env_name, "").strip()
    if raw_list:
        urls = [url.strip() for url in raw_list.split(",") if url.strip()]
        if urls:
            return urls

    if single_env_name:
        single_url = os.environ.get(single_env_name, "").strip()
        if single_url:
            return [single_url]

    default_url = (default_url or "").strip()
    return [default_url] if default_url else []


def normalize_llm_chat_url(url):
    normalized = (url or "").strip().rstrip("/")
    if not normalized:
        return ""
    if normalized.endswith("/chat/completions"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


def parse_worker_service_urls(default_url=""):
    for list_env_name, single_env_name in (
        ("WORKER_URLS", "WORKER_URL"),
    ):
        raw_list = os.environ.get(list_env_name, "").strip()
        if raw_list:
            urls = [url.strip().rstrip("/") for url in raw_list.split(",") if url.strip()]
            if urls:
                return urls

        if single_env_name:
            single_url = os.environ.get(single_env_name, "").strip().rstrip("/")
            if single_url:
                return [single_url]

    default_url = (default_url or "").strip().rstrip("/")
    if default_url:
        return [default_url]
    raise ValueError(
        "WORKER_URL must be set to the remote offline worker base URL, "
        "for example http://<host>:7001"
    )


def parse_punct_service_urls(default_url=""):
    return parse_service_urls("PUNCT_URLS", "PUNCT_URL", default_url)


def parse_llm_service_urls(default_url=""):
    for list_env_name, single_env_name in (
        ("LLM_URLS", "LLM_URL"),
        ("VLLM_URLS", "VLLM_URL"),
    ):
        raw_list = os.environ.get(list_env_name, "").strip()
        if raw_list:
            urls = [
                normalize_llm_chat_url(url)
                for url in raw_list.split(",")
                if url.strip()
            ]
            urls = [url for url in urls if url]
            if urls:
                return urls

        if single_env_name:
            single_url = os.environ.get(single_env_name, "").strip()
            if single_url:
                normalized = normalize_llm_chat_url(single_url)
                if normalized:
                    return [normalized]

    default_url = normalize_llm_chat_url(default_url)
    if default_url:
        return [default_url]
    raise ValueError(
        "LLM_URL must be set to the remote Qwen vLLM OpenAI base URL, "
        "for example http://<host>:8081/v1"
    )


def normalize_embed_url(url):
    normalized = (url or "").strip().rstrip("/")
    if not normalized:
        return ""
    if normalized.endswith("/embeddings"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/embeddings"
    return f"{normalized}/v1/embeddings"


def parse_embed_service_urls(default_url=""):
    for list_env_name, single_env_name in (
        ("EMBED_URLS", "EMBED_URL"),
    ):
        raw_list = os.environ.get(list_env_name, "").strip()
        if raw_list:
            urls = [
                normalize_embed_url(url)
                for url in raw_list.split(",")
                if url.strip()
            ]
            urls = [url for url in urls if url]
            if urls:
                return urls

        if single_env_name:
            single_url = os.environ.get(single_env_name, "").strip()
            if single_url:
                normalized = normalize_embed_url(single_url)
                if normalized:
                    return [normalized]

    default_url = normalize_embed_url(default_url)
    return [default_url] if default_url else []


def _log(logger, level, message):
    if logger is None:
        return
    log_fn = getattr(logger, level, None)
    if callable(log_fn):
        log_fn(message)


def remove_isolated_spaces(text):
    if not text:
        return text
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[，。！？；：、“”‘’（）《》【】])", "", text)
    text = re.sub(r"(?<=[，。！？；：、“”‘’（）《》【】])\s+(?=[\u4e00-\u9fff])", "", text)
    return re.sub(r"\s+", " ", text).strip()


def needs_local_punctuation_fallback(text):
    normalized = (text or "").strip()
    return len(normalized) > PUNCT_FALLBACK_RESULT_LENGTH and not _PUNCTUATION_RE.search(normalized)


def init_punctuation_pipeline(logger=None):
    global _punctuation_pipeline

    if _punctuation_pipeline is not None:
        return True

    with _punctuation_pipeline_lock:
        if _punctuation_pipeline is not None:
            return True

        try:
            from modelscope.pipelines import pipeline
            from modelscope.utils.constant import Tasks

            _punctuation_pipeline = pipeline(
                task=Tasks.punctuation,
                model=PUNCT_FALLBACK_MODEL_PATH,
                model_revision=PUNCT_FALLBACK_MODEL_REVISION,
                disable_update=True,
            )
            _log(logger, "info", f"Punctuation fallback model initialized: {PUNCT_FALLBACK_MODEL_PATH}")
            return True
        except Exception as exc:
            _log(logger, "error", f"Failed to initialize punctuation fallback model: {exc}")
            return False


def apply_local_punctuation(text, logger=None, session_id="UNKNOWN"):
    normalized = remove_isolated_spaces((text or "").strip())
    if not normalized:
        return ""

    if _punctuation_pipeline is None and not init_punctuation_pipeline(logger):
        return normalized

    try:
        rec_result = _punctuation_pipeline(input=normalized)
        if isinstance(rec_result, list) and rec_result:
            output_text = (rec_result[0] or {}).get("text", "").strip()
            return output_text or normalized
        if isinstance(rec_result, dict):
            output_text = rec_result.get("text", "").strip()
            return output_text or normalized
        return normalized
    except Exception as exc:
        _log(logger, "error", f"[{session_id}] Local punctuation fallback failed: {exc}")
        return normalized


def _extract_chat_content(payload):
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        return ""

    message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    content = message.get("content", "") if isinstance(message, dict) else ""
    return content.strip() if isinstance(content, str) else ""


def send_request_with_details(content, punct_url, session_id="UNKNOWN", logger=None, timeout=10):
    normalized = (content or "").strip()
    if not normalized:
        return {
            "text": "",
            "remote_text": "",
            "used_fallback": False,
            "request_failed": False,
        }

    headers = {"Content-Type": "application/json", "X-Session-Id": session_id}
    data = {
        "model": "llm",
        "messages": [{"role": "user", "content": normalized}],
        "max_tokens": 4096,
        "temperature": 0.1,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    result_text = normalized
    request_failed = False
    try:
        response = requests.post(punct_url, headers=headers, json=data, timeout=timeout)
        response.raise_for_status()
        parsed_text = _extract_chat_content(response.json())
        if parsed_text:
            result_text = parsed_text
    except Exception as exc:
        request_failed = True
        _log(logger, "error", f"[{session_id}] Remote punctuation request failed: {exc}")

    if needs_local_punctuation_fallback(result_text):
        _log(logger, "warning", f"[{session_id}] Remote punctuation result has no punctuation and exceeds {PUNCT_FALLBACK_RESULT_LENGTH} chars, switching to local fallback model")
        return {
            "text": apply_local_punctuation(result_text, logger=logger, session_id=session_id),
            "remote_text": result_text,
            "used_fallback": True,
            "request_failed": request_failed,
        }

    return {
        "text": result_text,
        "remote_text": result_text,
        "used_fallback": False,
        "request_failed": request_failed,
    }


def send_request(content, punct_url, session_id="UNKNOWN", logger=None, timeout=10):
    return send_request_with_details(content, punct_url, session_id=session_id, logger=logger, timeout=timeout)["text"]


CONTENT_SAFETY_MIN_MATCHES = max(int(os.environ.get("CONTENT_SAFETY_MIN_MATCHES", "2")), 1)
CONTENT_SAFETY_DICT_DIR = os.environ.get("CONTENT_SAFETY_DICT_DIR", os.path.join(os.getcwd(), "dict"))
POLITICAL_DICT_PATH = os.environ.get("POLITICAL_DICT_PATH", os.path.join(CONTENT_SAFETY_DICT_DIR, "political.dict"))
PROFANITY_DICT_PATH = os.environ.get("PROFANITY_DICT_PATH", os.path.join(CONTENT_SAFETY_DICT_DIR, "profanity.dict"))
_CONTENT_SAFETY_RULES = {
    "political": {"label": "反动内容", "path": POLITICAL_DICT_PATH},
    "profanity": {"label": "色情内容", "path": PROFANITY_DICT_PATH},
}
_content_safety_lock = threading.Lock()
_content_safety_matchers = {}


def _normalize_keyword_text(text):
    normalized = unicodedata.normalize("NFKC", str(text or "")).lower()
    return re.sub(r"\s+", " ", normalized, flags=re.UNICODE).strip()


def _load_keyword_list(dict_path):
    keywords = []
    try:
        with open(dict_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                keyword = _normalize_keyword_text(raw_line.strip())
                if keyword:
                    keywords.append(keyword)
    except FileNotFoundError:
        return []
    return sorted(set(keywords), key=lambda item: (-len(item), item))


def _is_ascii_keyword(keyword):
    return bool(re.fullmatch(r"[a-z0-9]+(?:[\s_-][a-z0-9]+)*", str(keyword or "")))


def _extract_english_word_set(normalized_text):
    if not normalized_text:
        return set()
    ascii_only_text = re.sub(r"[^a-z0-9\s_-]+", " ", normalized_text.lower())
    return {word for word in ascii_only_text.split() if word}


def _build_keyword_matcher(dict_path):
    keywords = _load_keyword_list(dict_path)
    ascii_keywords = [keyword for keyword in keywords if _is_ascii_keyword(keyword)]
    non_ascii_keywords = [keyword for keyword in keywords if not _is_ascii_keyword(keyword)]

    automaton = None
    if ahocorasick is not None:
        automaton = ahocorasick.Automaton()
        for keyword in non_ascii_keywords:
            automaton.add_word(keyword, keyword)
        automaton.make_automaton()
    return {
        "keywords": non_ascii_keywords,
        "automaton": automaton,
        "ascii_keywords": ascii_keywords,
    }


def _get_keyword_matcher(category, dict_path):
    abs_path = os.path.abspath(dict_path)
    mtime = os.path.getmtime(abs_path) if os.path.exists(abs_path) else None
    cache_key = (category, abs_path, mtime)

    with _content_safety_lock:
        cached = _content_safety_matchers.get(cache_key)
        if cached is not None:
            return cached

        matcher = _build_keyword_matcher(abs_path)
        _content_safety_matchers[cache_key] = matcher
        return matcher


def _collect_non_overlapping_matches(normalized_text, matcher):
    if not normalized_text:
        return []

    matched_items = []
    seen = set()

    english_word_set = _extract_english_word_set(normalized_text)
    for keyword in matcher.get("ascii_keywords", []):
        if keyword in english_word_set and keyword not in seen:
            matched_items.append({"keyword": keyword, "start": -1, "end": -1, "mode": "ascii_word"})
            seen.add(keyword)

    candidates = []
    automaton = matcher.get("automaton")
    if automaton is not None:
        for end_idx, keyword in automaton.iter(normalized_text):
            start_idx = end_idx - len(keyword) + 1
            candidates.append((start_idx, end_idx + 1, keyword))
    else:
        for keyword in matcher.get("keywords", []):
            start_idx = normalized_text.find(keyword)
            while start_idx != -1:
                candidates.append((start_idx, start_idx + len(keyword), keyword))
                start_idx = normalized_text.find(keyword, start_idx + 1)

    if not candidates:
        return matched_items

    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[1], item[2]))
    current_end = -1
    for start_idx, end_idx, keyword in candidates:
        if start_idx < current_end or keyword in seen:
            continue
        matched_items.append({"keyword": keyword, "start": start_idx, "end": end_idx, "mode": "automaton"})
        seen.add(keyword)
        current_end = end_idx

    return matched_items


def _parse_llm_json(text):
    raw_text = str(text or "").strip()
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
            continue
    return None


def _run_llm_content_safety_review(text, review_url, logger=None, source="content_safety", timeout=(3, 10)):
    result = {
        "performed": False,
        "is_safe": True,
        "raw": "",
        "error": "",
        "url": review_url,
    }
    if not review_url:
        return result

    result["performed"] = True
    payload = {
        "model": "llm",
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是一个理智且客观的内容审核助手。"
                    "请判断给定文本是否包含明显的反动或色情违规内容。"
                    "严格返回 JSON：{\"is_safe\": true/false}，不要输出任何解释性文字。"
                ),
            },
            {"role": "user", "content": str(text or "")},
        ],
        "max_tokens": 64,
        "temperature": 0.01,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False},
    }

    try:
        response = requests.post(
            review_url,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        response_payload = response.json()
        content = _extract_chat_content(response_payload)
        result["raw"] = content
        parsed = _parse_llm_json(content)
        if isinstance(parsed, dict):
            result["is_safe"] = bool(parsed.get("is_safe", True))
        else:
            result["error"] = "invalid_json_response"
            _log(logger, "warning", f"[{source}] LLM content-safety review returned non-JSON content, defaulting to allow")
    except Exception as exc:
        result["error"] = str(exc)
        _log(logger, "warning", f"[{source}] LLM content-safety review failed, defaulting to allow: {exc}")

    return result


def run_llm_content_safety_review(text, review_url, logger=None, source="content_safety", timeout=(3, 10)):
    return _run_llm_content_safety_review(
        text,
        review_url,
        logger=logger,
        source=source,
        timeout=timeout,
    )


def check_content_safety(text, logger=None, source="content_safety", review_url=None, review_url_getter=None, review_timeout=(3, 10)):
    normalized_text = _normalize_keyword_text(text)
    details = {
        "is_safe": True,
        "normalized_text": normalized_text,
        "matched": {},
        "violations": [],
        "summary": "",
        "keyword_triggered": False,
        "review_performed": False,
        "review_is_safe": True,
        "review_raw": "",
        "review_error": "",
        "final_reason": "no_keyword_hit",
    }

    if not normalized_text:
        return details

    if ahocorasick is None:
        _log(logger, "warning", f"[{source}] pyahocorasick is not installed, falling back to plain substring matching")

    for category, rule in _CONTENT_SAFETY_RULES.items():
        matcher = _get_keyword_matcher(category, rule["path"])
        matched_items = _collect_non_overlapping_matches(normalized_text, matcher)
        matched_keywords = [item["keyword"] for item in matched_items]
        details["matched"][category] = matched_keywords

        if len(matched_keywords) >= CONTENT_SAFETY_MIN_MATCHES:
            details["violations"].append({
                "category": category,
                "label": rule["label"],
                "count": len(matched_keywords),
                "matches": matched_keywords,
            })

    if not details["violations"]:
        return details

    details["keyword_triggered"] = True
    detail_parts = []
    for item in details["violations"]:
        preview = ", ".join(item["matches"][:8])
        detail_parts.append(f"{item['label']}命中 {item['count']} 个词: {preview}")
    details["summary"] = "；".join(detail_parts)

    if review_url is None and callable(review_url_getter):
        try:
            review_url = review_url_getter()
        except Exception as exc:
            details["review_error"] = str(exc)
            _log(logger, "warning", f"[{source}] Failed to resolve LLM review URL, defaulting to allow: {exc}")

    review_result = _run_llm_content_safety_review(
        text,
        review_url,
        logger=logger,
        source=source,
        timeout=review_timeout,
    )
    details["review_performed"] = review_result["performed"]
    details["review_is_safe"] = review_result["is_safe"]
    details["review_raw"] = review_result["raw"]
    if review_result["error"] and not details["review_error"]:
        details["review_error"] = review_result["error"]

    if review_result["performed"]:
        details["is_safe"] = review_result["is_safe"]
        details["final_reason"] = (
            "keyword_hit_and_llm_blocked" if not review_result["is_safe"] else "keyword_hit_but_llm_passed"
        )
        log_level = "warning" if not review_result["is_safe"] else "info"
        llm_decision = "block" if not review_result["is_safe"] else "allow"
        _log(logger, log_level, f"[{source}] Keyword hits triggered LLM review ({llm_decision}): {details['summary']}")
    else:
        details["is_safe"] = True
        details["final_reason"] = "keyword_hit_without_llm_review"
        _log(logger, "warning", f"[{source}] Keyword hits found but LLM review was unavailable, defaulting to allow: {details['summary']}")

    return details