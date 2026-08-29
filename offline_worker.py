import uvicorn
from fastapi import FastAPI, BackgroundTasks, UploadFile, File, Query
from pydantic import BaseModel
import asyncio
import ast
import json
import requests
import os
import shutil
import soundfile as sf
import time
import torch
import numpy as np
import tempfile
import re
import sys
from scipy.io.wavfile import write as write_wav
import onnxruntime
import concurrent.futures
import subprocess
import uuid
import threading
from collections import deque
from modelscope.pipelines import pipeline
from modelscope.utils.constant import Tasks
import aiohttp
from funasr import AutoModel
import utils
from functools import lru_cache
from qwen_asr_adapter import (
    QwenOfflineASR,
    infer_language_from_text,
    normalize_qwen_language,
    normalize_ui_language,
    qwen_language_name_from_code,
    qwen_language_display_name_zh,
    should_translate_for_ui_language,
    should_translate_to_chinese,
    should_use_firered_asr,
    ui_language_name_from_code,
)
from subtitles.artifact_sync import sync_session_artifacts_to_gateway
from subtitles.i18n import normalize_asr_language
from subtitles.wrap import format_elapsed_time

os.environ["MODELSCOPE_DISABLE_UPDATE"] = "1"
os.environ["FUNASR_DISABLE_UPDATE"] = "1"

sys.path.append("./src")
try:
    from logger import Logger
    os.makedirs('./logs', exist_ok=True)
    worker_log_file = os.environ.get("WORKER_LOG_FILE", "./logs/worker.log")
    server_log = Logger(worker_log_file, level='info', backCount=30)
except ImportError:
    sys.exit(1)

WORKER_INSTANCE_ID = os.environ.get("WORKER_INSTANCE_ID", "offline-worker")
OFFLINE_WORKER_PORT = os.environ.get("OFFLINE_WORKER_PORT", "unknown")
WORKER_INSTANCE_TAG = f"{WORKER_INSTANCE_ID}@{OFFLINE_WORKER_PORT}"

# 1. 创建一个全局的异步队列
task_queue = asyncio.Queue()
active_task_count = 0
# 按 FIFO 记录仍在排队、尚未被 Worker 取走的 session（用于更新排队位次）
waiting_session_order: list[tuple[str, str]] = []
TASK_EVENT_BUFFER_SIZE = max(int(os.environ.get("WORKER_TASK_EVENT_BUFFER_SIZE", "256")), 32)
task_events: dict[str, deque] = {}
task_event_seq: dict[str, int] = {}
task_event_lock = threading.Lock()
sys.path.insert(0, os.path.join(os.getcwd(), "fireredasr2s"))
from fireredasr2s.fireredasr2 import FireRedAsr2, FireRedAsr2Config

app = FastAPI()

# ==========================================
# 静态配置与模型加载
# ==========================================
SAVE_ROOT = "saved_data"
UPLOAD_DIR = os.path.join(SAVE_ROOT, "uploads")
SEGMENT_DIR = os.path.join(SAVE_ROOT, "segments") 
CACHE_DIR = os.path.join(SAVE_ROOT, "cache")
INTERNAL_WORKER_TOKEN = os.environ.get("INTERNAL_WORKER_TOKEN", "").strip()
PUNCT_URLS = utils.parse_punct_service_urls(
    os.environ.get("PUNCT_URL", "").strip()
    or f"http://127.0.0.1:{os.environ.get('PUNCT_PORT', '8080')}/v1/chat/completions",
)
_punct_router = utils.ServiceURLRouter(PUNCT_URLS)

# 动态批处理：限制每个批次的最大音频总秒数 (60s-90s 通常是 24G 显存的安全范围)
MAX_BATCH_DURATION_S = 80.0
# 动态批处理：限制每个批次的最大片段数量（防止文件句柄过多）
MAX_BATCH_COUNT = 10
# VAD 安全阈值：音频保持较长上下文，视频更激进切分，防止长段口播导致识别漂移
VAD_SAFE_DURATION_S = 59.0
VIDEO_VAD_SAFE_DURATION_S = 20.0
# 歌曲/音乐：单段上限放宽到 60s，让完整乐句整体送入 ASR，避免碎切丢词
MUSIC_VAD_SAFE_DURATION_S = 60.0
MUSIC_MERGE_GAP_S = 5.0
# 合并策略阈值：如果两个纯音频片段间隔小于此值，且合并后不超长，则合并
MAX_MERGE_GAP_S = 2.0
# 短音频/视频直通策略：仅跳过 VAD 切分，后续语种检测、说话人处理与识别流程保持不变
DIRECT_ASR_SKIP_VAD_DURATION_S = 60.0
# 聚类后主说话人占比达到该阈值时，强制按单说话人处理
SINGLE_SPEAKER_DOMINANCE_RATIO = 0.95

VAD_MODEL_PATH = "pretrained_models/FireRedASR-AED-L/silero_vad.onnx"
LOCAL_SD_MODEL_PATH = "pretrained_models/damo/speech_campplus_speaker-diarization_common"
abs_sd_path = os.path.abspath(LOCAL_SD_MODEL_PATH)
HALLUCINATION_LIST = ["一九八二年", "以色列正在研究埃及关于恢复叙以和谈的建议", "自古以来为了领土而以血偿血的纠纷", "嗯嗯嗯嗯嗯嗯"]

server_log.logger.info("Loading Models...")
sess_options = onnxruntime.SessionOptions()
sess_options.intra_op_num_threads, sess_options.inter_op_num_threads = 1, 1
vad_session = onnxruntime.InferenceSession(VAD_MODEL_PATH, sess_options) if os.path.exists(VAD_MODEL_PATH) else None

sd_pipeline = pipeline(task=Tasks.speaker_diarization, model=abs_sd_path, model_revision=None, device='cuda')
sv_pipeline = pipeline(task=Tasks.speaker_verification, model='./pretrained_models/damo/speech_campplus_sv_zh-cn_16k-common', model_revision='v1.0.0', device='cuda')
file_asr_config = FireRedAsr2Config(
    use_gpu=True,
    use_half=False,
    beam_size=3,
    nbest=1,
    decode_max_len=0,
    softmax_smoothing=1.25,
    aed_length_penalty=0.6,
    eos_penalty=1.0,
    return_timestamp=True
)
file_asr_model = FireRedAsr2.from_pretrained("aed", "fireredasr2s/pretrained_models/FireRedASR2-AED", file_asr_config)
qwen_file_asr_model = QwenOfflineASR(server_log.logger)
qwen_file_asr_model.preload()

sense_voice_model = AutoModel(model="./pretrained_models/SenseVoiceSmall", device="cuda" if torch.cuda.is_available() else "cpu", disable_update=True)
server_log.logger.info("SenseVoice model loaded for language detection.")

from transformers import Wav2Vec2ForSequenceClassification, Wav2Vec2FeatureExtractor
gender_model_path = "./pretrained_models/alefiury/wav2vec2-large-xlsr-53-gender-recognition-librispeech"
gender_model = Wav2Vec2ForSequenceClassification.from_pretrained(gender_model_path).to("cuda")
gender_processor = Wav2Vec2FeatureExtractor.from_pretrained(gender_model_path)

def _format_queue_progress_message(position: int, ui_language: str) -> str:
    ui_language = normalize_ui_language(ui_language)
    if position <= 0:
        return "开始处理..." if ui_language == "zh-CN" else "Processing started..."
    if ui_language == "en":
        return f"Queue position: {position}"
    return f"排队第 {position} 位，等待处理..."


def _remove_waiting_session(session_id: str) -> None:
    global waiting_session_order
    if waiting_session_order and waiting_session_order[0][0] == session_id:
        waiting_session_order.pop(0)
        return
    waiting_session_order = [(sid, lang) for sid, lang in waiting_session_order if sid != session_id]


def _notify_waiting_queue_positions() -> None:
    for idx, (session_id, ui_language) in enumerate(waiting_session_order):
        position = idx + 1
        _record_task_event(
            session_id,
            "progress",
            {
                "message": _format_queue_progress_message(position, ui_language),
                "queue_position": position,
                "phase": "queued",
            },
        )


# 2. 创建一个真正的后台消费者，它永远在后台跑，每次只拿出一个任务处理
async def offline_task_worker(worker_id: int):
    global active_task_count
    while True:
        # 阻塞等待新任务
        req = await task_queue.get()
        ui_language = normalize_ui_language(getattr(req, "ui_language", None) or "zh-CN")
        _remove_waiting_session(req.session_id)
        _record_task_event(
            req.session_id,
            "progress",
            {
                "message": _format_queue_progress_message(0, ui_language),
                "queue_position": 0,
                "phase": "processing",
            },
        )
        _notify_waiting_queue_positions()
        active_task_count += 1
        try:
            task_start_time = time.perf_counter()
            task_start_real = time.time()
            waiting_now = task_queue.qsize()
            pending_now = active_task_count + waiting_now
            server_log.logger.info(
                f"[{WORKER_INSTANCE_TAG}][Worker-{worker_id}] 开始处理任务: {req.session_id} ({req.original_filename}) | "
                f"active={active_task_count} waiting={waiting_now} pending={pending_now}"
            )
            resolved_filepath, fetch_temp_files = await asyncio.to_thread(resolve_process_filepath, req)
            try:
                await asyncio.to_thread(
                    background_transcribe_process,
                    req.session_id,
                    resolved_filepath,
                    req.original_filename,
                    req.fast_mode,
                    req.request_start_time,
                    req.ui_language,
                    req.callback_url or "",
                    asr_language=req.asr_language,
                )
            finally:
                for temp_path in fetch_temp_files:
                    if os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                        except OSError:
                            pass
            proc_elapsed_s = time.perf_counter() - task_start_time
            total_elapsed_s = proc_elapsed_s
            if req.request_start_time:
                try:
                    total_elapsed_s = time.time() - float(req.request_start_time)
                except (TypeError, ValueError):
                    total_elapsed_s = proc_elapsed_s
            proc_elapsed_text = format_elapsed_time(proc_elapsed_s)
            total_elapsed_text = format_elapsed_time(total_elapsed_s)
            server_log.logger.info(
                f"[{WORKER_INSTANCE_TAG}][Worker-{worker_id}] 任务完成: {req.session_id} ({req.original_filename}) | "
                f"处理耗时={proc_elapsed_text} 总耗时={total_elapsed_text}"
            )
        except Exception as e:
            server_log.logger.error(
                f"[{WORKER_INSTANCE_TAG}][Worker-{worker_id}] Task Failed: "
                f"{req.session_id} ({req.original_filename}) | {e}"
            )
            _record_task_event(req.session_id, "error", {"message": str(e)})
        finally:
            active_task_count = max(active_task_count - 1, 0)
            task_queue.task_done()
            _notify_waiting_queue_positions()

# 3. 在 FastAPI 启动时把消费者跑起来
OFFLINE_WORKER_CONCURRENCY = int(os.environ.get("OFFLINE_WORKER_CONCURRENCY", "2"))

@app.on_event("startup")
async def startup_event():
    utils.init_punctuation_pipeline(server_log.logger)
    # 启动多个并发消费者，支持同时处理多个离线任务（例如多个文件上传）
    for i in range(OFFLINE_WORKER_CONCURRENCY):
        asyncio.create_task(offline_task_worker(i+1))
    server_log.logger.info(
        f"[{WORKER_INSTANCE_TAG}] Offline worker pool started with {OFFLINE_WORKER_CONCURRENCY} concurrent workers (pid={os.getpid()})"
    )

# ==========================================
# 辅助函数库 (全量迁移自原代码)
# ==========================================
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


def get_punct_url(session_id: str = "", route_source: str = "unknown"):
    selection = _punct_router.select(routing_key=session_id or None)
    if len(PUNCT_URLS) > 1:
        session_prefix = f"[{session_id}] " if session_id else ""
        seq_text = f" seq={selection['seq']}" if selection["seq"] is not None else ""
        server_log.logger.info(
            f"{session_prefix}[Punct-LB] source={route_source} mode={selection['mode']} "
            f"idx={selection['idx']} selected={selection['url']}{seq_text}"
        )
    return selection["url"]


def fix_english_spacing(text):
    return re.sub(r'([a-zA-Z])([.?!,;])([a-zA-Z])', r'\1\2 \3', text) if text else text


def format_timestamp(s):
    return f"{int(divmod(s, 60)[0]):02d}:{divmod(s, 60)[1]:05.2f}"


GATEWAY_CALLBACK_URL = os.environ.get("GATEWAY_CALLBACK_URL", "").strip()


def resolve_gateway_base_url(callback_url: str = "") -> str:
    if callback_url:
        return callback_url.rstrip("/")
    if GATEWAY_CALLBACK_URL:
        return GATEWAY_CALLBACK_URL.rstrip("/")
    return ""



def detect_gender(audio_np):
    inputs = gender_processor(audio_np, sampling_rate=16000, return_tensors="pt", padding=True).to("cuda")
    with torch.no_grad():
        return {"0": "female", "1": "male"}[str(torch.argmax(gender_model(**inputs).logits, dim=-1).item())]

def asr_engine_label_from_lang(language_code):
    if should_use_firered_asr(language_code):
        return "FireRedASR2S"
    return "Qwen3 ASR"


def process_segment_with_punctuation(text, index, session_id, asr_engine="unknown"):
    if not text or not text.strip(): return ""
    server_log.logger.info(f"[{session_id}] Origin (片段 {index+1}) [{asr_engine}]: '{text}'")
    try:
        punctuated_text = utils.send_request(
            text,
            get_punct_url(session_id=session_id, route_source=f"segment_punctuate.{index+1}"),
            session_id,
            server_log.logger,
        )
        if punctuated_text:
            server_log.logger.info(f"[{session_id}] 加标点 (片段 {index+1}): '{punctuated_text}'")
            return punctuated_text
        else:
            return text
    except Exception as e:
        server_log.logger.error(f"[{session_id}] 标点出错: {e}")
        return text


class SileroVADProcessor:
    def __init__(self, session, threshold=0.5, min_speech_duration_ms=250, min_silence_duration_ms=100):
        self.session = session
        self.threshold = threshold
        # 迟滞释放阈值：必须严格 > 0，否则 `p < release` 恒为 False（VAD 概率恒 >= 0），
        # trig 一旦置位就永不释放，循环内切不出任何片段，只能靠末尾兜底吐出一整段。
        # 典型触发场景：视频策略 threshold=0.15 时，固定减 0.15 会得到 0.00。
        self.release_threshold = max(threshold * 0.5, 0.01)
        self.min_speech_samples, self.min_silence_samples = min_speech_duration_ms * 16, min_silence_duration_ms * 16
        self.window_size_samples = 512

    def get_speech_timestamps(self, audio_float):
        if not self.session:
            return [{'start': 0, 'end': len(audio_float)}]
        pad = self.window_size_samples - (len(audio_float) % self.window_size_samples)
        audio = np.pad(audio_float, (0, pad), 'constant') if pad != self.window_size_samples else audio_float
        state = np.zeros((2, 1, 128), dtype=np.float32)
        probs = []
        for i in range(0, len(audio), self.window_size_samples):
            chunk = audio[i:i + self.window_size_samples]
            out, state = self.session.run(
                None,
                {
                    self.session.get_inputs()[0].name: chunk[None, :],
                    self.session.get_inputs()[1].name: state,
                    self.session.get_inputs()[2].name: np.array([16000], dtype=np.int64),
                },
            )
            probs.append(out[0][0])

        speeches, trig, s_idx = [], False, 0
        for i, p in enumerate(probs):
            curr = i * self.window_size_samples
            if p >= self.threshold and not trig:
                trig, s_idx = True, curr
            if p < self.release_threshold and trig:
                if (curr - s_idx) > self.min_speech_samples:
                    speeches.append({'start': s_idx, 'end': curr})
                trig = False
        if trig:
            speeches.append({'start': s_idx, 'end': len(audio)})

        merged = []
        if speeches:
            c = speeches[0]
            for n in speeches[1:]:
                if n['start'] - c['end'] < self.min_silence_samples:
                    c['end'] = n['end']
                else:
                    merged.append(c)
                    c = n
            merged.append(c)
        return [s for s in merged if (s['end'] - s['start']) >= self.min_speech_samples]


def preprocess_audio(path):
    def _normalize_audio(audio_np):
        if audio_np is None:
            return np.zeros(16000, dtype=np.float32)
        audio_np = np.asarray(audio_np, dtype=np.float32)
        if audio_np.ndim > 1:
            audio_np = audio_np[:, 0]
        if audio_np.size == 0:
            return np.zeros(16000, dtype=np.float32)
        peak = np.max(np.abs(audio_np))
        if peak > 0:
            audio_np = audio_np / peak * 0.9
        return audio_np.astype(np.float32)

    def _is_native_16k_mono_wav(file_path):
        try:
            info = sf.info(file_path)
            return info.format == "WAV" and info.channels == 1 and info.samplerate == 16000
        except Exception:
            return False

    clean_wav = os.path.join(
        "/dev/shm" if os.path.exists("/dev/shm") else os.path.dirname(path),
        f"{uuid.uuid4()}.wav",
    )
    try:
        if _is_native_16k_mono_wav(path):
            audio, _ = sf.read(path, dtype="float32")
            return _normalize_audio(audio), path

        subprocess.run(
            ["ffmpeg", "-i", path, "-vn", "-ar", "16000", "-ac", "1", "-y", "-loglevel", "error", clean_wav],
            check=True,
        )
        audio, _ = sf.read(clean_wav, dtype="float32")
        return _normalize_audio(audio), clean_wav
    except Exception:
        return np.zeros(16000, dtype=np.float32), None


def get_dynamic_vad_strategies(is_video=False, is_music=False):
    if is_music:
        # 歌曲/音乐：粗粒度切分。演唱的换气、长音、间奏会让瞬时语音概率大幅波动，
        # 用小 min_silence 会把一句唱词切成碎片，导致 ASR 丢词。
        # 这里只在“足够长的静音”处切开，让每段尽量维持完整乐句，交给 Qwen3 整体识别。
        return [
            {"threshold": 0.20, "min_speech": 300, "min_silence": 5000},
            {"threshold": 0.20, "min_speech": 300, "min_silence": 3500},
            {"threshold": 0.20, "min_speech": 300, "min_silence": 2500},
        ]
    if is_video:
        return [
            {"threshold": 0.15, "min_speech": 100, "min_silence": 2000},
            {"threshold": 0.15, "min_speech": 100, "min_silence": 1500},
            {"threshold": 0.15, "min_speech": 100, "min_silence": 1000},
            {"threshold": 0.15, "min_speech": 100, "min_silence": 800},
        ]
    return [
        {"threshold": 0.20, "min_speech": 100, "min_silence": 3000},
        {"threshold": 0.20, "min_speech": 100, "min_silence": 2000},
        {"threshold": 0.20, "min_speech": 100, "min_silence": 1500},
        {"threshold": 0.20, "min_speech": 100, "min_silence": 1200},
        {"threshold": 0.20, "min_speech": 100, "min_silence": 800},
        {"threshold": 0.20, "min_speech": 100, "min_silence": 500},
    ]


SENSEVOICE_LANG_TAGS = ["zh", "en", "yue", "ja", "ko", "nospeech"]
SENSEVOICE_HIGH_TRUST_LANGS = frozenset({"ja", "zh", "en", "ko"})
CHINESE_FAMILY_LANGS = frozenset({"zh", "yue"})
LANG_DETECT_MIN_SAMPLES = 0.3
LANG_DETECT_MAX_SAMPLES = 5
LANG_DETECT_CLIP_SAMPLES = 16000 * 15


def _clip_lang_detect_audio(detect_audio):
    if len(detect_audio) > LANG_DETECT_CLIP_SAMPLES:
        return detect_audio[:LANG_DETECT_CLIP_SAMPLES]
    return detect_audio


def _sensevoice_detect_lang_tag(detect_audio):
    try:
        res = sense_voice_model.generate(
            input=detect_audio.astype(np.float32),
            cache={},
            language="auto",
            use_itn=False,
            batch_size_s=60,
        )
        raw_text = res[0]['text']
        for tag in SENSEVOICE_LANG_TAGS:
            if f"<|{tag}|>" in raw_text:
                return tag, raw_text
        return "unknown", raw_text
    except Exception as e:
        server_log.logger.warning(f"SenseVoice 语种检测失败: {e}")
        return "unknown", ""


def _pick_majority_lang(votes):
    if not votes:
        return "unknown"

    meaningful = [v for v in votes if v not in ("unknown", "nospeech", "")]
    if meaningful:
        counts = {}
        for tag in meaningful:
            counts[tag] = counts.get(tag, 0) + 1
        return max(counts, key=counts.get)

    if all(v == "nospeech" for v in votes):
        return "nospeech"
    return votes[0]


def _qwen_detect_lang_from_audio(detect_audio):
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            write_wav(f.name, 16000, (detect_audio * 32767).astype(np.int16))
            tmp_path = f.name
        qwen_lang, qwen_raw_lang, qwen_text = qwen_file_asr_model.detect_language(tmp_path)
        server_log.logger.info(
            f"Qwen3 语种二次判定: code={qwen_lang}, raw={qwen_raw_lang}, "
            f"text_preview={qwen_text[:80] if qwen_text else ''}"
        )
        if qwen_lang and qwen_lang != "unknown":
            return qwen_lang
    except Exception as e:
        server_log.logger.warning(f"Qwen3 语种二次判定失败: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
    return "unknown"


def _resolve_detected_language(sensevoice_lang, qwen_lang):
    """Merge SenseVoice + Qwen signals; Qwen covers Thai/Vietnamese etc. that SenseVoice lacks."""
    sv = (sensevoice_lang or "unknown").strip()
    qw = (qwen_lang or "unknown").strip()

    # Qwen LID is itself a free-form transcription. On singing it often "translates"
    # lyrics into English/Chinese/Korean and then mis-tags the language. SenseVoice
    # is the dedicated LID model for ja/zh/en/ko — trust it for those tags.
    if qw not in ("unknown", "nospeech") and qw not in SENSEVOICE_LANG_TAGS:
        return qw

    # SenseVoice often confuses Thai (and similar) with Cantonese.
    if sv == "yue" and qw not in ("unknown", "nospeech") and qw not in CHINESE_FAMILY_LANGS:
        return qw

    # SenseVoice may tag non-Chinese speech as Mandarin/Cantonese.
    if (
        sv in CHINESE_FAMILY_LANGS
        and qw not in ("unknown", "nospeech")
        and qw not in CHINESE_FAMILY_LANGS.union({"en", "ja", "ko"})
    ):
        return qw

    if sv in SENSEVOICE_HIGH_TRUST_LANGS:
        return sv

    if sv == "yue":
        return "yue"

    if qw not in ("unknown", "nospeech"):
        return qw

    return sv


_JA_KANA_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")
_KO_HANGUL_RE = re.compile(r"[\uac00-\ud7af]")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_THAI_RE = re.compile(r"[\u0e00-\u0e7f]")


def _script_counts(text):
    t = text or ""
    return {
        "ja": len(_JA_KANA_RE.findall(t)),
        "ko": len(_KO_HANGUL_RE.findall(t)),
        "zh": len(_HAN_RE.findall(t)),
        "en": len(_LATIN_RE.findall(t)),
        "th": len(_THAI_RE.findall(t)),
    }


def _text_script_conflicts(text, lang):
    """True when a segment is dominated by a script that doesn't match the locked language."""
    counts = _script_counts(text)
    if lang == "ja":
        native = counts["ja"] + counts["zh"]
        foreign = counts["en"] + counts["ko"] + counts["th"]
        return foreign >= 12 and foreign > native
    if lang == "ko":
        native = counts["ko"]
        foreign = counts["en"] + counts["ja"] + counts["th"]
        return foreign >= 12 and foreign > native
    if lang in ("zh", "yue"):
        native = counts["zh"]
        foreign = counts["en"] + counts["ja"] + counts["ko"] + counts["th"]
        return foreign >= 12 and foreign > native
    if lang == "en":
        native = counts["en"]
        foreign = counts["ja"] + counts["ko"] + counts["zh"] + counts["th"]
        return foreign >= 12 and foreign > native
    return False


def _stabilize_qwen_transcripts(paths, texts, session_lang, lang_forced=False):
    """Lock language from kana/hangul and re-decode segments that look translated."""
    locked = session_lang
    if not lang_forced:
        combined = " ".join((t or "").strip() for t in texts).strip()
        inferred = infer_language_from_text(combined)
        if inferred in ("ja", "ko") and locked in ("zh", "yue", "en", "unknown", "nospeech", ""):
            locked = inferred

    force_name = qwen_language_name_from_code(locked)
    if not force_name:
        return locked, texts

    if locked != session_lang:
        retry_idx = list(range(len(texts)))
    else:
        retry_idx = [i for i, t in enumerate(texts) if _text_script_conflicts(t, locked)]
    if not retry_idx:
        return locked, texts

    retry_paths = [paths[i] for i in retry_idx]
    retry_res = qwen_file_asr_model.transcribe(audio=retry_paths, language=force_name)
    new_texts = list(texts)
    if retry_res:
        for j, item in enumerate(retry_res):
            new_texts[retry_idx[j]] = getattr(item, "text", "") or new_texts[retry_idx[j]]
    return locked, new_texts


def refine_session_lang_from_text(session_lang, final_results_list):
    combined = " ".join(
        (item.get("text") or "").strip() for item in (final_results_list or [])
    ).strip()
    if not combined:
        return session_lang

    inferred = infer_language_from_text(combined)
    if inferred in ("unknown", "nospeech"):
        return session_lang

    # Correct audio mislabels (e.g. Thai detected as Cantonese) using transcript script.
    if session_lang == "yue" and inferred not in CHINESE_FAMILY_LANGS:
        return inferred

    # Japanese/Korean songs are often tagged zh/en because Qwen "translates" the chorus.
    if inferred in ("ja", "ko") and session_lang in ("zh", "yue", "en", "unknown", "nospeech"):
        return inferred

    if session_lang not in ("nospeech", "unknown"):
        return session_lang
    return inferred


def detect_language_from_vad_segments(audio_float, vad_segments):
    if not sense_voice_model:
        return "unknown"
    if not vad_segments:
        return "nospeech"

    sorted_segs = sorted(vad_segments, key=lambda ts: ts['end'] - ts['start'], reverse=True)
    sample_segs = sorted_segs[:min(LANG_DETECT_MAX_SAMPLES, len(sorted_segs))]
    min_samples = int(16000 * LANG_DETECT_MIN_SAMPLES)

    votes = []
    for idx, seg in enumerate(sample_segs, start=1):
        detect_audio = audio_float[seg['start']:seg['end']]
        if len(detect_audio) < min_samples:
            continue
        detect_audio = _clip_lang_detect_audio(detect_audio)
        sensevoice_lang, raw_text = _sensevoice_detect_lang_tag(detect_audio)
        stripped = re.sub(r"<\|[^|]+\|>", "", raw_text or "")
        script_lang = infer_language_from_text(stripped)
        if script_lang in ("ja", "ko") and sensevoice_lang != script_lang:
            server_log.logger.info(
                f"SenseVoice 脚本纠正: {sensevoice_lang} -> {script_lang} "
                f"(raw={raw_text[:80] if raw_text else ''})"
            )
            sensevoice_lang = script_lang
        server_log.logger.info(
            f"SenseVoice 语种检测样本 {idx}/{len(sample_segs)}: {sensevoice_lang} "
            f"(raw={raw_text[:80] if raw_text else ''})"
        )
        if sensevoice_lang != "unknown":
            votes.append(sensevoice_lang)

    sensevoice_lang = _pick_majority_lang(votes)

    longest_segment = sorted_segs[0]
    detect_audio = _clip_lang_detect_audio(
        audio_float[longest_segment['start']:longest_segment['end']]
    )
    qwen_lang = _qwen_detect_lang_from_audio(detect_audio)
    resolved_lang = _resolve_detected_language(sensevoice_lang, qwen_lang)
    server_log.logger.info(
        f"语种综合判定: SenseVoice={sensevoice_lang}, Qwen={qwen_lang}, "
        f"final={resolved_lang} (votes={votes})"
    )
    return resolved_lang


def detect_music_content(audio_float, vad_segments=None):
    """检测歌曲/音乐内容。

    vad_segments 为 None 时按固定窗口采样，使其可以在 VAD 之前调用——
    音乐判定需要反过来决定 VAD 的切分粒度，不能依赖 VAD 结果。
    """
    import re as _re
    reasons = []
    MUSIC_TAGS = {"Singing", "BGM", "Music"}
    if not sense_voice_model:
        return False, reasons

    if vad_segments:
        sorted_segs = sorted(vad_segments, key=lambda ts: ts['end'] - ts['start'], reverse=True)
        probe_ranges = [(s['start'], s['end']) for s in sorted_segs[:3]]
    else:
        # 无 VAD 先验：跳过头部可能的片头静音/器乐，在 10%/40%/70% 处各采样 15s
        total = len(audio_float)
        win = 16000 * 15
        probe_ranges = [
            (int(total * ratio), min(int(total * ratio) + win, total))
            for ratio in (0.1, 0.4, 0.7)
        ]

    for start, end in probe_ranges:
        detect_audio = audio_float[start:end]
        if len(detect_audio) < 16000 * 0.5:
            continue
        if len(detect_audio) > 16000 * 15:
            detect_audio = detect_audio[:16000 * 15]
        try:
            res = sense_voice_model.generate(
                input=detect_audio.astype(np.float32),
                cache={},
                language="auto",
                use_itn=False,
                batch_size_s=60,
            )
            raw_text = res[0]['text']
            tags = _re.findall(r'<\|([^|]+)\|>', raw_text)
            matched = MUSIC_TAGS.intersection(tags)
            if matched:
                reasons.append(f"SenseVoice tags: {matched}")
                break
        except Exception:
            continue
    return len(reasons) > 0, reasons


# 离线处理核心逻辑
# ==========================================
class ProcessRequest(BaseModel):
    session_id: str
    filepath: str
    original_filename: str
    fast_mode: bool = False
    request_start_time: float | None = None
    ui_language: str | None = "zh-CN"
    asr_language: str | None = None
    file_fetch_url: str | None = None
    callback_url: str | None = None


def _record_task_event(session_id: str, event_type: str, data: dict) -> int:
    payload = data if isinstance(data, dict) else {"message": str(data)}
    with task_event_lock:
        current_seq = task_event_seq.get(session_id, -1) + 1
        task_event_seq[session_id] = current_seq
        event_buffer = task_events.get(session_id)
        if event_buffer is None:
            event_buffer = deque(maxlen=TASK_EVENT_BUFFER_SIZE)
            task_events[session_id] = event_buffer
        event_buffer.append({"seq": current_seq, "type": event_type, "data": payload})
        return current_seq


def _reset_task_events(session_id: str):
    with task_event_lock:
        task_event_seq.pop(session_id, None)
        task_events.pop(session_id, None)

def resolve_process_filepath(req: ProcessRequest):
    temp_files = []
    if req.file_fetch_url:
        headers = {}
        if INTERNAL_WORKER_TOKEN:
            headers["X-Internal-Worker-Token"] = INTERNAL_WORKER_TOKEN
        suffix = os.path.splitext(req.original_filename or "")[1] or ".wav"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp_path = tmp.name
        tmp.close()
        temp_files.append(tmp_path)
        server_log.logger.info(
            f"[{req.session_id}] 从网关拉取音频: {req.file_fetch_url} -> {tmp_path}"
        )
        with requests.get(req.file_fetch_url, headers=headers, stream=True, timeout=300) as resp:
            resp.raise_for_status()
            with open(tmp_path, "wb") as out:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        out.write(chunk)
        return tmp_path, temp_files

    if req.filepath and os.path.exists(req.filepath):
        return req.filepath, temp_files

    raise FileNotFoundError(
        f"无法定位离线任务音频: filepath={req.filepath!r}, file_fetch_url={req.file_fetch_url!r}"
    )

def push_event(callback_url: str, session_id, event_type, data):
    _record_task_event(session_id, event_type, data)
    if not callback_url:
        return
    try:
        requests.post(
            f"{callback_url.rstrip('/')}/internal/push_event",
            json={"session_id": session_id, "event_type": event_type, "data": data},
            timeout=5,
        )
    except Exception as e:
        server_log.logger.error(f"Push event failed: {e}")

# ==========================================
# 核心：后台处理函数 (fast_mode会跳过声纹识别、全局声纹合并、性别检测)
# ==========================================
def background_transcribe_process(session_id, filepath, original_filename, fast_mode=False, request_start_time=None, ui_language="zh-CN", callback_url="", asr_language=None):

    ui_language = normalize_ui_language(ui_language)
    forced_asr_lang = normalize_asr_language(asr_language)
    lang_forced = bool(forced_asr_lang)
    server_log.logger.info(
        f"[{session_id}] >>> 后台任务启动: {original_filename} | "
        f"ui_language={ui_language} asr_language={forced_asr_lang or 'auto'}"
    )
    proc_start_time = time.time()
    temp_files_to_delete = []
    stage_timings = {
        "preprocess_audio_s": 0.0,
        "vad_clean_s": 0.0,
        "language_detect_s": 0.0,
        "speaker_diarization_s": 0.0,
        "speaker_verify_s": 0.0,
        "speaker_postprocess_s": 0.0,
        "segment_export_s": 0.0,
        "asr_s": 0.0,
    }

    # =================【新增：动态参数控制中心】=================
    ext = os.path.splitext(original_filename)[1].lower()
    is_video = ext in ['.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.webm']

    # 1. 动态合并间隙：视频提高至 2.0s，避免歌曲/音乐场景中相邻人声碎片无法合并
    DYNAMIC_MERGE_GAP_S = 2.0 if is_video else MAX_MERGE_GAP_S

    # 2. 动态单段上限：视频更早切开，避免长视频单口播整段漂移
    DYNAMIC_VAD_SAFE_DURATION_S = VIDEO_VAD_SAFE_DURATION_S if is_video else VAD_SAFE_DURATION_S

    # 3. 动态 VAD 策略：视频对停顿更敏感，早点切开防止滞后。音频讲究“语义连贯”，而视频字幕讲究“视听同步”
    #    歌曲场景会在下方检测到音乐后整体替换为粗粒度策略。
    DYNAMIC_VAD_STRATEGIES = get_dynamic_vad_strategies(is_video)

    import concurrent.futures

    def parallel_vad_slicing(audio_float, vad_session, max_workers=4):
        """
        多线程并行 VAD 切分 (Thread-based Parallel VAD Slicing)
    
        Args:
            audio_float: 完整的音频数据 (numpy array)
            vad_session: ONNX Runtime Session (需要在外部加载好)
            max_workers: 并行线程数 (建议 4-8)
    
        Returns:
            List[Dict]: 按时间排序的切分片段列表
        """
    
        # --- Worker 函数：处理单个音频块 ---
        def process_chunk(start_idx, end_idx, level):
            """
            子任务：返回 (is_final, result_data)
            - is_final=True: result_data 是 list[segment] (成品)
            - is_final=False: result_data 是 list[task_args] (新任务)
            """
            chunk_len = end_idx - start_idx
            duration_s = chunk_len / 16000.0
    
            # [Base Case 1]: 长度安全 -> 直接返回成品
            if duration_s <= DYNAMIC_VAD_SAFE_DURATION_S:
                return True, [{'start': start_idx, 'end': end_idx}]
    
            # [Base Case 2]: 策略用尽 -> 硬切兜底
            if level >= len(DYNAMIC_VAD_STRATEGIES):
                # server_log.logger.warning(f"⚠️ 硬切兜底: {start_idx}-{end_idx}")
                hard_segments = []
                curr = 0
                cut_len = int(DYNAMIC_VAD_SAFE_DURATION_S * 16000)
                while curr < chunk_len:
                    seg_end = min(curr + cut_len, chunk_len)
                    hard_segments.append({'start': start_idx + curr, 'end': start_idx + seg_end})
                    curr = seg_end
                return True, hard_segments
    
            # [Process]: 执行 VAD
            params = DYNAMIC_VAD_STRATEGIES[level]
    
            # 实例化 Processor (线程安全的关键：每个线程有自己的 wrapper，但共享底层的 session)
            processor = SileroVADProcessor(
                vad_session,
                threshold=params['threshold'],
                min_speech_duration_ms=params['min_speech'],
                min_silence_duration_ms=params['min_silence']
            )
    
            # 使用切片 (View) 避免内存拷贝
            chunk_audio = audio_float[start_idx:end_idx]
            timestamps = processor.get_speech_timestamps(chunk_audio)
    
            # 检查是否切分有效
            is_split_successful = True
            if not timestamps:
                is_split_successful = False
            elif len(timestamps) == 1:
                ts = timestamps[0]
                if ts['start'] == 0 and ts['end'] == len(chunk_audio):
                    is_split_successful = False
    
            # 生成结果
            new_tasks = []
            if not is_split_successful:
                # 没切开 -> 升级 Level，生成一个新任务
                new_tasks.append((start_idx, end_idx, level + 1))
            else:
                # 切开了 -> 生成多个子任务 (让子任务自己去判断是否需要继续切)
                # 注意：必须覆盖整个 [start_idx, end_idx)，不能只下传 timestamps 命中的区间。
                # 语音区间之间的间隙（换气、长音、间奏）若直接丢弃，这段音频将永久消失，
                # 后续的 merge_short_segments 只合并存活片段，无法找回。
                # 做法：以各语音区间的中点为界切分，把间隙并入相邻片段一起下传。
                cursor = 0
                for i, ts in enumerate(timestamps):
                    if i + 1 < len(timestamps):
                        boundary = (ts['end'] + timestamps[i + 1]['start']) // 2
                    else:
                        boundary = len(chunk_audio)
                    if boundary > cursor:
                        new_tasks.append((start_idx + cursor, start_idx + boundary, level + 1))
                        cursor = boundary
                if cursor < len(chunk_audio):
                    new_tasks.append((start_idx + cursor, end_idx, level + 1))

            return False, new_tasks
    
        # --- 主控制逻辑 ---
        final_segments = []
    
        # 线程池管理器
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 任务集合，用于追踪正在运行的任务
            futures = set()
    
            # 提交初始任务：整个音频
            initial_future = executor.submit(process_chunk, 0, len(audio_float), 0)
            futures.add(initial_future)
    
            # 循环直到所有任务完成
            while futures:
                # 等待任意一个任务完成 (return_when=FIRST_COMPLETED)
                done, _ = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED)
    
                for fut in done:
                    futures.remove(fut) # 从监控集合中移除
                    try:
                        is_final, result_data = fut.result()
    
                        if is_final:
                            # 已经是成品片段，收集起来
                            final_segments.extend(result_data)
                        else:
                            # 是新任务列表，继续提交给线程池
                            for task_args in result_data:
                                new_fut = executor.submit(process_chunk, *task_args)
                                futures.add(new_fut)
    
                    except Exception as e:
                        server_log.logger.error(f"[Parallel VAD Error] {e}")
                        # 出错时防止死循环，可以直接丢弃或做兜底处理
                        pass
    
        # 最后必须按时间排序，因为多线程完成顺序是乱的
        final_segments.sort(key=lambda x: x['start'])
        return final_segments

    def concat_audio_files(path_a, path_b, out_path, gap_samples=0):
        try:
            data_a, sr = sf.read(path_a)
            data_b, _ = sf.read(path_b)
            if gap_samples > 0:
                silence = np.zeros(gap_samples, dtype=data_a.dtype)
                combined = np.concatenate((data_a, silence, data_b))
            else:
                combined = np.concatenate((data_a, data_b))
            sf.write(out_path, combined, sr)
            return True
        except Exception as e:
            server_log.logger.error(f"Audio Merge Error: {e}")
            return False

    def parse_timestamp_str(ts_str):
        try:
            start_s, end_s = ts_str.split('-')
            return start_s, end_s
        except:
            return "00:00", "00:00"

    def push_event(event_type, data):
        _record_task_event(session_id, event_type, data)
        try:
            if callback_url:
                requests.post(
                    f"{callback_url.rstrip('/')}/internal/push_event",
                    json={"session_id": session_id, "event_type": event_type, "data": data},
                    timeout=5,
                )
        except Exception as e:
            server_log.logger.error(f"[{session_id}] Push Event Failed: {e}")

    def record_stage(stage_name, start_time):
        stage_timings[stage_name] += time.perf_counter() - start_time

    def finalize_segment(final_data, session_lang):
        final_text_content = final_data['text']
        asr_engine = asr_engine_label_from_lang(session_lang)

        if fast_mode and final_text_content.strip():
            final_text_content = process_segment_with_punctuation(
                final_text_content,
                final_data.get('index', -1),
                session_id,
                asr_engine,
            )
            final_data['text'] = final_text_content

        if fast_mode:
            final_data.pop('speaker', None)

        if fast_mode:
            _fast_mode_segment_buffer.append(final_data)
        else:
            push_event("segment_final", final_data)
        final_results_list.append(final_data)


    def recursive_vad(audio_chunk, global_start_offset, level):
        duration_s = len(audio_chunk) / 16000.0
        # 格式化时间戳，方便日志查看 (e.g. 02:30-03:40)
        fmt_start = format_timestamp(global_start_offset / 16000.0)
        fmt_end = format_timestamp((global_start_offset + len(audio_chunk)) / 16000.0)
        seg_info = f"[{fmt_start}-{fmt_end}] ({duration_s:.1f}s)"

        # [Base Case 1]: 长度安全，无需继续切分
        if duration_s <= DYNAMIC_VAD_SAFE_DURATION_S:
            # 只有在它曾经被认为"危险"进入递归后变安全了，或者第一层就安全，才会走到这
            # 如果是Level 0直接安全，我们一般不打印太多，避免刷屏
            # 但如果是Level > 0 (说明是递归下来的)，打印一下成功
            if level > 0:
                server_log.logger.info(f"[{session_id}] ✅ 片段 {seg_info} 安全，保留。")
            return [{'start': global_start_offset, 'end': global_start_offset + len(audio_chunk)}]

        # [Base Case 2]: 长度不安全，但已无策略可用 -> 硬切兜底
        if level >= len(DYNAMIC_VAD_STRATEGIES):
            server_log.logger.error(f"[{session_id}] ⚠️ 硬切兜底: {seg_info}")
            hard_segments = []
            curr = 0
            while curr < len(audio_chunk):
                cut_len = int(DYNAMIC_VAD_SAFE_DURATION_S * 16000)
                end = min(curr + cut_len, len(audio_chunk))

                hard_start_fmt = format_timestamp((global_start_offset + curr)/16000.0)
                hard_end_fmt = format_timestamp((global_start_offset + end)/16000.0)

                server_log.logger.warning(f"[{session_id}] -> 强制硬切: [{hard_start_fmt}-{hard_end_fmt}]")
                hard_segments.append({'start': global_start_offset + curr, 'end': global_start_offset + end})
                curr = end
            return hard_segments

        # 递归步骤
        params = DYNAMIC_VAD_STRATEGIES[level]
        server_log.logger.info(f"[{session_id}] 🔪 尝试 Level {level+1} 切分 (silence={params['min_silence']}ms) 对顽固片段 {seg_info}")

        processor = SileroVADProcessor(
            vad_session,
            threshold=params['threshold'],
            min_speech_duration_ms=params['min_speech'],
            min_silence_duration_ms=params['min_silence']
        )
        timestamps = processor.get_speech_timestamps(audio_chunk)

        if not timestamps or (len(timestamps) == 1 and timestamps[0]['start'] == 0 and timestamps[0]['end'] == len(audio_chunk)):
            # 没切开，或者切出来还是原样 -> 递归下一级
            server_log.logger.warning(f"[{session_id}] Level {level+1} 未能切开 {seg_info}，整体进入下一级...")
            return recursive_vad(audio_chunk, global_start_offset, level + 1)

        final_results = []
        for i, ts in enumerate(timestamps):
            sub_chunk = audio_chunk[ts['start']:ts['end']]
            sub_offset = global_start_offset + ts['start']
            sub_dur = len(sub_chunk) / 16000.0

            # 对切出来的子片段，递归调用（level + 1）
            # 只有当子片段依然超长时，level+1 才会真正生效；否则会在 Base Case 1 返回
            if sub_dur > DYNAMIC_VAD_SAFE_DURATION_S:
                fmt_sub_start = format_timestamp(sub_offset / 16000.0)
                fmt_sub_end = format_timestamp((sub_offset + len(sub_chunk)) / 16000.0)
                server_log.logger.warning(f"[{session_id}] -> 发现子片段 #{i+1} [{fmt_sub_start}-{fmt_sub_end}] ({sub_dur:.1f}s) 依然过长，递归进入 Level {level+2}...")
            sub_results = recursive_vad(sub_chunk, sub_offset, level + 1)
            final_results.extend(sub_results)
        return final_results

    # --- 合并逻辑 (仅合并同说话人的片段) ---
    def merge_short_segments(segments):
        """
        合并逻辑：
        1. 按时间顺序遍历片段。
        2. 如果 (当前片段 + 间隔 + 下一片段) 的总时长 <= DYNAMIC_VAD_SAFE_DURATION_S
           并且 间隔 (Gap) <= MAX_MERGE_GAP_S
           则合并。
        3. 否则，开始新的片段。
        """

        if not segments: return []
        merged = []
        current = segments[0]
        for next_seg in segments[1:]:
            # 计算间隔 (Gap)
            gap_seconds = (next_seg['start'] - current['end']) / 16000.0
            # 计算合并后的假想时长 (包括中间的 Gap)
            merged_duration_seconds = (next_seg['end'] - current['start']) / 16000.0
            # 只有当：间隔小 + 合并后不超长 + (隐式前提:同属于一个Speaker的原始片段) 时合并
            if (gap_seconds <= DYNAMIC_MERGE_GAP_S) and (merged_duration_seconds <= DYNAMIC_VAD_SAFE_DURATION_S):
                # 执行合并：只需要把当前片段的 end 延伸到下一个片段的 end
                # 注意：这里会把中间的静音也包含进去，但这正是我们想要的（保留上下文）
                current['end'] = next_seg['end']
            else:
                # 无法合并，封存当前片段，开始下一个
                merged.append(current)
                current = next_seg
        # 别忘了最后一个
        merged.append(current)
        return merged

    try:
        final_results_list = []
        _fast_mode_segment_buffer = []
        # 1. 预处理 (得到 float32 数组用于VAD，clean_path 用于 Diarization)
        if not fast_mode:
            push_event("progress", {"message": "正在预处理音频..."})
        preprocess_start = time.perf_counter()
        audio_float, clean_file_path = preprocess_audio(filepath)
        record_stage("preprocess_audio_s", preprocess_start)
        total_audio_duration_s = len(audio_float) / 16000.0
        direct_single_speaker_mode = total_audio_duration_s <= DIRECT_ASR_SKIP_VAD_DURATION_S

        if clean_file_path and os.path.exists(clean_file_path) and clean_file_path != filepath:
            temp_files_to_delete.append(clean_file_path)
        # =======================================================
        # [新增] 1.5 统一全局 VAD：后续语种检测与识别都复用这一次切分结果
        # =======================================================
        if fast_mode:
            pass  # 跳过中间过程，静默处理
        elif direct_single_speaker_mode:
            push_event("progress", {"message": "短音视频直通处理中..."})
        else:
            push_event("progress", {"message": "正在进行 VAD 清洗与声纹识别..."})
        vad_start = time.perf_counter()
        # None = 尚未检测（下面按分支决定检测时机）；VAD 分支需在切分前先判定音乐
        is_music, music_reasons = None, []
        if direct_single_speaker_mode:
            optimized_vad_segments = [{'start': 0, 'end': len(audio_float)}]
            server_log.logger.info(
                f"[{session_id}] 音视频时长 {total_audio_duration_s:.2f}s <= "
                f"{DIRECT_ASR_SKIP_VAD_DURATION_S:.0f}s，跳过 VAD 与 Speaker Diarization，直接按单说话人整段识别（保留 Gender 检测）。"
            )
        else:
            server_log.logger.info(f"[{session_id}] 开始 Global VAD (Shared Mode)...")
            # 音乐检测必须在 VAD 之前：歌曲需要粗粒度切分，若等 VAD 跑完再判定，
            # 碎片化的切分已经定型，后续 merge 无法还原被切碎的乐句。
            is_music, music_reasons = detect_music_content(audio_float)
            if is_music:
                DYNAMIC_VAD_STRATEGIES = get_dynamic_vad_strategies(is_video, is_music=True)
                # 歌曲：单段上限放宽到 60s，让完整乐句整体送入 ASR
                DYNAMIC_VAD_SAFE_DURATION_S = MUSIC_VAD_SAFE_DURATION_S
                DYNAMIC_MERGE_GAP_S = MUSIC_MERGE_GAP_S
                server_log.logger.info(
                    f"[{session_id}] 🎵 检测到歌曲/音乐内容，启用粗粒度 VAD "
                    f"(safe_dur={DYNAMIC_VAD_SAFE_DURATION_S}s, merge_gap={DYNAMIC_MERGE_GAP_S}s)。"
                    f"原因: {music_reasons}"
                )
            raw_vad_segments = parallel_vad_slicing(audio_float, vad_session, max_workers=8)
            optimized_vad_segments = merge_short_segments(raw_vad_segments)
            server_log.logger.info(f"[{session_id}] VAD 得到 {len(optimized_vad_segments)} 个纯净片段。")
        record_stage("vad_clean_s", vad_start)

        # =======================================================
        # [新增] 1.6 智能语种检测（复用上面的 VAD 结果）
        # 用户指定 asr_language 时锁定该语种，跳过自动检测。
        # =======================================================
        session_lang = "zh"
        language_detect_start = time.perf_counter()
        if lang_forced:
            session_lang = forced_asr_lang
            lang_name = qwen_language_display_name_zh(session_lang)
            if not fast_mode:
                push_event("progress", {
                    "message": (
                        f"Language locked: {lang_name}"
                        if ui_language == "en"
                        else f"已锁定语种：{lang_name}，跳过自动检测"
                    ),
                })
            server_log.logger.info(
                f"[{session_id}] 用户指定识别语种: {session_lang}，跳过自动检测"
            )
        else:
            if not fast_mode:
                push_event("progress", {"message": "正在检测音频语种（长视频可能需要几分钟）..."})
            session_lang = detect_language_from_vad_segments(audio_float, optimized_vad_segments)
            server_log.logger.info(f"[{session_id}] 智能检测语种结果: {session_lang}")
        record_stage("language_detect_s", language_detect_start)

        # =======================================================
        # [新增] 1.7 歌曲/音乐内容检测
        # 已在 VAD 之前完成（见 1.5），此处不再重复检测：
        # 重跑一次不仅浪费一次模型推理，还会把上面设好的粗粒度参数覆盖回更小的值。
        # direct_single_speaker_mode（短音视频直通）不走 VAD 分支，这里补一次检测。
        # =======================================================
        if is_music is None:
            is_music, music_reasons = detect_music_content(audio_float, optimized_vad_segments)
            if is_music:
                server_log.logger.info(
                    f"[{session_id}] 🎵 检测到歌曲/音乐内容，将使用 Qwen3-ASR。原因: {music_reasons}"
                )
        
        # =======================================================
        # [新逻辑] 2. 执行说话人识别 (获取 ID)
        # =======================================================
        
        # --- A. 执行说话人区分 (只为了拿时间戳和ID，不做物理切分) ---
        if fast_mode:
            server_log.logger.info(f"[{session_id}] Fast mode: 跳过 Speaker Diarization/Gender，直接进入 ASR+标点流程")
        elif direct_single_speaker_mode:
            server_log.logger.info(
                f"[{session_id}] 短音视频直通模式：跳过 Speaker Diarization / Verification，统一按单说话人处理，并保留 Gender 检测"
            )
        else:
            server_log.logger.info(f"[{session_id}] 开始 Speaker Diarization (获取 ID)...")
        diar_segments_raw = []
        speaker_diarization_start = time.perf_counter()

        # 定义一个兜底结果：短音频直通时全段归为单说话人，其余场景回退 unknown
        fallback_speaker_id = "0" if direct_single_speaker_mode else "unknown"
        fallback_diar = [[0.0, total_audio_duration_s, fallback_speaker_id]]

        if sd_pipeline and not fast_mode and not direct_single_speaker_mode:
            try:
                # 这里的 clean_file_path 是必须的，ModelScope 需要文件路径
                # 【优化】如果音频太短（例如小于 1秒），直接跳过 ModelScope，防止报错
                if total_audio_duration_s < 1.0:
                    server_log.logger.info(f"[{session_id}] Audio too short ({total_audio_duration_s:.2f}s), skipping diarization model.")
                    diar_segments_raw = fallback_diar
                else:
                    result = sd_pipeline(clean_file_path, min_num_speakers=1)
                    if 'text' in result:
                        diar_segments_raw = result['text'] # [[start, end, id], ...]
                    else:
                        diar_segments_raw = fallback_diar
            except Exception as e:
                # 【优化】降低日志级别，如果是 duration error，视为 Warning 而非 Error
                if "too short" in str(e).lower():
                    server_log.logger.warning(f"[{session_id}] Diarization skipped (audio too short): {e}")
                else:
                    server_log.logger.error(f"[{session_id}] Diarization Failed: {e}")

                diar_segments_raw = fallback_diar
        else:
             diar_segments_raw = fallback_diar
        
        # 清洗说话人数据格式，确保是 [float, float, str]
        cleaned_diar_segments = []
        for item in diar_segments_raw:
            if isinstance(item, list) and len(item) >= 3:
                cleaned_diar_segments.append([float(item[0]), float(item[1]), str(item[2])])
        # =================【修复 Bug：缝合过碎的说话人片段】=================
        cleaned_diar_segments.sort(key=lambda x: x[0])
        merged_diar = []
        if cleaned_diar_segments:
            curr = cleaned_diar_segments[0]
            for nxt in cleaned_diar_segments[1:]:
                # 如果是同一个人，并且与上一段的间隔小于等于 2.0 秒（即 MAX_MERGE_GAP_S）
                if nxt[2] == curr[2] and (nxt[0] - curr[1]) <= DYNAMIC_MERGE_GAP_S:
                    # 将当前段的结束时间向后延伸，吃掉中间的缝隙
                    curr[1] = max(curr[1], nxt[1])
                else:
                    merged_diar.append(curr)
                    curr = nxt
            merged_diar.append(curr)
        cleaned_diar_segments = merged_diar
        record_stage("speaker_diarization_s", speaker_diarization_start)
        # ====================================================================

        # =======================================================
        # 3. 核心步骤：求交集切分 (Intersection Slicing)
        # 解决 "包含多个说话人" 和 "噪音" 的终极方案
        # =======================================================
        
        final_processing_segments = []
        speaker_stats_map = {} 

        # 必须先按时间对说话人片段排序，确保切分有序
        cleaned_diar_segments.sort(key=lambda x: x[0])

        for vad_seg in optimized_vad_segments:
            # vad_seg: {'start': int, 'end': int} (采样点)
            vad_start_s = vad_seg['start'] / 16000.0
            vad_end_s = vad_seg['end'] / 16000.0
            
            # 标记当前 VAD 片段是否被至少一个说话人认领过
            has_intersection = False
            
            # 遍历所有说话人区间，寻找交集
            for diar_seg in cleaned_diar_segments:
                diar_start_s = diar_seg[0]
                diar_end_s = diar_seg[1]
                spk_id = diar_seg[2]
                
                # 计算交集：取 [最大开始时间, 最小结束时间]
                inter_start_s = max(vad_start_s, diar_start_s)
                inter_end_s = min(vad_end_s, diar_end_s)
                
                # 如果存在有效交集 (时长 > 0.05秒，防止极短碎片)
                if inter_end_s - inter_start_s > 0.05:
                    has_intersection = True
                    
                    # 转换回采样点
                    start_idx = int(inter_start_s * 16000)
                    end_idx = int(inter_end_s * 16000)
                    
                    # 边界保护
                    start_idx = max(0, start_idx)
                    end_idx = min(len(audio_float), end_idx)
                    
                    if end_idx > start_idx:
                        # === 关键：这里切出来的片段，既通过了VAD(无噪)，又在说话人区间内(无混叠) ===
                        duration_s = (end_idx - start_idx) / 16000.0
                        
                        final_processing_segments.append({
                            "start": start_idx,
                            "end": end_idx,
                            "speaker": spk_id,
                            "audio": audio_float[start_idx:end_idx]
                        })
                        
                        # 统计时长
                        speaker_stats_map[spk_id] = speaker_stats_map.get(spk_id, 0.0) + duration_s

            # [兜底策略 A]：Diarization 可能只覆盖 VAD 段的一部分，交集切分会丢掉
            # 未被认领的区间（典型如歌曲的演唱段——说话人模型对唱腔召回很差，
            # 只认出对白，整段歌词就此消失）。这里把间隙补回为 "unknown"，确保不丢字。
            # 注意：不能限制音频总时长，长音频同样会漏（199s 的歌曾因此只剩 34% 覆盖）。
            if has_intersection:
                # 收集本 VAD 段内所有已覆盖的区间（秒级）
                covered = []
                for diar_seg in cleaned_diar_segments:
                    cs = max(vad_start_s, diar_seg[0])
                    ce = min(vad_end_s, diar_seg[1])
                    if ce - cs > 0.05:
                        covered.append((cs, ce))
                covered.sort()
                # 合并重叠区间
                merged_cov = [covered[0]] if covered else []
                for s, e in covered[1:]:
                    if s <= merged_cov[-1][1]:
                        merged_cov[-1] = (merged_cov[-1][0], max(merged_cov[-1][1], e))
                    else:
                        merged_cov.append((s, e))
                # 计算间隙并补回
                prev_e = vad_start_s
                for cs, ce in merged_cov:
                    if cs - prev_e > 0.2:
                        gap_start = max(0, int(prev_e * 16000))
                        gap_end = min(len(audio_float), int(cs * 16000))
                        if gap_end > gap_start:
                            final_processing_segments.append({
                                "start": gap_start, "end": gap_end,
                                "speaker": "unknown",
                                "audio": audio_float[gap_start:gap_end]
                            })
                            server_log.logger.info(
                                f"[{session_id}] 补回 Diarization 遗漏区间: "
                                f"{format_timestamp(prev_e)}-{format_timestamp(cs)} ({cs - prev_e:.2f}s)"
                            )
                    prev_e = ce
                # 补回尾部间隙
                if vad_end_s - prev_e > 0.2:
                    gap_start = max(0, int(prev_e * 16000))
                    gap_end = min(len(audio_float), int(vad_end_s * 16000))
                    if gap_end > gap_start:
                        final_processing_segments.append({
                            "start": gap_start, "end": gap_end,
                            "speaker": "unknown",
                            "audio": audio_float[gap_start:gap_end]
                        })
                        server_log.logger.info(
                            f"[{session_id}] 补回 Diarization 遗漏尾部: "
                            f"{format_timestamp(prev_e)}-{format_timestamp(vad_end_s)} ({vad_end_s - prev_e:.2f}s)"
                        )

            # [兜底策略 B]：如果这段 VAD 语音没有任何说话人认领 (Diarization 漏检)
            # 为了防止丢字，我们将其保留，标记为 unknown
            if not has_intersection:
                # 再次确认长度，太短的噪音依然不要
                if vad_end_s - vad_start_s > 0.2:
                    final_processing_segments.append({
                        "start": vad_seg['start'],
                        "end": vad_seg['end'],
                        "speaker": "unknown",
                        "audio": audio_float[vad_seg['start']:vad_seg['end']]
                    })

        # =======================================================
        # [升级版] 3.5: 基于声纹验证的修正 (Speaker Verification Correction)
        # 优化策略：并行计算分数 -> 顺序应用逻辑
        # =======================================================
        
        # 1. 依然先排序
        final_processing_segments.sort(key=lambda x: x['start'])

        # 2. 参数定义
        SIMILARITY_THRESHOLD = 0.55
        MIN_ANCHOR_DURATION = 0.5 

        # 定义单次比对任务函数
        def compute_pair_similarity(index, seg_prev, seg_curr):
            """
            计算 index 与 index-1 之间的相似度
            返回: (index, score, message)
            """
            prev_dur = (seg_prev['end'] - seg_prev['start']) / 16000.0
            curr_dur = (seg_curr['end'] - seg_curr['start']) / 16000.0
            
            # 过滤条件
            if prev_dur < MIN_ANCHOR_DURATION:
                return index, -1.0, "Skip(PrevTooShort)"
            if curr_dur < 0.1:
                return index, -1.0, "Skip(CurrTooShort)"

            try:
                # 使用唯一文件名防止冲突
                t_prev = os.path.join(tempfile.gettempdir(), f"p_{uuid.uuid4()}.wav")
                t_curr = os.path.join(tempfile.gettempdir(), f"c_{uuid.uuid4()}.wav")
                
                try:
                    write_wav(t_prev, 16000, (seg_prev['audio']*32767).astype(np.int16))
                    write_wav(t_curr, 16000, (seg_curr['audio']*32767).astype(np.int16))
                    
                    # 这里的 sv_pipeline 通常不是线程安全的，但在 ThreadPool 中
                    # 如果显存足够，ModelScope 内部一般能处理并发，或者 Python GIL 会让其串行化但节省了IO时间
                    res = sv_pipeline([t_prev, t_curr])
                    score = res.get('score', 0.0)
                    return index, score, "OK"
                finally:
                    # 确保清理临时文件
                    if os.path.exists(t_prev): os.remove(t_prev)
                    if os.path.exists(t_curr): os.remove(t_curr)
            except Exception as e:
                return index, -1.0, f"Error: {e}"

        if len(final_processing_segments) >= 2 and sv_pipeline and not fast_mode and not direct_single_speaker_mode:
            server_log.logger.info(f"[{session_id}] 开始并行声纹验证 (Count={len(final_processing_segments)-1})...")
            speaker_verify_start = time.perf_counter()
            
            comparison_results = {} # 存储结果: {index: score}
            
            # --- 阶段一：并行计算 (IO与推理并行) ---
            # max_workers 建议设为 4-8，太高可能会爆显存或导致 CUDA 争用
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                futures = []
                for i in range(1, len(final_processing_segments)):
                    prev_seg = final_processing_segments[i-1]
                    curr_seg = final_processing_segments[i]
                    # 提交任务
                    futures.append(executor.submit(compute_pair_similarity, i, prev_seg, curr_seg))
                
                # 等待所有任务完成
                for future in concurrent.futures.as_completed(futures):
                    idx, score, msg = future.result()
                    if score >= 0:
                        comparison_results[idx] = score
            
            server_log.logger.info(f"[{session_id}] 声纹比对完成，开始合并逻辑...")

            # --- 阶段二：顺序应用传染逻辑 ---
            # 必须按顺序遍历，因为 curr 的 ID 可能会被 prev 改变，而 next 依赖改变后的 curr
            merge_count = 0
            for i in range(1, len(final_processing_segments)):
                if i not in comparison_results:
                    continue
                
                score = comparison_results[i]
                prev_seg = final_processing_segments[i-1]
                curr_seg = final_processing_segments[i]
                
                if score >= SIMILARITY_THRESHOLD:
                    # 只有当 ID 不同时才记录日志，避免刷屏
                    if curr_seg['speaker'] != prev_seg['speaker']:
                        server_log.logger.info(f"   -> [{i}] Merge Spk{curr_seg['speaker']} -> Spk{prev_seg['speaker']} (Score={score:.2f})")
                        curr_seg['speaker'] = prev_seg['speaker']
                        merge_count += 1
                else:
                    # 【新增】打印失败的分数，这样你就能看到到底低到多少了
                    if curr_seg['speaker'] != prev_seg['speaker']:
                         server_log.logger.info(f"   -> [{i}] ❌ Failed Merge Spk{curr_seg['speaker']} vs Spk{prev_seg['speaker']} (Score={score:.2f} Too Low)")
            
            if merge_count > 0:
                server_log.logger.info(f"[{session_id}] SV修正生效: 合并了 {merge_count} 处断点。")
            record_stage("speaker_verify_s", speaker_verify_start)

        # =======================================================
        # [新增] 3.6: 全局说话人融合 (Global Speaker Fusion)
        # 解决 "单人长音频被分成两人" 的终极方案
        # =======================================================
        speaker_audio_samples = {}
        for seg in final_processing_segments:
            spk = seg['speaker']
            if spk not in speaker_audio_samples:
                speaker_audio_samples[spk] = []
            # 只收集长度 > 1.5s 的片段作为样本，避免噪音干扰
            if (seg['end'] - seg['start']) / 16000.0 > 1.5:
                speaker_audio_samples[spk].append(seg['audio'])
    

        if sv_pipeline and not fast_mode and not direct_single_speaker_mode:
            server_log.logger.info(f"[{session_id}] 开始全局说话人融合检测...")
            speaker_verify_start = time.perf_counter()
            
            # 2. 构建合并映射表
            # 获取所有现存的 ID
            unique_speakers = sorted(list(speaker_audio_samples.keys()))
            merge_map = {spk: spk for spk in unique_speakers} # 初始映射：自己映射自己
            
            # 3. 两两比对所有 Speaker ID (不仅仅是相邻的)
            import random
            GLOBAL_MERGE_THRESHOLD = 0.55  # 全局合并阈值，可以设得比局部略低，因为是平均声纹
            
            for i in range(len(unique_speakers)):
                spk_a = unique_speakers[i]
                if spk_a not in speaker_audio_samples or len(speaker_audio_samples[spk_a]) == 0: continue
                
                for j in range(i + 1, len(unique_speakers)):
                    spk_b = unique_speakers[j]
                    if spk_b not in speaker_audio_samples or len(speaker_audio_samples[spk_b]) == 0: continue
                    
                    # 如果已经被映射过了，就跳过（比如 A已经合并不B了，就不用比 A和C了，等B和C比）
                    # 这里简化处理，两两硬比
                    
                    try:
                        # 拼凑音频 A (最多取 3 段，防止太长爆显存)
                        samples_a = speaker_audio_samples[spk_a]
                        wav_a = np.concatenate(random.sample(samples_a, min(3, len(samples_a))))
                        
                        # 拼凑音频 B
                        samples_b = speaker_audio_samples[spk_b]
                        wav_b = np.concatenate(random.sample(samples_b, min(3, len(samples_b))))
                        
                        # 临时保存文件
                        t_a = os.path.join(tempfile.gettempdir(), f"g_a_{uuid.uuid4()}.wav")
                        t_b = os.path.join(tempfile.gettempdir(), f"g_b_{uuid.uuid4()}.wav")
                        
                        write_wav(t_a, 16000, (wav_a * 32767).astype(np.int16))
                        write_wav(t_b, 16000, (wav_b * 32767).astype(np.int16))
                        
                        score = sv_pipeline([t_a, t_b])['score']
                        
                        os.remove(t_a)
                        os.remove(t_b)
                        
                        server_log.logger.info(f"   -> 全局比对 Spk{spk_a} vs Spk{spk_b}: Score={score:.3f}")
                        
                        if score >= GLOBAL_MERGE_THRESHOLD:
                            # 发现 A 和 B 是同一个人 -> 记录映射
                            # 总是把后面的 ID 映射到前面的 ID
                            merge_map[spk_b] = merge_map[spk_a] 
                            server_log.logger.info(f"   -> 决定合并: Spk{spk_b} -> Spk{spk_a}")
                            
                    except Exception as e:
                        server_log.logger.error(f"Global Merge Error: {e}")
    
            # 4. 应用映射
            global_merge_count = 0
            for seg in final_processing_segments:
                original_spk = seg['speaker']
                # 递归查找最终映射 (处理 A->B, B->C 的情况)
                target_spk = original_spk
                while target_spk != merge_map[target_spk]:
                    target_spk = merge_map[target_spk]
                
                if seg['speaker'] != target_spk:
                    seg['speaker'] = target_spk
                    global_merge_count += 1
                    
            if global_merge_count > 0:
                server_log.logger.info(f"[{session_id}] 全局融合生效：更新了 {global_merge_count} 个片段的归属。")
            record_stage("speaker_verify_s", speaker_verify_start)

        # =======================================================
        # 4. 统计与排序 (整理切片顺序)
        # =======================================================
        
        # 1. 再次确保时间顺序正确
        final_processing_segments.sort(key=lambda x: x['start'])

        # 2. [关键修正] 重新统计时长 
        # (因为 Step 3.5 可能修改了 speaker，之前的 speaker_stats_map 已经脏了)
        final_stats_map = {}
        for item in final_processing_segments:
            dur = (item['end'] - item['start']) / 16000.0
            spk = item['speaker']
            final_stats_map[spk] = final_stats_map.get(spk, 0.0) + dur

        total_speech_duration = sum(final_stats_map.values())
        force_single_speaker_mode = False
        dominant_speaker_id = None
        dominant_speaker_duration = 0.0
        dominant_speaker_ratio = 0.0
        if (
            not fast_mode
            and not direct_single_speaker_mode
            and len(final_stats_map) > 1
            and total_speech_duration > 0
        ):
            dominant_speaker_id, dominant_speaker_duration = max(final_stats_map.items(), key=lambda x: x[1])
            dominant_speaker_ratio = dominant_speaker_duration / total_speech_duration
            server_log.logger.info(
                f"[{session_id}] 主说话人占比检查: dominant={dominant_speaker_id} "
                f"duration={dominant_speaker_duration:.3f}s total={total_speech_duration:.3f}s "
                f"ratio={dominant_speaker_ratio:.4%} threshold={SINGLE_SPEAKER_DOMINANCE_RATIO:.0%}"
            )
            if dominant_speaker_ratio >= SINGLE_SPEAKER_DOMINANCE_RATIO:
                force_single_speaker_mode = True
                server_log.logger.info(
                    f"[{session_id}] 主说话人 {dominant_speaker_id} 占比 {dominant_speaker_ratio:.2%} "
                    f">= {SINGLE_SPEAKER_DOMINANCE_RATIO:.0%}，覆盖聚类出的多人结果，回退为单说话人处理。"
                )

        # 3. 按新的说话时长生成 ID 映射
        # 注意：这里会过滤掉总时长几乎为0的说话人
        sorted_raw = sorted(final_stats_map.items(), key=lambda x: x[1], reverse=True)
        id_mapping = {} 
        final_stats_list = []
        valid_rank_counter = 0 # 独立的计数器，确保 ID 连续 (0, 1, 2...)

        speaker_postprocess_start = time.perf_counter()
        if fast_mode:
            # Fast mode 仅做 ASR + 标点，不做说话人重映射与性别检测。
            id_mapping = {old_id: old_id for old_id, _ in sorted_raw}
        elif direct_single_speaker_mode or force_single_speaker_mode:
            # 短音视频直通或聚类后单说话人回退场景：统一按单说话人处理，但保留性别检测。
            single_speaker_duration = total_speech_duration or total_audio_duration_s
            id_mapping = {old_id: "0" for old_id, _ in sorted_raw}

            detected_gender = "unknown"
            gender_sample = None
            sample_speaker_id = "0"
            if force_single_speaker_mode and dominant_speaker_id is not None:
                sample_speaker_id = dominant_speaker_id
            samples = speaker_audio_samples.get(sample_speaker_id, [])
            if samples:
                gender_sample = max(samples, key=len)
            elif final_processing_segments:
                gender_sample = max((seg['audio'] for seg in final_processing_segments), key=len, default=None)

            if gender_sample is not None and len(gender_sample) > 0:
                single_start = time.perf_counter()
                try:
                    detected_gender = detect_gender(gender_sample)
                    single_end = time.perf_counter()
                    duration_ms = (single_end - single_start) * 1000
                    mode_label = "cluster fallback mode" if force_single_speaker_mode else "short direct mode"
                    server_log.logger.info(
                        f"[{session_id}] Gender Detection for Spk 0 ({mode_label}): "
                        f"{detected_gender} (Time: {duration_ms:.2f}ms)"
                    )
                except Exception as e:
                    mode_label = "cluster fallback mode" if force_single_speaker_mode else "short direct mode"
                    server_log.logger.error(f"Gender detection error in {mode_label}: {e}")

            final_stats_list.append({"id": "0", "duration": single_speaker_duration, "gender": detected_gender})
            if force_single_speaker_mode:
                server_log.logger.info(
                    f"[{session_id}] 聚类后单说话人回退：主说话人占比 {dominant_speaker_ratio:.2%}，"
                    f"统一映射到 Spk 0，最终 speaker_count=1，Gender={detected_gender}。"
                )
            else:
                server_log.logger.info(
                    f"[{session_id}] 短音视频单说话人直通：统一映射到 Spk 0，跳过 Speaker Verification，保留 Gender={detected_gender}。"
                )
        else:
            # 记录性别检测的总开始时间
            gender_total_start = time.perf_counter()
            for old_id, duration in sorted_raw:
                # [新增] 过滤规则：如果总时长 <= 0.7秒，视为无效说话人/噪音，不分配ID
                if duration <= 0.7:
                    server_log.logger.info(f"[{session_id}] 过滤无效说话人: {old_id} (Total Dur={duration:.3f}s)")
                    continue

                new_id = str(valid_rank_counter)
                id_mapping[old_id] = new_id

                # 【新增】从样本中检测性别
                # 取该说话人最长的一个片段作为性别判断依据
                samples = speaker_audio_samples.get(old_id, [])
                detected_gender = "male" # 默认值
                if samples:
                    # 取最长的片段
                    longest_sample = max(samples, key=len)
                    single_start = time.perf_counter()
                    try:
                        detected_gender = detect_gender(longest_sample)
                        single_end = time.perf_counter()
                        # 记录单次检测耗时到日志
                        duration_ms = (single_end - single_start) * 1000
                        server_log.logger.info(
                            f"[{session_id}] Gender Detection for Spk {old_id}: "
                            f"{detected_gender} (Time: {duration_ms:.2f}ms)"
                        )
                    except Exception as e:
                        server_log.logger.error(f"Gender detection error: {e}")

                final_stats_list.append({"id": new_id, "duration": duration, "gender": detected_gender})
                valid_rank_counter += 1

            # 记录并打印整组说话人性别检测的总耗时
            gender_total_end = time.perf_counter()
            total_gender_time = (gender_total_end - gender_total_start) * 1000
            server_log.logger.info(
                f"[{session_id}] Total Gender Detection Time for all speakers: {total_gender_time:.2f}ms"
            )
        record_stage("speaker_postprocess_s", speaker_postprocess_start)

            
        # 4. 统一替换最终的 ID，并将未映射的"孤儿"片段归并到时间最近的有效说话人
        #    （避免因 Diarization 在歌曲/音乐场景下误判说话人导致歌词丢失）
        final_segments_filtered = []
        # 预先确定一个默认说话人（时长最长的有效说话人）
        default_speaker = "0"
        if id_mapping:
            default_speaker = next(iter(id_mapping.values()))

        for item in final_processing_segments:
            old = item['speaker']
            if old in id_mapping:
                item['speaker'] = id_mapping[old]
            else:
                # 不丢弃：将孤儿片段归并到时间上最近的有效说话人
                item_mid = (item['start'] + item['end']) / 2.0
                best_spk = default_speaker
                best_dist = float('inf')
                for other in final_processing_segments:
                    if other['speaker'] in id_mapping:
                        other_mid = (other['start'] + other['end']) / 2.0
                        dist = abs(item_mid - other_mid)
                        if dist < best_dist:
                            best_dist = dist
                            best_spk = id_mapping[other['speaker']]
                item['speaker'] = best_spk
                server_log.logger.info(f"[{session_id}] 孤儿片段 ({(item['end']-item['start'])/16000.0:.2f}s) 归并到 Spk {best_spk}")
            final_segments_filtered.append(item)
        
        # 更新为过滤后的列表
        final_processing_segments = final_segments_filtered

        # =======================================================
        # 4.5 [合并相邻同说话人碎片] 防止歌曲/音乐场景下碎片过多导致 ASR 缺少上下文
        # =======================================================
        final_processing_segments.sort(key=lambda x: x['start'])
        if len(final_processing_segments) > 1:
            merged_segments = [final_processing_segments[0]]
            for seg in final_processing_segments[1:]:
                prev = merged_segments[-1]
                gap_s = (seg['start'] - prev['end']) / 16000.0
                merged_dur_s = (seg['end'] - prev['start']) / 16000.0
                # 同说话人 + 间隙 <= DYNAMIC_MERGE_GAP_S + 合并后不超长 → 合并
                if (seg['speaker'] == prev['speaker']
                        and gap_s <= DYNAMIC_MERGE_GAP_S
                        and merged_dur_s <= DYNAMIC_VAD_SAFE_DURATION_S):
                    # 用原始 audio_float 重新切出完整的合并段（含中间静音上下文）
                    new_start = prev['start']
                    new_end = seg['end']
                    merged_segments[-1] = {
                        "start": new_start,
                        "end": new_end,
                        "speaker": prev['speaker'],
                        "audio": audio_float[new_start:new_end],
                    }
                else:
                    merged_segments.append(seg)
            if len(merged_segments) < len(final_processing_segments):
                server_log.logger.info(
                    f"[{session_id}] 碎片合并: {len(final_processing_segments)} -> {len(merged_segments)} 个切片"
                )
            final_processing_segments = merged_segments

        server_log.logger.info(f"[{session_id}] 最终切分: {len(final_processing_segments)} 个切片 (Intersection Mode)。")

        # 推送给前端：fast_mode 下不返回说话人统计。
        if not fast_mode:
            push_event("speaker_stats", {
                "speaker_count": len(final_stats_list),
                "speakers": final_stats_list,
                "detected_lang": session_lang,
                "detected_lang_name": qwen_language_display_name_zh(session_lang),
                "lang_forced": lang_forced,
            })
        
        if not fast_mode:
            push_event("progress", {"message": f"精细切分完成，准备识别 {len(final_processing_segments)} 个切片..."})


        # =======================================================
        # 5. 保存切片、动态批处理、识别 (携带 Speaker ID)
        # =======================================================
        session_seg_dir = os.path.join(SEGMENT_DIR, session_id)
        os.makedirs(session_seg_dir, exist_ok=True)
        segment_export_start = time.perf_counter()

        segment_paths = []
        segment_ids = []
        segment_durations = []
        segment_urls = []
        segment_time_ranges = []
        segment_speakers = [] # [新增] 同步存储 speaker

        for i, item in enumerate(final_processing_segments):
            seg_audio = item['audio']
            seg_len_s = len(seg_audio) / 16000.0
            segment_durations.append(seg_len_s)
            
            seg_int16 = (np.clip(seg_audio, -1.0, 1.0) * 32767).astype(np.int16)
            seg_filename = f"{i}.wav"
            seg_path = os.path.join(session_seg_dir, seg_filename)
            write_wav(seg_path, 16000, seg_int16)
            
            segment_paths.append(seg_path)
            segment_ids.append(f"{session_id}_{i}")
            # 生成前端可访问的 URL
            segment_urls.append(f"/segments/{session_id}/{seg_filename}")
            segment_time_ranges.append((item['start'], item['end']))
            segment_speakers.append(item['speaker']) # 记录说话人

        del audio_float

        batches = []
        current_batch_indices = []
        current_batch_duration = 0.0

        for i, dur in enumerate(segment_durations):
            if (current_batch_duration + dur > MAX_BATCH_DURATION_S) or (len(current_batch_indices) >= MAX_BATCH_COUNT):
                if current_batch_indices: batches.append(current_batch_indices)
                current_batch_indices = [i]
                current_batch_duration = dur
            else:
                current_batch_indices.append(i)
                current_batch_duration += dur
        if current_batch_indices: batches.append(current_batch_indices)
        record_stage("segment_export_s", segment_export_start)

        # [新增] 缓存状态：包含 结果对象 和 原始文本
        pending_entry = None 
        # [新增] 阈值：如果两个片段间隔小于 1秒 (16000 * 1 = 16000 samples)，视为连续
        MERGE_THRESHOLD_SAMPLES = 16000
        import re

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            for batch_idx, indices in enumerate(batches):
                b_paths = [segment_paths[i] for i in indices]
                b_ids = [segment_ids[i] for i in indices]
                b_ranges = [segment_time_ranges[i] for i in indices]
                b_urls = [segment_urls[i] for i in indices]
                b_speakers = [segment_speakers[i] for i in indices] # 取出speaker
                
                # 时间戳格式化用于显示
                b_timestamps = [f"{format_timestamp(r[0]/16000.0)}-{format_timestamp(r[1]/16000.0)}" for r in b_ranges]
                
                if not fast_mode:
                    push_event("progress", {"message": f"正在识别第 {batch_idx+1}/{len(batches)} 批次..."})

                # 1. 基础 ASR 识别(根据语种动态路由，歌曲内容强制走 Qwen3)
                raw_texts = []
                asr_engine = asr_engine_label_from_lang(session_lang)
                if should_use_firered_asr(session_lang) and not is_music:
                    # 中文、英文、粤语及中文方言继续使用 FireRedASR。
                    asr_start = time.perf_counter()
                    res = file_asr_model.transcribe(b_ids, b_paths)
                    record_stage("asr_s", asr_start)
                    raw_texts = [r.get('text', "") for r in res] if res else [""] * len(b_paths)
                else:
                    # 其他外语 或 歌曲/音乐内容 统一切换到 Qwen3-ASR-1.7B。
                    asr_engine = "Qwen3-ASR-1.7B"
                    asr_start = time.perf_counter()
                    qwen_lang_name = qwen_language_name_from_code(session_lang)
                    if lang_forced and not qwen_lang_name:
                        qwen_lang_name = session_lang
                    qwen_res = qwen_file_asr_model.transcribe(
                        audio=b_paths,
                        language=qwen_lang_name,
                    )
                    raw_texts = [getattr(item, 'text', '') or '' for item in qwen_res] if qwen_res else [""] * len(b_paths)
                    locked_lang, raw_texts = _stabilize_qwen_transcripts(
                        b_paths, raw_texts, session_lang, lang_forced=lang_forced
                    )
                    if locked_lang != session_lang:
                        server_log.logger.info(
                            f"[{session_id}] Qwen 语种锁定: {session_lang} -> {locked_lang}"
                        )
                        session_lang = locked_lang
                    record_stage("asr_s", asr_start)

                future_map = {}
                batch_raw_items = [None] * len(raw_texts) # 初始化

                # 初始化 batch_raw_items 占位符
                batch_raw_items = [None] * len(raw_texts)

                for local_i, text in enumerate(raw_texts): 
                    clean_text = text.strip()
                    if not clean_text: 
                        continue
                        
                    global_idx = indices[local_i]
                    
                    # 清洗掉标点符号，方便进行纯文本比对
                    test_text = re.sub(r'[^\w\u4e00-\u9fa5]', '', clean_text)
                    is_hallucination = any(h in test_text for h in HALLUCINATION_LIST)
                    
                    # 日/韩等外语不要走中文标点模型，避免把歌词改坏。
                    use_punct = (
                        not fast_mode
                        and len(clean_text) >= 2
                        and not is_hallucination
                        and session_lang in ("zh", "yue", "en")
                    )
                    if fast_mode:
                        batch_raw_items[local_i] = {
                            "local_i": local_i,
                            "global_idx": global_idx,
                            "raw_text": clean_text,
                            "timestamp_str": b_timestamps[local_i],
                            "sample_range": b_ranges[local_i],
                            "segment_url": b_urls[local_i],
                            "file_path": b_paths[local_i],
                            "id": b_ids[local_i],
                            "speaker": b_speakers[local_i]
                        }
                    elif use_punct:
                        fut = executor.submit(
                            process_segment_with_punctuation,
                            clean_text,
                            global_idx,
                            session_id,
                            asr_engine,
                        )
                        future_map[fut] = local_i
                        
                        batch_raw_items[local_i] = {
                            "local_i": local_i,
                            "global_idx": global_idx,
                            "raw_text": clean_text,
                            "timestamp_str": b_timestamps[local_i],
                            "sample_range": b_ranges[local_i],
                            "segment_url": b_urls[local_i],
                            "file_path": b_paths[local_i],
                            "id": b_ids[local_i],
                            "speaker": b_speakers[local_i]
                        }
                    elif len(clean_text) >= 2 and not is_hallucination:
                        batch_raw_items[local_i] = {
                            "local_i": local_i,
                            "global_idx": global_idx,
                            "raw_text": clean_text,
                            "timestamp_str": b_timestamps[local_i],
                            "sample_range": b_ranges[local_i],
                            "segment_url": b_urls[local_i],
                            "file_path": b_paths[local_i],
                            "id": b_ids[local_i],
                            "speaker": b_speakers[local_i]
                        }
                    else:
                        batch_raw_items[local_i] = None

                punctuated_texts = [""] * len(raw_texts)
                if fast_mode:
                    for item in batch_raw_items:
                        if item:
                            punctuated_texts[item['local_i']] = item['raw_text']

                for fut in concurrent.futures.as_completed(future_map):
                    local_i = future_map[fut]
                    try:
                        punctuated_texts[local_i] = fut.result()
                    except Exception as e:
                        server_log.logger.error(f"Punctuation Error: {e}")
                        if batch_raw_items[local_i]:
                            punctuated_texts[local_i] = batch_raw_items[local_i]["raw_text"]

                # 日/韩等未走中文标点的片段，直接用 ASR 原文，否则结果全是空串。
                for item in batch_raw_items:
                    if item and not (punctuated_texts[item['local_i']] or "").strip():
                        punctuated_texts[item['local_i']] = item['raw_text']

                # === 顺序扫描与智能合并 (需判断 Speaker 是否一致) ===
                for item in batch_raw_items:
                    if not item: continue
                    
                    current_punctuated = punctuated_texts[item['local_i']]
                    if not (current_punctuated or "").strip():
                        continue
                    current_res = {
                        "index": item['global_idx'],
                        "text": current_punctuated,
                        "timestamp": item['timestamp_str'],
                        "start_ts": item['sample_range'][0] / 16000.0,
                        "end_ts": item['sample_range'][1] / 16000.0,
                        "segment_url": item['segment_url'],
                        "file_path": item['file_path'],
                        "id": item['id'],
                        "speaker": item['speaker'] 
                    }

                    current_start_sample = item['sample_range'][0]
                    current_end_sample = item['sample_range'][1]
                    current_raw_text = item['raw_text']
                    current_speaker = item['speaker']

                    if pending_entry is None:
                        pending_entry = {
                            'data': current_res,
                            'raw_text': current_raw_text,
                            'sample_end': current_end_sample,
                            'speaker': current_speaker,
                            # 记录构成本段的原始片段（合并后用于逐片段强制对齐）
                            'pieces': [{
                                'start_ts': current_res['start_ts'],
                                'end_ts': current_res['end_ts'],
                                'file_path': current_res['file_path'],
                            }],
                        }
                    else:
                        prev_res = pending_entry['data']
                        prev_raw = pending_entry['raw_text']
                        prev_end = pending_entry['sample_end']
                        prev_speaker = pending_entry['speaker']

                        # ================== 【修改点开始：放宽合并条件与引入语义判断】 ==================
                        # 1. 计算Gap与时间阈值放宽
                        gap = current_start_sample - prev_end
                        # 统一使用 2 秒 (32000) 阈值，ForcedAligner 已能精确处理字幕时间轴
                        MERGE_THRESHOLD_SAMPLES = 32000
                        is_tight = gap < MERGE_THRESHOLD_SAMPLES
                        
                        # 2. 严格检查上一句和本句的语义状态
                        prev_text_str = prev_res['text'].strip()
                        curr_text_str = current_res['text'].strip()
                        
                        import re
                        # 是否以句末标点（句号、问号、感叹号）结尾
                        is_sentence_end = bool(re.search(r'[。？！.?!]$', prev_text_str))
                        
                        # 3. [新增] 强关联词语境检查
                        # 当下一句以助词、连词等不能独立成句的词开头，或者上一句以这类词（哪怕被误加了标点）结尾时，强行视为未说完
                        force_merge_starters = r'^(乃至|从而|以及|充当|关于|哪怕|因为|因此|因而|对于|就是|并且|所以|既然|无论|不论|不管|即使|只要|只有|如果|可是|甚至|而且|或者|否则|还是|那么|除非|然而|不过|但是|的|地|得|和|与|及|或|而|也|那|就|又|都|才|却|连|并|更|越|让|把|被|对|从|在|是)'
                        force_merge_enders = r'(也就是说|换句话说|也就是|实际上|事实上|乃至|甚至|以及|而且|并且|或者|还是|就是|但是|可是|不过|然而|否则|所以|因为|如果|只要|只有|即使|不论|不管|对于|关于|至于|按照|依照|根据|其实|比如|例如|的|地|得|和|与|或|而|也|那|就|又|都|才|却|连|并|更|让|把|被|对|从|在|将|使|自|由|到|往|朝|向|按|依|据|论|拿|用|以|凭|是)[。？！.?!]?$'
                        
                        is_semantic_continuous = bool(re.search(force_merge_starters, curr_text_str)) or bool(re.search(force_merge_enders, prev_text_str))


                        # 4. 长度兜底检查
                        MAX_SAFE_LENGTH = 800
                        current_len_combined = len(prev_text_str) + len(current_res['text'])
                        is_length_safe = current_len_combined < MAX_SAFE_LENGTH

                        # 5. 检查 Speaker 是否一致
                        is_same_speaker = (prev_speaker == current_speaker)

                        # 【已移除视频抢跑逻辑】Qwen3-ForcedAligner 已能精确处理字幕时间轴，不需要启发式禁用规则
                        # fast_mode 仅按紧邻规则合并，避免额外复杂语义判断。
                        if fast_mode:
                            should_merge = is_same_speaker and is_length_safe and is_tight
                        else:
                            # 纯音频模式和视频模式统一策略，由强制对齐器精确确定字幕时间
                            should_merge = is_same_speaker and (
                                (is_length_safe and (is_tight or not is_sentence_end))
                                or is_semantic_continuous
                            )

                        if should_merge:
                            # === 执行合并 (代码保持不变) ===
                            merged_raw = prev_raw + " " + current_raw_text # 加个空格防止粘连

                            # 拼接音频（插入间隙静音，保持合并音频时间轴与原始视频对齐，
                            # 确保 ForcedAligner 产生的时间戳精确匹配说话时机）
                            new_filename = f"{prev_res['id']}_merged.wav"
                            new_filepath = os.path.join(session_seg_dir, new_filename)
                            concat_audio_files(prev_res['file_path'], current_res['file_path'], new_filepath, gap_samples=max(gap, 0))

                            # --- [核心优化] 增量重新标点策略 (只取【上句尾】+【本句首】，且去标点) ---
                            prev_punctuated_text = prev_res['text']
                            curr_punctuated_text = current_res['text']

                            # ================== 【修复点开始】 ==================
                            # 必须定义 prev_matches 变量，否则后面会报错
                            punc_pattern = r'[。？！.?!]'
                            prev_matches = list(re.finditer(punc_pattern, prev_punctuated_text))
                            # ================== 【修复点结束】 ==================

                            if not prev_matches:
                                prev_split_index = 0
                            else:
                                last_match = prev_matches[-1]
                                # 判断上一句是否以标点结尾
                                is_ends_with_punc = (last_match.end() == len(prev_punctuated_text)) or \
                                                    (len(prev_punctuated_text) - last_match.end() < 2)
                                
                                if is_ends_with_punc:
                                    # 如果以标点结尾，取倒数第二个标点后的内容 (e.g. "AAA。BBB。" -> 取 "BBB。")
                                    if len(prev_matches) >= 2:
                                        prev_split_index = prev_matches[-2].end()
                                    else:
                                        prev_split_index = 0 # 只有一个句子，那只能取全句
                                else:
                                    # 如果没以标点结尾，取最后一个标点后的内容 (e.g. "AAA。BBB" -> 取 "BBB")
                                    prev_split_index = last_match.end()

                            prev_prefix_part = prev_punctuated_text[:prev_split_index] # 不参与重修的前缀
                            prev_tail_part = prev_punctuated_text[prev_split_index:]   # 参与重修的尾巴

                            # B. 处理当前句 (关键修改：严格只取第一个标点前的内容)
                            # 使用更广泛的标点集来查找分割点，包括逗号
                            split_punc_pattern = r'[，。？！,;:.?!、]'
                            curr_matches = list(re.finditer(split_punc_pattern, curr_punctuated_text))
                            
                            curr_split_index = len(curr_punctuated_text) # 默认全取(万一没标点)
                            if curr_matches:
                                # 只取第一个标点符号结束的位置
                                # e.g. "葡萄倒说葡萄酸呢，这个小儿歌..." -> 切在 "，" 后面
                                curr_split_index = curr_matches[0].end()
                            
                            curr_head_part = curr_punctuated_text[:curr_split_index]   # 参与重修的句首 (短)
                            curr_suffix_part = curr_punctuated_text[curr_split_index:] # 不参与重修的剩余 (长)

                            # C. 清洗标点 (去除所有干扰标点，变成纯文字)
                            remove_punc_pattern = r'[，。？！,;:.?!、]'
                            # e.g. "BBB。" -> "BBB"
                            clean_tail = re.sub(remove_punc_pattern, '', prev_tail_part).strip()
                            # e.g. "葡萄倒说葡萄酸呢，" -> "葡萄倒说葡萄酸呢"
                            clean_head = re.sub(remove_punc_pattern, '', curr_head_part).strip()

                            # D. 构建 Prompt 并请求
                            # 现在的 prompt 只有 "BBB 葡萄倒说葡萄酸呢" 这么短
                            prompt_input = f"{clean_tail} {clean_head}".strip()
                            
                            # ================== 【修改点开始：优化合并日志打印】 ==================
                            # 确定触发合并的具体原因，方便查证
                            merge_reasons = []
                            if is_tight:
                                merge_reasons.append(f"时间紧邻(Gap={gap})")
                            if not is_sentence_end:
                                merge_reasons.append("上句无结束标点")
                            if is_semantic_continuous:
                                merge_reasons.append("命中语义强关联词")
                            
                            reason_str = " + ".join(merge_reasons) if merge_reasons else "强制兜底合并"

                            # 使用多行结构化打印，视觉上更清晰
                            server_log.logger.info(f"[{session_id}] 🔗 触发段落缝合 | 说话人: {current_speaker} | 原因: {reason_str}")
                            server_log.logger.info(f"[{session_id}]    -> 🧩 原始接缝: [...{prev_text_str[-8:] if len(prev_text_str)>8 else prev_text_str}] <Gap> [{curr_text_str[:8]}...]")
                            server_log.logger.info(f"[{session_id}]    -> 🤖 大模型 Prompt: '{prompt_input}'")
                            # ================== 【修改点结束】 ==================

                            if fast_mode or session_lang not in ("zh", "yue", "en"):
                                corrected_boundary = prompt_input
                            else:
                                corrected_boundary = utils.send_request(
                                    prompt_input,
                                    get_punct_url(session_id=session_id, route_source="boundary_merge"),
                                    session_id,
                                    server_log.logger,
                                )
                                if not corrected_boundary:
                                    corrected_boundary = prompt_input

                            # E. 缝合最终结果
                            # [上句固定前缀] + [LLM修补的接缝] + [本句固定后缀]
                            final_text = prev_prefix_part + corrected_boundary + curr_suffix_part
                            # 修补英文标点后被 strip() 吞掉的空格（如 "another.Instead" → "another. Instead"）
                            final_text = fix_english_spacing(final_text)

                            # F. 更新数据结构
                            prev_res['text'] = final_text
                            prev_res['file_path'] = new_filepath
                            prev_res['segment_url'] = f"/segments/{session_id}/{new_filename}"
                            
                            s_t, _ = parse_timestamp_str(prev_res['timestamp'])
                            _, e_t = parse_timestamp_str(current_res['timestamp'])
                            prev_res['timestamp'] = f"{s_t}-{e_t}"
                            prev_res['end_ts'] = current_res['end_ts']

                            pending_entry['raw_text'] = merged_raw
                            pending_entry['sample_end'] = current_end_sample

                            # 追加本次并入的原始片段，并同步到落盘数据。
                            # 合并会把不连续片段拼成长音频（中间填静音），
                            # ForcedAligner 在这种输入上会把时间戳整体塌缩；
                            # 保留片段边界，下游即可改为逐片段对齐。
                            pending_entry['pieces'].append({
                                'start_ts': current_res['start_ts'],
                                'end_ts': current_res['end_ts'],
                                'file_path': current_res['file_path'],
                            })
                            prev_res['piece_spans'] = list(pending_entry['pieces'])

                        else:
                            # 这是一个完整的句子结束了，不能再合并了，准备归档
                            final_data = pending_entry['data']
                            finalize_segment(final_data, session_lang)

                            pending_entry = {
                                'data': current_res,
                                'raw_text': current_raw_text,
                                'sample_end': current_end_sample,
                                'speaker': current_speaker,
                                'pieces': [{
                                    'start_ts': current_res['start_ts'],
                                    'end_ts': current_res['end_ts'],
                                    'file_path': current_res['file_path'],
                                }],
                            }

        if pending_entry:
            # 处理最后一个遗留的片段
            final_data = pending_entry['data']
            finalize_segment(final_data, session_lang)

        if lang_forced:
            refined_lang = session_lang
        else:
            refined_lang = refine_session_lang_from_text(session_lang, final_results_list)
        if refined_lang != session_lang:
            server_log.logger.info(
                f"[{session_id}] ASR 文本推断语种: {session_lang} -> {refined_lang}"
            )
            session_lang = refined_lang

        result_json_path = os.path.join(session_seg_dir, "final_result.json")
        with open(result_json_path, "w", encoding="utf-8") as f:
            json.dump(final_results_list, f, ensure_ascii=False, indent=2)

        proc_end_time = time.time()
        proc_duration_s = proc_end_time - proc_start_time
        total_duration_s = proc_duration_s
        if request_start_time:
            try:
                total_duration_s = proc_end_time - float(request_start_time)
            except (TypeError, ValueError):
                total_duration_s = proc_duration_s
        pretty_proc_duration = format_elapsed_time(proc_duration_s)
        pretty_total_duration = format_elapsed_time(total_duration_s)
        display_name = os.path.basename(original_filename) or original_filename
        stage_timing_summary = {k: round(v, 3) for k, v in stage_timings.items() if v > 0}
        stage_timing_summary["accounted_stage_s"] = round(sum(stage_timings.values()), 3)
        stage_timing_summary["total_pipeline_s"] = round(proc_duration_s, 3)
        server_log.logger.info(f"[{session_id}] Stage Timing Summary: {json.dumps(stage_timing_summary, ensure_ascii=False)}")

        gateway_base = None if fast_mode else resolve_gateway_base_url(callback_url)
        if not fast_mode and gateway_base:
            if not sync_session_artifacts_to_gateway(
                session_id,
                session_seg_dir,
                final_results_list,
                gateway_base,
                internal_token=INTERNAL_WORKER_TOKEN,
                logger=server_log.logger,
            ):
                push_event("error", {"message": "同步识别产物到网关失败，请稍后重试。"})
            elif is_video:
                push_event("asr_done", {
                    "message": f"{display_name} 语音识别完成，正在生成字幕...",
                    "original_filename": original_filename,
                    "ui_language": ui_language,
                    "session_lang": session_lang,
                    "audio_duration": f"{total_audio_duration_s:.1f}s",
                    "audio_duration_seconds": round(total_audio_duration_s, 3),
                    "proc_duration": pretty_proc_duration,
                    "proc_duration_seconds": round(proc_duration_s, 3),
                    "total_duration": pretty_total_duration,
                    "total_duration_seconds": round(total_duration_s, 3),
                    "speaker_count": len(final_stats_list),
                    "stage_timings": stage_timing_summary,
                })
            else:
                push_event("done", {
                    "message": f"{display_name} 精修完成（处理 {pretty_proc_duration} / 总耗时 {pretty_total_duration}）",
                    "original_filename": original_filename,
                    "ui_language": ui_language,
                    "session_lang": session_lang,
                    "audio_duration": f"{total_audio_duration_s:.1f}s",
                    "audio_duration_seconds": round(total_audio_duration_s, 3),
                    "proc_duration": pretty_proc_duration,
                    "proc_duration_seconds": round(proc_duration_s, 3),
                    "total_duration": pretty_total_duration,
                    "total_duration_seconds": round(total_duration_s, 3),
                    "speaker_count": len(final_stats_list),
                    "video_url": None,
                    "stage_timings": stage_timing_summary,
                })
        elif is_video and not fast_mode:
            push_event("error", {"message": "未配置网关回调地址，无法同步字幕产物。"})
        else:
            push_event("done", {
                "message": f"{display_name} 精修完成（处理 {pretty_proc_duration} / 总耗时 {pretty_total_duration}）",
                "original_filename": original_filename,
                "ui_language": ui_language,
                "session_lang": session_lang,
                "audio_duration": f"{total_audio_duration_s:.1f}s",
                "audio_duration_seconds": round(total_audio_duration_s, 3),
                "proc_duration": pretty_proc_duration,
                "proc_duration_seconds": round(proc_duration_s, 3),
                "total_duration": pretty_total_duration,
                "total_duration_seconds": round(total_duration_s, 3),
                "speaker_count": len(final_stats_list),
                "video_url": None,
                "stage_timings": stage_timing_summary,
                "final_segments": _fast_mode_segment_buffer if fast_mode else None,
            })

    except Exception as e:
        server_log.logger.error(f"[{session_id}] Background Task Error: {e}")
        import traceback; traceback.print_exc()
        push_event("error", {"message": str(e)})
    finally:
        for p in temp_files_to_delete:
            if os.path.exists(p):
                try: os.remove(p)
                except: pass
# ==========================================
# API 路由
# ==========================================
@app.post("/process_offline")
async def trigger_offline_process(req: ProcessRequest):
    _reset_task_events(req.session_id)
    ui_language = normalize_ui_language(req.ui_language or "zh-CN")
    waiting_before = task_queue.qsize()
    await task_queue.put(req)
    waiting_after = task_queue.qsize()
    waiting_session_order.append((req.session_id, ui_language))
    pending_after = active_task_count + waiting_after
    server_log.logger.info(
        f"[{WORKER_INSTANCE_TAG}] 任务入队: session={req.session_id} file={req.original_filename} "
        f"asr_language={req.asr_language or 'auto'} "
        f"active={active_task_count} waiting_before={waiting_before} waiting_after={waiting_after} pending={pending_after}"
    )
    _notify_waiting_queue_positions()
    return {"status": "queued_globally"}

@app.get("/task_events/{session_id}")
async def get_task_events(session_id: str, after_seq: int = Query(-1, ge=-1)):
    with task_event_lock:
        session_events = list(task_events.get(session_id, []))
        latest_seq = task_event_seq.get(session_id, -1)
    events = [event for event in session_events if event["seq"] > after_seq]
    last_event_type = events[-1]["type"] if events else None
    if not last_event_type and session_events:
        last_event_type = session_events[-1]["type"]
    return {
        "session_id": session_id,
        "events": events,
        "latest_seq": latest_seq,
        "is_terminal": last_event_type in {"done", "error", "asr_done"},
    }

@app.get("/worker_status")
async def worker_status():
    waiting = task_queue.qsize()
    active = max(active_task_count, 0)
    concurrency = max(OFFLINE_WORKER_CONCURRENCY, 1)
    return {
        "worker_instance": WORKER_INSTANCE_ID,
        "worker_tag": WORKER_INSTANCE_TAG,
        "port": OFFLINE_WORKER_PORT,
        "concurrency": concurrency,
        "active_tasks": active,
        "queue_size": waiting,
        "pending_tasks": active + waiting,
        "available_slots": max(concurrency - active, 0),
    }

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Offline ASR Worker")
    parser.add_argument("--port", type=int, default=int(os.environ.get("OFFLINE_WORKER_PORT", "7862")), help="HTTP port for the offline worker")
    args = parser.parse_args()

    uvicorn.run(app, host="0.0.0.0", port=args.port)
