#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YouTube 视频下载器 (YouTube video downloader)

供网关统一路由复用（接口与 bilibili_downloader.py 对齐）：
  - looks_like_youtube(text)          粗判是否 YouTube 链接
  - resolve_target_format(is_admin)   返回清晰度 format 表达式（本机 yt-dlp 模式）
  - fetch_basic_meta(url)             解析标题/时长等，不下载
  - download_to_path(url, out_path)   下载并落盘为 mp4

下载策略（优先级）：
  1. 配置了 remote_api_url + remote_api_token（或环境变量
     YOUTUBE_DOWNLOAD_API_URL / YOUTUBE_DOWNLOAD_API_TOKEN）时，调用自建下载服务。
  2. 否则回退本机 yt-dlp（需 Node、可连通 googlevideo 的代理等）。

用法 (Usage):
    python3 youtube_downloader.py <视频链接> [-o 输出目录] [--max-height 720]

配置：
  - youtube_config.json（复制 youtube_config.example.json，已 gitignore）
  - 或环境变量 YOUTUBE_DOWNLOAD_API_* / YTDLP_PROXY 等（见 .env.example）
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from contextlib import contextmanager

import requests
import yt_dlp

DEFAULT_MAX_HEIGHT = 0

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "youtube_config.json"
)

YT_URL_RE = re.compile(
    r"(?:youtube\.com/|youtu\.be/|youtube-nocookie\.com/)",
    re.IGNORECASE,
)
YT_ID_RE = re.compile(
    r"(?:v=|youtu\.be/|/embed/|/shorts/|/live/)([A-Za-z0-9_-]{11})",
    re.IGNORECASE,
)


class YoutubeError(Exception):
    """业务/网络错误的统一异常类型（对标 bilibili_downloader.BiliError）。"""


def looks_like_youtube(text):
    """粗判输入是否为 YouTube 视频链接，供上层做入口路由。"""
    if not text:
        return False
    return bool(YT_URL_RE.search(str(text)))


def load_config(path=None):
    """读取 youtube_config.json；不存在或损坏时返回空字典。"""
    path = path or os.environ.get("YTDLP_CONFIG_PATH") or DEFAULT_CONFIG_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _config_int(cfg, key, default):
    try:
        return int(cfg.get(key))
    except (TypeError, ValueError):
        return default


def config_remote_download(config=None):
    """返回 (api_url, bearer_token)；未配置时为空字符串。"""
    cfg = config if config is not None else load_config()
    url = str(
        os.environ.get("YOUTUBE_DOWNLOAD_API_URL")
        or cfg.get("remote_api_url")
        or ""
    ).strip()
    token = str(
        os.environ.get("YOUTUBE_DOWNLOAD_API_TOKEN")
        or cfg.get("remote_api_token")
        or ""
    ).strip()
    return url, token


def remote_download_configured(config=None):
    """是否已配置远程 YouTube 下载服务。"""
    url, token = config_remote_download(config)
    return bool(url and token)


def config_remote_timeout(config=None):
    """远程下载读超时（秒）。"""
    cfg = config if config is not None else load_config()
    env = str(os.environ.get("YOUTUBE_DOWNLOAD_API_TIMEOUT") or "").strip()
    if env:
        try:
            return max(30, int(env))
        except ValueError:
            pass
    return max(30, _config_int(cfg, "remote_timeout_sec", 600))


def config_remote_proxy(config=None):
    """访问自建下载服务时使用的 HTTP 代理（内网节点通常需走出口代理）。"""
    cfg = config if config is not None else load_config()
    explicit = str(
        os.environ.get("YOUTUBE_DOWNLOAD_API_PROXY")
        or cfg.get("remote_api_proxy")
        or ""
    ).strip()
    if explicit:
        return explicit
    for key in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return None


def config_proxy(config=None):
    """本机 yt-dlp 模式下的代理地址。"""
    env = str(os.environ.get("YTDLP_PROXY") or "").strip()
    if env:
        return env
    for key in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    cfg = config if config is not None else load_config()
    value = str(cfg.get("proxy") or "").strip()
    return value or None


def config_cookies_file(config=None):
    cfg = config if config is not None else load_config()
    value = str(cfg.get("cookies_file") or "").strip()
    return value if value and os.path.exists(value) else None


def extract_video_id(url):
    """从 YouTube URL 提取 11 位 video id。"""
    if not url:
        return None
    m = YT_ID_RE.search(str(url))
    return m.group(1) if m else None


def _requests_session_for_remote(config=None):
    """远程/oEmbed 请求：显式配置代理，避免 trust_env 与残留 SMTP 代理混乱。"""
    session = requests.Session()
    session.trust_env = False
    proxy = config_remote_proxy(config)
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


@contextmanager
def _ipv4_only_dns():
    """本机 IPv6 出口不通时，强制 requests/urllib3 只解析 A 记录。"""
    import socket
    import urllib3.util.connection as urllib3_connection

    orig = urllib3_connection.allowed_gai_family
    urllib3_connection.allowed_gai_family = lambda: socket.AF_INET
    try:
        yield
    finally:
        urllib3_connection.allowed_gai_family = orig


def _fetch_meta_oembed(url):
    """通过 YouTube oEmbed 获取标题/缩略图（远程下载模式，无需 yt-dlp）。"""
    vid = extract_video_id(url) or "youtube_video"
    oembed_url = (
        "https://www.youtube.com/oembed?"
        + urllib.parse.urlencode({"url": url, "format": "json"})
    )
    try:
        with _ipv4_only_dns():
            resp = _requests_session_for_remote().get(oembed_url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        # 内网/代理环境 oEmbed 可能不可达；用 video id 兜底，下载仍走远程服务。
        return {
            "id": vid,
            "title": vid,
            "duration": None,
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        }
    except ValueError as e:
        raise YoutubeError("解析 YouTube 视频信息失败：oEmbed 返回无效 JSON。") from e
    title = str(data.get("title") or vid).strip() or vid
    return {
        "id": vid,
        "title": title,
        "duration": None,
        "thumbnail": data.get("thumbnail_url"),
    }


def _resolve_node_path(config=None):
    cfg = config if config is not None else load_config()
    candidates = [
        str(os.environ.get("YTDLP_NODE_PATH") or "").strip(),
        str(cfg.get("node_path") or "").strip(),
        shutil.which("node") or "",
    ]
    nvm_glob = os.path.expanduser("~/.nvm/versions/node/*/bin/node")
    candidates.extend(sorted(glob.glob(nvm_glob), reverse=True))
    seen = set()
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _format_for_height(max_height):
    if max_height and max_height > 0:
        return (
            f"bv*[height<={max_height}]+ba/"
            f"b[height<={max_height}]/"
            f"bv*+ba/b"
        )
    return "bv*+ba/b"


def resolve_target_format(is_admin=False, config=None):
    cfg = config if config is not None else load_config()
    height = DEFAULT_MAX_HEIGHT
    for key in ("max_height", "admin_max_height", "guest_max_height"):
        if key in cfg:
            height = _config_int(cfg, key, DEFAULT_MAX_HEIGHT)
            break
    return _format_for_height(height)


def _base_ydl_opts(config=None, proxy=None):
    cfg = config if config is not None else load_config()
    opts = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 15,
        "fragment_retries": 15,
        "file_access_retries": 5,
        "socket_timeout": 120,
        "http_chunk_size": 1024 * 1024,
        "concurrent_fragment_downloads": 1,
        "extractor_args": {
            "youtube": {
                "player_client": ["web_embedded", "visionos", "-android_vr"],
            }
        },
    }
    node_path = _resolve_node_path(cfg)
    if node_path:
        opts["js_runtimes"] = {"node": {"path": node_path}}
    proxy = proxy if proxy is not None else config_proxy(cfg)
    if proxy:
        opts["proxy"] = proxy
    else:
        opts["source_address"] = "0.0.0.0"
    cookies = config_cookies_file(cfg)
    if cookies:
        opts["cookiefile"] = cookies
    return opts


def fetch_basic_meta(url, config=None, proxy=None):
    """解析视频标题/时长等，不触发下载。"""
    cfg = config if config is not None else load_config()
    if remote_download_configured(cfg):
        return _fetch_meta_oembed(url)

    opts = _base_ydl_opts(config=cfg, proxy=proxy)
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        raise YoutubeError(f"解析 YouTube 视频信息失败: {e}") from e
    if not isinstance(info, dict):
        raise YoutubeError("解析 YouTube 视频信息失败：未返回有效数据。")
    if info.get("_type") == "playlist" and info.get("entries"):
        info = info["entries"][0] or {}
    vid = info.get("id") or extract_video_id(url) or "youtube_video"
    return {
        "id": vid,
        "title": info.get("title") or vid,
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
    }


def _guess_download_suffix(content_type, content_disposition):
    ct = str(content_type or "").lower()
    if "mp4" in ct:
        return ".mp4"
    if "webm" in ct:
        return ".webm"
    if "matroska" in ct or "mkv" in ct:
        return ".mkv"
    cd = str(content_disposition or "")
    m = re.search(r"filename\*=utf-8''([^;\s]+)", cd, re.IGNORECASE)
    if m:
        name = urllib.parse.unquote(m.group(1))
        ext = os.path.splitext(name)[1]
        if ext:
            return ext.lower()
    m = re.search(r'filename="?([^";]+)"?', cd, re.IGNORECASE)
    if m:
        ext = os.path.splitext(m.group(1))[1]
        if ext:
            return ext.lower()
    return ".webm"


def _materialize_mp4(src_path, out_path):
    """将下载产物转为网关期望的 mp4 路径。"""
    src_path = os.path.abspath(src_path)
    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    if src_path.lower().endswith(".mp4"):
        if src_path != out_path:
            os.replace(src_path, out_path)
        return out_path

    tmp_out = out_path + ".remux.mp4"
    src_lower = src_path.lower()
    if src_lower.endswith(".webm"):
        transcode_cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", src_path, "-c:v", "libx264", "-c:a", "aac", "-f", "mp4", tmp_out,
        ]
        subprocess.run(transcode_cmd, check=True, timeout=1200)
    else:
        copy_cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", src_path, "-c", "copy", "-movflags", "+faststart", "-f", "mp4", tmp_out,
        ]
        try:
            subprocess.run(copy_cmd, check=True, timeout=600)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            transcode_cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", src_path, "-c:v", "libx264", "-c:a", "aac", "-f", "mp4", tmp_out,
            ]
            subprocess.run(transcode_cmd, check=True, timeout=1200)
    os.replace(tmp_out, out_path)
    if src_path != out_path and os.path.exists(src_path):
        os.remove(src_path)
    return out_path


def _download_via_remote_api(url, out_path, progress_cb=None, config=None):
    """调用自建 YouTube 下载服务，落盘为 mp4。"""
    cfg = config if config is not None else load_config()
    api_url, token = config_remote_download(cfg)
    if not api_url or not token:
        raise YoutubeError("未配置 YouTube 远程下载服务（remote_api_url / remote_api_token）。")

    timeout_sec = config_remote_timeout(cfg)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
    }

    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    base, _ = os.path.splitext(os.path.abspath(out_path))
    tmp_path = base + ".downloading"

    try:
        session = _requests_session_for_remote(cfg)
        with _ipv4_only_dns(), session.post(
            api_url,
            json={"url": url},
            headers=headers,
            stream=True,
            timeout=(30, timeout_sec),
        ) as resp:
            if resp.status_code >= 400:
                detail = (resp.text or "").strip()[:300]
                raise YoutubeError(
                    f"YouTube 下载服务返回 HTTP {resp.status_code}"
                    + (f": {detail}" if detail else "")
                )

            suffix = _guess_download_suffix(
                resp.headers.get("Content-Type"),
                resp.headers.get("Content-Disposition"),
            )
            tmp_path = tmp_path + suffix
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb is not None and total > 0:
                        try:
                            progress_cb("video", done, total)
                        except Exception:
                            pass
    except requests.RequestException as e:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise YoutubeError(f"调用 YouTube 下载服务失败: {e}") from e

    if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) <= 0:
        raise YoutubeError("YouTube 下载服务未返回有效视频数据。")

    try:
        return _materialize_mp4(tmp_path, out_path)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise YoutubeError(f"转换 YouTube 视频为 mp4 失败: {e}") from e


def download_to_path(
    url,
    out_path,
    target_format=None,
    proxy=None,
    progress_cb=None,
    info=None,
    config=None,
):
    """下载并合并为指定的 out_path（mp4）。返回 out_path。"""
    cfg = config if config is not None else load_config()
    if remote_download_configured(cfg):
        return _download_via_remote_api(
            url, out_path, progress_cb=progress_cb, config=cfg
        )

    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    base, _ = os.path.splitext(os.path.abspath(out_path))

    opts = _base_ydl_opts(config=cfg, proxy=proxy)
    opts.update({
        "format": target_format or resolve_target_format(config=cfg),
        "merge_output_format": "mp4",
        "outtmpl": base + ".%(ext)s",
        "overwrites": True,
    })

    if progress_cb is not None:
        def _hook(d):
            if d.get("status") != "downloading":
                return
            done = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            try:
                progress_cb("video", int(done), int(total))
            except Exception:
                pass
        opts["progress_hooks"] = [_hook]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        raise YoutubeError(f"下载 YouTube 视频失败: {e}") from e

    produced = base + ".mp4"
    if os.path.abspath(produced) != os.path.abspath(out_path):
        if os.path.exists(produced):
            os.replace(produced, out_path)
    if not os.path.exists(out_path):
        raise YoutubeError("下载完成但未找到输出文件。")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="YouTube 视频下载器")
    parser.add_argument("url", help="视频链接")
    parser.add_argument("-o", "--out", default="downloads", help="输出目录 (默认: downloads)")
    parser.add_argument(
        "--max-height", type=int, default=None,
        help="清晰度高度上限 (仅本机 yt-dlp 模式)",
    )
    parser.add_argument("--proxy", default=None, help="代理地址 (仅本机 yt-dlp 模式)")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    try:
        meta = fetch_basic_meta(args.url, proxy=args.proxy)
        safe = re.sub(r'[\\/:*?"<>|\n\r\t]', "_", meta["title"]).strip()[:120] or meta["id"]
        out_mp4 = os.path.join(args.out, f"{safe}.mp4")
        fmt = _format_for_height(args.max_height) if args.max_height else resolve_target_format()
        mode = "远程服务" if remote_download_configured() else "本机 yt-dlp"
        print(f"[1/2] 解析 ({mode}): {meta['title']} (时长 {meta.get('duration')}s)")
        print("[2/2] 下载中 ...")
        download_to_path(args.url, out_mp4, target_format=fmt, proxy=args.proxy)
        size_mb = os.path.getsize(out_mp4) / 1048576
        print(f"\n✅ 完成: {out_mp4}  ({size_mb:.1f} MB)")
    except YoutubeError as e:
        print(f"\n❌ 出错: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
