# OpenCaptioner

[English](README.md) | **中文**

离线语音转写服务：上传音视频或粘贴 B 站 / YouTube / 抖音链接，完成识别、说话人分离、字幕对齐与翻译，并提供摘要与基于转写的问答。

本仓库是服务端与 Web 前端，**不包含模型权重**。中英粤默认走 [FireRedASR2S](https://github.com/FireRedTeam/FireRedASR2S)，其它语种与歌曲内容走 [Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR)。

**试用地址：** [https://www.scnet.cn/ui/aihub/agent/zhenghong/asr](https://www.scnet.cn/ui/aihub/agent/zhenghong/asr)（超算互联网 AIHub，使用 SCNet 账号登录）

### 成片示例

OpenCaptioner 烧录双语字幕后的样例视频：

<video src="docs/demo-subtitled.mp4" poster="docs/demo-poster.jpg" width="720" controls playsinline></video>

[![OpenCaptioner 字幕成片示例](docs/demo-poster.jpg)](docs/demo-subtitled.mp4)

[下载 demo.mp4](docs/demo-subtitled.mp4)

## 功能

- 文件上传与链接解析下载（哔哩哔哩 / YouTube / 抖音）
- 离线 ASR、标点、说话人统计
- 字幕编辑、时间对齐、烧录成片
- 视频摘要与 RAG 问答（可选 embedding 服务）
- 本地账号或可选 SCNet OAuth 登录
- 可选内容审核（词表由部署方自行放置，不随仓库分发）

## 架构

| 角色 | 脚本 | 说明 |
|---|---|---|
| 网关 + 本机离线 ASR | `run_gateway.sh` | `gateway_server.py` + 本机 `offline_worker.py` |
| 标点 | `run_punct.sh` | `llama-server` |
| 对话 / 翻译 LLM | `run_llm.sh` | vLLM + Qwen3 |
| Embedding（可选） | `run_embed.sh` | RAG 检索；未部署时自动降级 |
| 独立离线 Worker（可选） | `run_offline_worker.sh` | 需要扩容时再拆出去 |

```
浏览器 ──► 网关 :7860 ──► 本机或远程 offline worker
                 │
                 ├─► 标点服务
                 ├─► LLM（摘要 / 翻译 / 审核）
                 └─► Embedding（可选）
```

## 环境要求

- Python 3.10+、Node.js 18+（构建前端）、FFmpeg、`ffprobe`
- NVIDIA 或兼容 GPU（DCU 环境可参考 `install.sh`）
- 本机放置 FireRedASR2S 代码、各模型目录、`llama-server` 与字幕字体（均不入库，见下文）

### Python 依赖

```bash
pip install -r requirements.txt
# 按本机 CUDA / ROCm 版本另行安装 PyTorch
pip install vllm   # 仅运行 run_llm.sh / run_embed.sh 的机器需要
```

`install.sh` 面向特定 DCU 环境（含私有 onnxruntime wheel），通用 Linux 请用上面的 pip 安装，并自行安装 `ffmpeg`、`libsndfile` 等系统库。

### 前端

```bash
cd frontend && npm install && npm run build
```

开发时 `cd frontend && npm run dev`，默认 `http://127.0.0.1:5173`，已代理到网关 `:7860`。

## 模型与第三方代码（需自行下载）

| 用途 | 来源 | 许可 | 本地约定 |
|---|---|---|---|
| 中英粤 ASR | [FireRedASR2S](https://github.com/FireRedTeam/FireRedASR2S) | Apache-2.0 | 克隆为仓库根目录下的 `fireredasr2s/`，权重放在 `fireredasr2s/pretrained_models/FireRedASR2-AED` |
| 多语种 / 歌曲 ASR | [Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B) | Apache-2.0 | `QWEN3_OFFLINE_MODEL_PATH` |
| 字幕强制对齐 | [Qwen3-ForcedAligner-0.6B](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B) | Apache-2.0 | `FORCED_ALIGNER_MODEL_PATH` |
| 对话 / 翻译 | [Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B) | Apache-2.0 | `LLM_MODEL_PATH` |
| RAG Embedding（可选） | [Qwen3-Embedding-8B](https://huggingface.co/Qwen/Qwen3-Embedding-8B) | Apache-2.0 | `EMBED_MODEL_PATH` |
| 标点 GGUF | 本地文件（`PUNCT_MODEL`） | 以该文件来源为准 | 默认 `./Qwen3_Merge-596M-F16.gguf`，需兼容 llama.cpp OpenAI Chat API |

示例（中国大陆可用 ModelScope）：

```bash
git clone https://github.com/FireRedTeam/FireRedASR2S.git fireredasr2s

pip install -U modelscope
modelscope download --model Qwen/Qwen3-ASR-1.7B --local_dir /path/to/Qwen3-ASR-1.7B
modelscope download --model Qwen/Qwen3-ForcedAligner-0.6B --local_dir /path/to/Qwen3-ForcedAligner-0.6B
modelscope download --model Qwen/Qwen3-8B --local_dir /path/to/Qwen3-8B
```

### 附属模型（离线 Worker，均放在已 gitignore 的 `pretrained_models/`）

路径写死在 `offline_worker.py` / `utils.py` 中，下载时请保持下列目录名。

| 用途 | 来源 | 本地路径 |
|---|---|---|
| VAD | [FireRedASR-AED-L](https://github.com/FireRedTeam/FireRedASR) 包内 `silero_vad.onnx` | `pretrained_models/FireRedASR-AED-L/silero_vad.onnx` |
| 说话人分离 | [iic/speech_campplus_speaker-diarization_common](https://www.modelscope.cn/models/iic/speech_campplus_speaker-diarization_common) | `pretrained_models/damo/speech_campplus_speaker-diarization_common` |
| 说话人确认 | [iic/speech_campplus_sv_zh-cn_16k-common](https://www.modelscope.cn/models/iic/speech_campplus_sv_zh-cn_16k-common) | `pretrained_models/damo/speech_campplus_sv_zh-cn_16k-common` |
| 语种检测 | [iic/SenseVoiceSmall](https://www.modelscope.cn/models/iic/SenseVoiceSmall) | `pretrained_models/SenseVoiceSmall` |
| 性别识别 | [alefiury/wav2vec2-large-xlsr-53-gender-recognition-librispeech](https://huggingface.co/alefiury/wav2vec2-large-xlsr-53-gender-recognition-librispeech) | `pretrained_models/alefiury/wav2vec2-large-xlsr-53-gender-recognition-librispeech` |
| 标点回退（可选） | [iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch](https://www.modelscope.cn/models/iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch) | `pretrained_models/iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch` |

```bash
modelscope download --model iic/SenseVoiceSmall --local_dir pretrained_models/SenseVoiceSmall
modelscope download --model iic/speech_campplus_speaker-diarization_common \
  --local_dir pretrained_models/damo/speech_campplus_speaker-diarization_common
modelscope download --model iic/speech_campplus_sv_zh-cn_16k-common \
  --local_dir pretrained_models/damo/speech_campplus_sv_zh-cn_16k-common
modelscope download --model iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch \
  --local_dir pretrained_models/iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch

# 性别识别（Hugging Face；国内可走镜像）
huggingface-cli download alefiury/wav2vec2-large-xlsr-53-gender-recognition-librispeech \
  --local-dir pretrained_models/alefiury/wav2vec2-large-xlsr-53-gender-recognition-librispeech
```

VAD：从 FireRedASR 的 `FireRedASR-AED-L` 权重中取出 `silero_vad.onnx` 放到上表路径。该文件缺失时 worker 会跳过 ONNX VAD。

### llama-server（标点服务）

`run_punct.sh` 调用仓库根目录下的 `./llama-server`（已 gitignore）。请从 [llama.cpp](https://github.com/ggml-org/llama.cpp) 自行编译或下载预编译包，放到本仓库根目录并赋予执行权限：

```bash
git clone https://github.com/ggml-org/llama.cpp.git
cd llama.cpp
cmake -B build -DGGML_CUDA=ON          # NVIDIA；ROCm/DCU 可改 GGML_HIP=ON
cmake --build build --config Release -t llama-server
cp build/bin/llama-server /path/to/this-repo/
chmod +x /path/to/this-repo/llama-server
```

同时将标点 GGUF 放到 `PUNCT_MODEL`（默认 `./Qwen3_Merge-596M-F16.gguf`）。该权重需能走 llama.cpp 的 `/v1/chat/completions`。

### 字幕字体（不入库）

`run_gateway.sh` 启动时会尝试把仓库根目录下的字体复制到 `~/.local/share/fonts/`（文件不存在则跳过）：

| 文件 | 字体名（ASS 默认） | 说明 |
|---|---|---|
| `msyh.ttf` | Microsoft YaHei | 中日韩；**微软雅黑为专有字体，请自行合法获取，不要提交 git** |
| `NotoSansThai-Regular.ttf` | Noto Sans Thai | 泰文；[SIL OFL](https://fonts.google.com/noto/specimen/Noto+Sans+Thai)，可自行下载 |

可用开源替代（如 [Noto Sans CJK](https://github.com/notofonts/noto-cjk)），安装到系统字体目录后设置：

```bash
export SUBTITLE_CJK_FONTNAME="Noto Sans CJK SC"
export SUBTITLE_THAI_FONTNAME="Noto Sans Thai"
```

改完后执行 `fc-cache -fv`，并用 `fc-list :lang=zh family` 确认名称与环境变量一致。

## 快速开始

1. 复制配置（已 gitignore，不要提交）：

```bash
cp .env.example .env.local
cp smtp.env.example smtp.env   # 仅在需要邮箱验证码时
```

2. 在 `.env.local` 填写模型路径，以及标点 / LLM 地址。密钥、代理、OAuth、管理员用户名也都写在这里。

3. 启动远程 GPU 服务（可换机器）：

```bash
export GPU_ID=0
bash run_punct.sh          # http://<punct-ip>:8080/v1/chat/completions
export LLM_MODEL_PATH=/path/to/Qwen3-8B
bash run_llm.sh            # http://<llm-ip>:8081/v1
```

4. 启动网关。启动脚本会加载 `.env.local` 与 `smtp.env`；**命令行里已 `export` 的变量优先**。

```bash
export LLM_URL=http://<llm-ip>:8081/v1
export PUNCT_URL=http://<punct-ip>:8080/v1/chat/completions
export QWEN3_OFFLINE_MODEL_PATH=/path/to/Qwen3-ASR-1.7B
export GATEWAY_PUBLIC_URL=http://<gateway-ip>:7860
# export ENABLE_FRONTEND_BUILD=0   # 若 frontend/dist 已构建
bash run_gateway.sh
```

内容审核默认关闭。若开启，请按 [`dict/README.md`](dict/README.md) 自行放置 `dict/*.dict`（词表不入库）。

## 配置

| 变量 | 使用方 | 说明 |
|---|---|---|
| `PUNCT_URL` | 网关、离线 worker | 标点服务完整 URL |
| `WORKER_URL` | 网关 | 离线 worker 基址；默认 `http://127.0.0.1:7001` |
| `RUN_LOCAL_OFFLINE_WORKER` | 启动脚本 | 默认 `1`：随 gateway 启动本机 worker |
| `OFFLINE_WORKER_PORT` | 启动脚本、离线 worker | 默认 `7001` |
| `QWEN3_OFFLINE_MODEL_PATH` | 离线 worker | Qwen3 离线 ASR 模型路径 |
| `WORKER_EVENT_MODE` | 网关 | `pull`（默认）/ `push` / `hybrid` |
| `GATEWAY_PUBLIC_URL` | 网关 | worker 拉取音视频用的地址 |
| `GATEWAY_CALLBACK_URL` | 网关、离线 worker | worker 回调地址，默认同 `GATEWAY_PUBLIC_URL` |
| `FORCED_ALIGNER_MODEL_PATH` | 网关 | Qwen3-ForcedAligner 路径 |
| `INTERNAL_WORKER_TOKEN` | 网关 + worker | 可选内部令牌 |
| `ENABLE_CONTENT_SAFETY` | 网关 | `0` 关闭（默认），`1` 开启 |
| `ENABLE_FRONTEND_BUILD` | 启动脚本 | 默认 `1`；`0` 时要求已有 `frontend/dist` |
| `FRONTEND_BUILD_INSTALL_DEPS` | 启动脚本 | `1` 时构建前 `npm install` |
| `LLM_URL` | 网关 | 外部 vLLM 基址 |
| `EMBED_URL` | 网关 | 可选 embedding 服务 |
| `ADMIN_USERNAMES` | 网关 | 管理员用户名，逗号分隔 |
| `SCNET_OAUTH_*` | 网关 | 可选 OAuth，写入 `.env.local` |
| `SMTP_*` | 网关 | 验证码邮件，写入 `smtp.env` |
| `AUTH_EMAIL_DEV_MODE` | 网关 | `1` 时无 SMTP 仅打日志（勿用于生产） |
| `BILI_META_LLM_URL` / `BILI_COVER_IMAGE_URL` | 网关 | 可选；B 站投稿文案与封面生成 |
| `YOUTUBE_DOWNLOAD_API_URL` / `YOUTUBE_DOWNLOAD_API_TOKEN` | 网关 | 可选；自建 YouTube 下载服务（推荐，见 `youtube_config.example.json`） |
| `PUNCT_MODEL` | 标点脚本 | 标点 GGUF 路径，默认 `./Qwen3_Merge-596M-F16.gguf` |
| `SUBTITLE_CJK_FONTNAME` | 网关 | 烧录字幕 CJK 字体名，默认 `Microsoft YaHei` |
| `SUBTITLE_THAI_FONTNAME` | 网关 | 烧录字幕泰文字体名，默认 `Noto Sans Thai` |

多实例可用逗号分隔：`PUNCT_URLS`、`WORKER_URLS`。完整模板见 [`.env.example`](.env.example)。

哔哩哔哩 / YouTube / 抖音账号或 Cookie 使用对应 `*_config.example.json` 复制为本地配置（已 gitignore）。

**YouTube 下载**：推荐部署独立的下载服务，在本项目配置 `YOUTUBE_DOWNLOAD_API_URL` 与 `YOUTUBE_DOWNLOAD_API_TOKEN`（或写入 `youtube_config.json`）。未配置时回退本机 yt-dlp。

## 前端

详见 [`frontend/README.md`](frontend/README.md)。网关 `/` 默认托管 `frontend/dist`。

## 压测

```bash
python benchmarks/concurrency_benchmark.py offline --concurrency 6 --requests 18
```

说明见 [`benchmarks/README.md`](benchmarks/README.md)。

## 许可证

本仓库**原创代码**采用 [Apache License 2.0](LICENSE)，与 FireRedASR、Qwen3-ASR 等上游一致，便于组合使用。

使用模型**不等于**把上游项目整份拷进本仓库：权重与 `fireredasr2s/` 需自行获取，并遵守各项目的 LICENSE / model card。第三方归属见 [NOTICE](NOTICE)。

## 致谢

- [FireRedTeam/FireRedASR](https://github.com/FireRedTeam/FireRedASR) 与 [FireRedASR2S](https://github.com/FireRedTeam/FireRedASR2S)
- [QwenLM/Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR)（含 ForcedAligner 与 `qwen-asr`）
- [Qwen3](https://huggingface.co/Qwen/Qwen3-8B) / [Qwen3-Embedding](https://huggingface.co/Qwen/Qwen3-Embedding-8B)
- [FunAudioLLM/SenseVoice](https://github.com/FunAudioLLM/SenseVoice)、[FunASR CAM++](https://www.modelscope.cn/models/iic/speech_campplus_speaker-diarization_common)
- [llama.cpp](https://github.com/ggml-org/llama.cpp)
