#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音视频下载器 (Douyin video downloader)

基于 yt-dlp，接口与 youtube_downloader.py / bilibili_downloader.py 对齐，
供网关统一路由复用：
  - looks_like_douyin(text)           粗判是否抖音链接
  - normalize_url(url)                短链/分享页 -> douyin.com/video/<id>
  - resolve_target_format(is_admin)   返回清晰度 format 表达式（默认最高清晰度）
  - fetch_basic_meta(url)             仅解析标题/时长等，不下载
  - download_to_path(url, out_path)   下载并合并为 mp4

用法 (Usage):
    python3 douyin_downloader.py <视频链接> [-o 输出目录] [--max-height 720]

说明:
    - 本机直连抖音不可达时，复用 https_proxy / YTDLP_PROXY / 配置中的 proxy（与 YouTube 一致）。
    - v.douyin.com / iesdouyin.com 分享短链会先跟随跳转再交给 yt-dlp。
    - 部分视频需新鲜 cookies（s_v_web_id）；可在 douyin_config.json 配置 cookies_file。
"""

import argparse
import json
import os
import re
import sys

import requests
import yt_dlp

# 默认不限制高度，由 yt-dlp 选可用最高清晰度（0 / 负数表示不限制）。
DEFAULT_MAX_HEIGHT = 0

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "douyin_config.json"
)

# 识别抖音主站 / 短链 / 分享页。
DY_URL_RE = re.compile(
    r"(?:(?:www\.)?douyin\.com/|(?:www\.)?iesdouyin\.com/|v\.douyin\.com/)",
    re.IGNORECASE,
)

# 从规范页或跳转后的 URL 中抽取 aweme / video id。
DY_VIDEO_ID_RE = re.compile(
    r"(?:douyin\.com/video/|iesdouyin\.com/(?:share/)?video/|aweme/detail/|modal_id=)(\d+)",
    re.IGNORECASE,
)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class DouyinError(Exception):
    """业务/网络错误的统一异常类型（对标 YoutubeError / BiliError）。"""


def looks_like_douyin(text):
    """粗判输入是否为抖音视频链接，供上层做入口路由。"""
    if not text:
        return False
    return bool(DY_URL_RE.search(str(text)))


def load_config(path=None):
    """读取 douyin_config.json，返回配置字典。

    文件不存在或格式损坏时返回空字典。可用环境变量 DOUYIN_CONFIG_PATH 覆盖路径。
    """
    path = path or os.environ.get("DOUYIN_CONFIG_PATH") or DEFAULT_CONFIG_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def config_cookies_file(config=None):
    """从配置取出 cookies 文件路径（应对反爬要求的 s_v_web_id），无则返回 None。"""
    cfg = config if config is not None else load_config()
    value = str(cfg.get("cookies_file") or "").strip()
    return value if value and os.path.exists(value) else None


def config_proxy(config=None):
    """从配置/环境变量取出代理地址。

    优先级：YTDLP_PROXY > https_proxy/HTTPS_PROXY/http_proxy/HTTP_PROXY > 配置 proxy。
    本部署环境直连抖音不通，需走与 YouTube 相同的出口代理。
    """
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


def _config_int(cfg, key, default):
    try:
        return int(cfg.get(key))
    except (TypeError, ValueError):
        return default


def _format_for_height(max_height):
    """构造 yt-dlp format 表达式：优先渐进式单文件，再回退分离音视频。"""
    if max_height and max_height > 0:
        return (
            f"best[height<={max_height}][protocol^=http][vcodec!*=none][acodec!*=none]/"
            f"best[height<={max_height}]/"
            f"bv*[height<={max_height}]+ba/"
            f"b[height<={max_height}]/"
            f"bv*+ba/b"
        )
    return (
        "best[protocol^=http][vcodec!*=none][acodec!*=none]/"
        "bv*+ba/b"
    )


def resolve_target_format(is_admin=False, config=None):
    """返回目标清晰度的 format 表达式；默认不限高度，选可用最高清晰度。

    is_admin 保留兼容旧调用方，不再区分身份。若配置里写了 max_height /
    guest_max_height / admin_max_height，则按该上限筛选。
    """
    cfg = config if config is not None else load_config()
    height = DEFAULT_MAX_HEIGHT
    for key in ("max_height", "admin_max_height", "guest_max_height"):
        if key in cfg:
            height = _config_int(cfg, key, DEFAULT_MAX_HEIGHT)
            break
    return _format_for_height(height)


def _extract_video_id(text):
    """从任意文本/URL 中提取抖音视频数字 id，找不到则返回 None。"""
    if not text:
        return None
    m = DY_VIDEO_ID_RE.search(str(text))
    return m.group(1) if m else None


def normalize_url(url, proxy=None):
    """将短链/分享页规范化为 https://www.douyin.com/video/<id>。

    若已是规范链接则直接返回；短链通过 HTTP 跟随跳转（走出口代理，与下载一致）。
    失败时抛出 DouyinError。
    """
    raw = (url or "").strip()
    if not raw:
        raise DouyinError("抖音链接为空。")

    vid = _extract_video_id(raw)
    if vid:
        return f"https://www.douyin.com/video/{vid}"

    if not looks_like_douyin(raw):
        raise DouyinError("不是有效的抖音视频链接。")

    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = "https://" + raw.lstrip("/")

    session = requests.Session()
    # 信任环境代理；若显式传入 proxy 则覆盖。
    proxy = proxy if proxy is not None else config_proxy()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    headers = {
        "User-Agent": _USER_AGENT,
        "Referer": "https://www.douyin.com/",
    }
    try:
        resp = session.get(raw, headers=headers, allow_redirects=True, timeout=20)
    except requests.RequestException as e:
        raise DouyinError(f"解析抖音短链失败: {e}") from e

    candidates = [resp.url or ""]
    if resp.text:
        candidates.append(resp.text[:200000])
    for text in candidates:
        vid = _extract_video_id(text)
        if vid:
            return f"https://www.douyin.com/video/{vid}"

    raise DouyinError(
        "无法从该抖音链接解析出视频 ID，请使用完整视频页链接或检查链接是否有效。"
    )


def _base_ydl_opts(config=None, proxy=None):
    """构造 YoutubeDL 通用参数：走出口代理；可选 cookies。"""
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
    }
    proxy = proxy if proxy is not None else config_proxy(cfg)
    if proxy:
        # 有 HTTP 代理时不要绑 source_address，否则 CONNECT 隧道易超时。
        opts["proxy"] = proxy
    else:
        opts["source_address"] = "0.0.0.0"
    cookies = config_cookies_file(cfg)
    if cookies:
        opts["cookiefile"] = cookies
    return opts


def fetch_basic_meta(url, config=None, proxy=None):
    """仅解析视频标题/时长等基础信息，不触发下载。

    返回 {"id", "title", "duration", "thumbnail"}；失败时抛出 DouyinError。
    """
    canonical = normalize_url(url, proxy=proxy)
    opts = _base_ydl_opts(config=config, proxy=proxy)
    opts["skip_download"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(canonical, download=False)
    except DouyinError:
        raise
    except Exception as e:
        msg = str(e)
        if "cookies" in msg.lower() or "s_v_web_id" in msg.lower():
            raise DouyinError(
                "解析抖音视频失败：需要有效 cookies（请在 douyin_config.json 配置 cookies_file）。"
            ) from e
        raise DouyinError(f"解析抖音视频信息失败: {e}") from e
    if not isinstance(info, dict):
        raise DouyinError("解析抖音视频信息失败：未返回有效数据。")
    if info.get("_type") == "playlist" and info.get("entries"):
        info = info["entries"][0] or {}
    vid = info.get("id") or "douyin_video"
    return {
        "id": vid,
        "title": info.get("title") or vid,
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
    }


def download_to_path(
    url,
    out_path,
    target_format=None,
    proxy=None,
    progress_cb=None,
    info=None,
    config=None,
):
    """下载并合并为指定的 out_path（mp4）。返回 out_path。

    progress_cb: 可选 fn(stage, done_bytes, total_bytes)，stage ∈ {"video", "audio"}，
                 与 bilibili/youtube 下载器回调签名对齐。
    info: 目前不使用，仅为与其它下载器保持同构签名。
    """
    del info  # 同构签名占位
    canonical = normalize_url(url, proxy=proxy)
    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    base, _ = os.path.splitext(os.path.abspath(out_path))

    opts = _base_ydl_opts(config=config, proxy=proxy)
    opts.update({
        "format": target_format or resolve_target_format(),
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
            ydl.download([canonical])
    except Exception as e:
        msg = str(e)
        if "cookies" in msg.lower() or "s_v_web_id" in msg.lower():
            raise DouyinError(
                "下载抖音视频失败：需要有效 cookies（请在 douyin_config.json 配置 cookies_file）。"
            ) from e
        raise DouyinError(f"下载抖音视频失败: {e}") from e

    produced = base + ".mp4"
    if os.path.abspath(produced) != os.path.abspath(out_path):
        if os.path.exists(produced):
            os.replace(produced, out_path)
    if not os.path.exists(out_path):
        raise DouyinError("下载完成但未找到输出文件。")
    return out_path


def main():
    parser = argparse.ArgumentParser(description="抖音视频下载器")
    parser.add_argument("url", help="视频链接（支持短链）")
    parser.add_argument("-o", "--out", default="downloads", help="输出目录 (默认: downloads)")
    parser.add_argument(
        "--max-height", type=int, default=None,
        help="清晰度高度上限 (如 720, 1080; 默认不限制，选最高)",
    )
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    try:
        meta = fetch_basic_meta(args.url)
        safe = re.sub(r'[\\/:*?"<>|\n\r\t]', "_", meta["title"]).strip()[:120] or meta["id"]
        out_mp4 = os.path.join(args.out, f"{safe}.mp4")
        fmt = _format_for_height(args.max_height) if args.max_height else resolve_target_format()
        print(f"[1/2] 解析: {meta['title']} (时长 {meta.get('duration')}s)")
        print("[2/2] 下载中 ...")
        download_to_path(args.url, out_mp4, target_format=fmt)
        size_mb = os.path.getsize(out_mp4) / 1048576
        print(f"\n✅ 完成: {out_mp4}  ({size_mb:.1f} MB)")
    except DouyinError as e:
        print(f"\n❌ 出错: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
