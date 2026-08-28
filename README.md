# OpenCaptioner

**English** | [中文](README.zh-CN.md)

Offline speech transcription: upload audio/video or paste a Bilibili / YouTube / Douyin link to run recognition, speaker diarization, subtitle alignment and translation, plus summarization and RAG Q&A over the transcript.

This repository is the server and web frontend. **It does not ship model weights.** Mandarin, English, and Cantonese default to [FireRedASR2S](https://github.com/FireRedTeam/FireRedASR2S). Other languages and sung content use [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR).

**Live demo:** [https://www.scnet.cn/ui/aihub/agent/zhenghong/asr](https://www.scnet.cn/ui/aihub/agent/zhenghong/asr) (SCNet AI Hub; sign in with an SCNet account)

### Sample output

A subtitled video produced by OpenCaptioner (bilingual burn-in):

https://github.com/user-attachments/assets/373480c4-4429-40a7-9692-6c138140727a

## Features

- File upload and link download (Bilibili / YouTube / Douyin)
- Offline ASR, punctuation, and speaker stats
- Subtitle editing, time alignment, and burn-in
- Video summaries and RAG Q&A (optional embedding service)
- Local accounts or optional SCNet OAuth
- Optional content safety (word lists are supplied by the operator, not this repo)

## Architecture

| Role | Script | Description |
|---|---|---|
| Gateway + local offline ASR | `run_gateway.sh` | `gateway_server.py` + local `offline_worker.py` |
| Punctuation | `run_punct.sh` | `llama-server` |
| Chat / translation LLM | `run_llm.sh` | vLLM + Qwen3 |
| Embedding (optional) | `run_embed.sh` | RAG retrieval; degrades automatically if unset |
| Standalone offline worker (optional) | `run_offline_worker.sh` | Split out when you need extra capacity |

```
Browser ──► Gateway :7860 ──► local or remote offline worker
                 │
                 ├─► punctuation service
                 ├─► LLM (summary / translation / safety)
                 └─► Embedding (optional)
```

## Requirements

- Python 3.10+, Node.js 18+ (frontend build), FFmpeg, `ffprobe`
- NVIDIA or a compatible GPU (`install.sh` is a DCU-oriented helper)
- Place FireRedASR2S, model dirs, `llama-server`, and subtitle fonts on the machine (none of these are in git; see below)

### Python

```bash
pip install -r requirements.txt
# Install PyTorch for your CUDA / ROCm build
pip install vllm   # only on machines that run run_llm.sh / run_embed.sh
```

`install.sh` targets a specific DCU environment (including a private onnxruntime wheel). On generic Linux, use pip as above and install `ffmpeg`, `libsndfile`, and similar system libraries yourself.

### Frontend

```bash
cd frontend && npm install && npm run build
```

For development, `cd frontend && npm run dev` (default `http://127.0.0.1:5173`, proxied to gateway `:7860`).

## Models and third-party code (download separately)

| Use | Source | License | Local layout |
|---|---|---|---|
| ZH / EN / Yue ASR | [FireRedASR2S](https://github.com/FireRedTeam/FireRedASR2S) | Apache-2.0 | Clone as `fireredasr2s/` at the repo root; weights in `fireredasr2s/pretrained_models/FireRedASR2-AED` |
| Multilingual / singing ASR | [Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) | Apache-2.0 | `QWEN3_OFFLINE_MODEL_PATH` |
| Forced subtitle alignment | [Qwen3-ForcedAligner-0.6B](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B) | Apache-2.0 | `FORCED_ALIGNER_MODEL_PATH` |
| Chat / translation | [Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B) | Apache-2.0 | `LLM_MODEL_PATH` |
| RAG embedding (optional) | [Qwen3-Embedding-8B](https://huggingface.co/Qwen/Qwen3-Embedding-8B) | Apache-2.0 | `EMBED_MODEL_PATH` |
| Punctuation GGUF | local file (`PUNCT_MODEL`) | follow that file's source | Default `./Qwen3_Merge-596M-F16.gguf`; must work with llama.cpp OpenAI Chat API |

Example (ModelScope works well in mainland China):

```bash
git clone https://github.com/FireRedTeam/FireRedASR2S.git fireredasr2s

pip install -U modelscope
modelscope download --model Qwen/Qwen3-ASR-1.7B --local_dir /path/to/Qwen3-ASR-1.7B
modelscope download --model Qwen/Qwen3-ForcedAligner-0.6B --local_dir /path/to/Qwen3-ForcedAligner-0.6B
modelscope download --model Qwen/Qwen3-8B --local_dir /path/to/Qwen3-8B
```

### Auxiliary models (offline worker; all under gitignored `pretrained_models/`)

Paths are hardcoded in `offline_worker.py` / `utils.py`. Keep these directory names when you download.

| Use | Source | Local path |
|---|---|---|
| VAD | `silero_vad.onnx` from [FireRedASR-AED-L](https://github.com/FireRedTeam/FireRedASR) | `pretrained_models/FireRedASR-AED-L/silero_vad.onnx` |
| Speaker diarization | [iic/speech_campplus_speaker-diarization_common](https://www.modelscope.cn/models/iic/speech_campplus_speaker-diarization_common) | `pretrained_models/damo/speech_campplus_speaker-diarization_common` |
| Speaker verification | [iic/speech_campplus_sv_zh-cn_16k-common](https://www.modelscope.cn/models/iic/speech_campplus_sv_zh-cn_16k-common) | `pretrained_models/damo/speech_campplus_sv_zh-cn_16k-common` |
| Language ID | [iic/SenseVoiceSmall](https://www.modelscope.cn/models/iic/SenseVoiceSmall) | `pretrained_models/SenseVoiceSmall` |
| Gender recognition | [alefiury/wav2vec2-large-xlsr-53-gender-recognition-librispeech](https://huggingface.co/alefiury/wav2vec2-large-xlsr-53-gender-recognition-librispeech) | `pretrained_models/alefiury/wav2vec2-large-xlsr-53-gender-recognition-librispeech` |
| Punctuation fallback (optional) | [iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch](https://www.modelscope.cn/models/iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch) | `pretrained_models/iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch` |

```bash
modelscope download --model iic/SenseVoiceSmall --local_dir pretrained_models/SenseVoiceSmall
modelscope download --model iic/speech_campplus_speaker-diarization_common \
  --local_dir pretrained_models/damo/speech_campplus_speaker-diarization_common
modelscope download --model iic/speech_campplus_sv_zh-cn_16k-common \
  --local_dir pretrained_models/damo/speech_campplus_sv_zh-cn_16k-common
modelscope download --model iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch \
  --local_dir pretrained_models/iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch

# Gender model (Hugging Face; use a mirror if needed)
huggingface-cli download alefiury/wav2vec2-large-xlsr-53-gender-recognition-librispeech \
  --local-dir pretrained_models/alefiury/wav2vec2-large-xlsr-53-gender-recognition-librispeech
```

VAD: copy `silero_vad.onnx` from the FireRedASR `FireRedASR-AED-L` weights to the path above. If the file is missing, the worker skips ONNX VAD.

### llama-server (punctuation)

`run_punct.sh` runs `./llama-server` at the repo root (gitignored). Build or download a binary from [llama.cpp](https://github.com/ggml-org/llama.cpp), place it at the repo root, and make it executable:

```bash
git clone https://github.com/ggml-org/llama.cpp.git
cd llama.cpp
cmake -B build -DGGML_CUDA=ON          # NVIDIA; use GGML_HIP=ON for ROCm/DCU
cmake --build build --config Release -t llama-server
cp build/bin/llama-server /path/to/this-repo/
chmod +x /path/to/this-repo/llama-server
```

Put the punctuation GGUF at `PUNCT_MODEL` (default `./Qwen3_Merge-596M-F16.gguf`). It must serve llama.cpp `/v1/chat/completions`.

### Subtitle fonts (not in git)

On startup, `run_gateway.sh` copies these files from the repo root into `~/.local/share/fonts/` (missing files are skipped):

| File | ASS font name (default) | Notes |
|---|---|---|
| `msyh.ttf` | Microsoft YaHei | CJK. **Microsoft YaHei is proprietary — obtain it legally; do not commit it.** |
| `NotoSansThai-Regular.ttf` | Noto Sans Thai | Thai; [SIL OFL](https://fonts.google.com/noto/specimen/Noto+Sans+Thai) |

You can use an open alternative such as [Noto Sans CJK](https://github.com/notofonts/noto-cjk). After installing system fonts:

```bash
export SUBTITLE_CJK_FONTNAME="Noto Sans CJK SC"
export SUBTITLE_THAI_FONTNAME="Noto Sans Thai"
```

Then run `fc-cache -fv` and check names with `fc-list :lang=zh family` so they match the env vars.

## Quick start

1. Copy config files (gitignored — do not commit):

```bash
cp .env.example .env.local
cp smtp.env.example smtp.env   # only if you need email verification codes
```

2. Fill in model paths and punctuation / LLM URLs in `.env.local`. Secrets, proxies, OAuth, and admin usernames go there too.

3. Start remote GPU services (they can run on another machine):

```bash
export GPU_ID=0
bash run_punct.sh          # http://<punct-ip>:8080/v1/chat/completions
export LLM_MODEL_PATH=/path/to/Qwen3-8B
bash run_llm.sh            # http://<llm-ip>:8081/v1
```

4. Start the gateway. Startup scripts load `.env.local` and `smtp.env`; **variables already `export`ed in the shell take precedence**.

```bash
export LLM_URL=http://<llm-ip>:8081/v1
export PUNCT_URL=http://<punct-ip>:8080/v1/chat/completions
export QWEN3_OFFLINE_MODEL_PATH=/path/to/Qwen3-ASR-1.7B
export GATEWAY_PUBLIC_URL=http://<gateway-ip>:7860
# export ENABLE_FRONTEND_BUILD=0   # if frontend/dist is already built
bash run_gateway.sh
```

Content safety is off by default. To enable it, place `dict/*.dict` yourself (see [`dict/README.md`](dict/README.md); lists are not in git).

## Configuration

| Variable | Used by | Description |
|---|---|---|
| `PUNCT_URL` | Gateway, offline worker | Full punctuation service URL |
| `WORKER_URL` | Gateway | Offline worker base URL; default `http://127.0.0.1:7001` |
| `RUN_LOCAL_OFFLINE_WORKER` | Startup script | Default `1`: start a local worker with the gateway |
| `OFFLINE_WORKER_PORT` | Startup script, offline worker | Default `7001` |
| `QWEN3_OFFLINE_MODEL_PATH` | Offline worker | Qwen3 offline ASR model path |
| `WORKER_EVENT_MODE` | Gateway | `pull` (default) / `push` / `hybrid` |
| `GATEWAY_PUBLIC_URL` | Gateway | URL the worker uses to fetch audio/video |
| `GATEWAY_CALLBACK_URL` | Gateway, offline worker | Worker callback URL; defaults to `GATEWAY_PUBLIC_URL` |
| `FORCED_ALIGNER_MODEL_PATH` | Gateway | Qwen3-ForcedAligner path |
| `INTERNAL_WORKER_TOKEN` | Gateway + worker | Optional internal token |
| `ENABLE_CONTENT_SAFETY` | Gateway | `0` off (default), `1` on |
| `ENABLE_FRONTEND_BUILD` | Startup script | Default `1`; `0` requires an existing `frontend/dist` |
| `FRONTEND_BUILD_INSTALL_DEPS` | Startup script | `1` runs `npm install` before build |
| `LLM_URL` | Gateway | External vLLM base URL |
| `EMBED_URL` | Gateway | Optional embedding service |
| `ADMIN_USERNAMES` | Gateway | Admin usernames, comma-separated |
| `SCNET_OAUTH_*` | Gateway | Optional OAuth; put in `.env.local` |
| `SMTP_*` | Gateway | Verification email; put in `smtp.env` |
| `AUTH_EMAIL_DEV_MODE` | Gateway | `1` logs codes without SMTP (do not use in production) |
| `BILI_META_LLM_URL` / `BILI_COVER_IMAGE_URL` | Gateway | Optional Bilibili publish copy and cover generation |
| `YOUTUBE_DOWNLOAD_API_URL` / `YOUTUBE_DOWNLOAD_API_TOKEN` | Gateway | Optional self-hosted YouTube downloader (recommended; see `youtube_config.example.json`) |
| `PUNCT_MODEL` | Punctuation script | Punctuation GGUF path; default `./Qwen3_Merge-596M-F16.gguf` |
| `SUBTITLE_CJK_FONTNAME` | Gateway | Burn-in CJK font name; default `Microsoft YaHei` |
| `SUBTITLE_THAI_FONTNAME` | Gateway | Burn-in Thai font name; default `Noto Sans Thai` |

Use comma-separated lists for multiple instances: `PUNCT_URLS`, `WORKER_URLS`. Full template: [`.env.example`](.env.example).

For Bilibili / YouTube / Douyin cookies or accounts, copy the matching `*_config.example.json` to a local file (gitignored).

**YouTube download:** prefer a separate download service and set `YOUTUBE_DOWNLOAD_API_URL` plus `YOUTUBE_DOWNLOAD_API_TOKEN` (or put them in `youtube_config.json`). Without those, the gateway falls back to local yt-dlp.

## Frontend

See [`frontend/README.md`](frontend/README.md). The gateway serves `frontend/dist` at `/` by default.

## Benchmarks

```bash
python benchmarks/concurrency_benchmark.py offline --concurrency 6 --requests 18
```

Details: [`benchmarks/README.md`](benchmarks/README.md).

## License

Original code in this repository is [Apache License 2.0](LICENSE), same as FireRedASR, Qwen3-ASR, and related upstream projects, so they can be combined more easily.

Using a model does **not** mean this repo vendors the upstream project. Download weights and `fireredasr2s/` yourself and follow each project's LICENSE / model card. Third-party attribution: [NOTICE](NOTICE).

## Acknowledgements

- [FireRedTeam/FireRedASR](https://github.com/FireRedTeam/FireRedASR) and [FireRedASR2S](https://github.com/FireRedTeam/FireRedASR2S)
- [QwenLM/Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) (including ForcedAligner and `qwen-asr`)
- [Qwen3](https://huggingface.co/Qwen/Qwen3-8B) / [Qwen3-Embedding](https://huggingface.co/Qwen/Qwen3-Embedding-8B)
- [FunAudioLLM/SenseVoice](https://github.com/FunAudioLLM/SenseVoice), [FunASR CAM++](https://www.modelscope.cn/models/iic/speech_campplus_speaker-diarization_common)
- [llama.cpp](https://github.com/ggml-org/llama.cpp)
