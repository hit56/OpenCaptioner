#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
哔哩哔哩视频下载器 (Bilibili video downloader)

用法 (Usage):
    python3 bilibili_downloader.py <视频链接或BV号> [-o 输出目录] [-q 目标清晰度]

示例 (Examples):
    python3 bilibili_downloader.py BV1GmmmBkEU3
    python3 bilibili_downloader.py "https://www.bilibili.com/video/BV1GmmmBkEU3/?xxx"
    python3 bilibili_downloader.py BV1GmmmBkEU3 -o ./downloads -q 80

说明:
    - 使用 WBI 签名调用官方 playurl 接口获取 DASH 流。
    - 视频、音频分别下载后用 ffmpeg 合并为 mp4。
    - 未登录时最高一般为 1080P(qn=80)；更高清晰度需提供带 SESSDATA 的 cookie。
    - 可通过环境变量 BILI_SESSDATA 传入登录 cookie 以解锁更高画质/会员视频。
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

import requests

# 默认请求最高清晰度档（8K）；实际拿到的流仍受账号权限与片源限制，
# _pick_best 在目标档不可用时会自动回退到可用最高档。
DEFAULT_QN = 127

# 下载配置文件（含 SESSDATA / 账号信息），默认与本脚本同目录。
DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "bilibili_config.json"
)


def load_config(path=None):
    """读取 bilibili_config.json，返回配置字典。

    文件不存在或格式损坏时返回空字典，调用方据此退化为未登录下载。
    可用环境变量 BILI_CONFIG_PATH 覆盖配置路径。
    """
    path = path or os.environ.get("BILI_CONFIG_PATH") or DEFAULT_CONFIG_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _config_dir(config=None):
    """配置文件所在目录，用于解析相对 cookie_file 路径。"""
    cfg_path = os.environ.get("BILI_CONFIG_PATH") or DEFAULT_CONFIG_PATH
    return os.path.dirname(os.path.abspath(cfg_path))


def resolve_cookie_file_path(config=None):
    """解析 cookie_file 绝对路径；未配置或文件不存在时返回 None。"""
    cfg = config if config is not None else load_config()
    raw = str(cfg.get("cookie_file") or os.environ.get("BILI_COOKIE_FILE") or "").strip()
    if not raw:
        return None
    path = raw if os.path.isabs(raw) else os.path.join(_config_dir(cfg), raw)
    return path if os.path.isfile(path) else None


def parse_netscape_cookie_file(path):
    """解析 Netscape / curl cookie 文件，返回 {name: value}。

    兼容常见浏览器扩展导出的 ``# Netscape HTTP Cookie File`` 格式。
    同名 cookie 以后出现的为准。
    """
    cookies = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    # 部分导出器用 #HttpOnly_ 前缀标记 HttpOnly
                    if line.startswith("#HttpOnly_"):
                        line = line[len("#HttpOnly_") :]
                    else:
                        continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                name = parts[5].strip()
                value = parts[6].strip()
                if name:
                    cookies[name] = value
    except OSError:
        return {}
    return cookies


def load_cookie_map(config=None):
    """汇总登录 cookie：cookie_file + cookie 字符串 + 独立字段（后者覆盖前者）。

    优先级（高 → 低）：
      1. 配置里显式 sessdata / bili_jct / dede_user_id
      2. cookie 请求头字符串
      3. cookie_file（Netscape）
    """
    cfg = config if config is not None else load_config()
    merged = {}

    cookie_path = resolve_cookie_file_path(cfg)
    if cookie_path:
        merged.update(parse_netscape_cookie_file(cookie_path))

    raw_cookie = str(cfg.get("cookie") or "").strip()
    if raw_cookie:
        for part in raw_cookie.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k:
                merged[k] = v

    sessdata = str(cfg.get("sessdata") or "").strip()
    if sessdata:
        merged["SESSDATA"] = sessdata
    bili_jct = str(cfg.get("bili_jct") or "").strip()
    if bili_jct:
        merged["bili_jct"] = bili_jct
    dede = str(cfg.get("dede_user_id") or cfg.get("DedeUserID") or "").strip()
    if dede:
        merged["DedeUserID"] = dede

    env_sess = str(os.environ.get("BILI_SESSDATA") or "").strip()
    if env_sess:
        merged["SESSDATA"] = env_sess
    env_jct = str(os.environ.get("BILI_JCT") or "").strip()
    if env_jct:
        merged["bili_jct"] = env_jct

    return merged


def config_sessdata(config=None):
    """从配置中取出可用的 SESSDATA（去空白），无则返回 None。

    会回退到 cookie_file / cookie 字符串 / 环境变量 BILI_SESSDATA。
    """
    cookies = load_cookie_map(config)
    value = str(cookies.get("SESSDATA") or "").strip()
    return value or None


def config_bili_jct(config=None):
    """从配置中取出 bili_jct，支持 cookie_file / cookie 字符串 / 独立字段。"""
    cookies = load_cookie_map(config)
    value = str(cookies.get("bili_jct") or "").strip()
    return value or None


def config_dede_user_id(config=None):
    cookies = load_cookie_map(config)
    value = str(cookies.get("DedeUserID") or "").strip()
    return value or None


def _config_int(cfg, key, default):
    try:
        return int(cfg.get(key))
    except (TypeError, ValueError):
        return default


def resolve_target_qn(is_admin=False, config=None):
    """返回目标清晰度代码；默认请求最高档。

    is_admin 保留兼容旧调用方，不再区分身份。若配置里写了 qn /
    admin_qn / guest_qn，则按该值请求。
    """
    cfg = config if config is not None else load_config()
    for key in ("qn", "admin_qn", "guest_qn"):
        if key in cfg:
            return _config_int(cfg, key, DEFAULT_QN)
    return DEFAULT_QN


# WBI 签名用的固定重排表
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

# 常见清晰度代码 -> 描述
QN_DESC = {
    127: "8K 超高清",
    126: "杜比视界",
    125: "HDR 真彩",
    120: "4K 超清",
    116: "1080P60 高帧率",
    112: "1080P+ 高码率",
    80: "1080P 高清",
    64: "720P 高清",
    32: "480P 清晰",
    16: "360P 流畅",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com",
    "Origin": "https://www.bilibili.com",
}


class BiliError(Exception):
    """业务/网络错误的统一异常类型。"""


def make_session(sessdata=None, cookie_map=None):
    s = requests.Session()
    s.headers.update(HEADERS)
    # buvid3 是很多接口的必要 cookie，随便请求首页即可拿到；这里直接给个默认值兜底。
    s.cookies.set("buvid3", "0", domain=".bilibili.com")
    if cookie_map:
        for name, value in cookie_map.items():
            if name and value is not None and str(value) != "":
                s.cookies.set(str(name), str(value), domain=".bilibili.com")
    if sessdata:
        s.cookies.set("SESSDATA", sessdata, domain=".bilibili.com")
    elif not cookie_map:
        # 未显式传入时，自动从配置 / cookie_file 加载
        auto = load_cookie_map()
        for name, value in auto.items():
            if name and value is not None and str(value) != "":
                s.cookies.set(str(name), str(value), domain=".bilibili.com")
    return s


def extract_bvid(text):
    """从 URL 或原始输入中提取 BV 号。"""
    m = re.search(r"(BV[0-9A-Za-z]{10})", text)
    if not m:
        raise BiliError(f"无法从输入中解析出 BV 号: {text!r}")
    return m.group(1)


def get_wbi_keys(session):
    """获取 WBI 签名所需的 img_key / sub_key。"""
    r = session.get("https://api.bilibili.com/x/web-interface/nav", timeout=20)
    r.raise_for_status()
    data = r.json()
    # 即使未登录 (code=-101) 也会返回 wbi_img 字段
    wbi = data["data"]["wbi_img"]
    img_key = wbi["img_url"].rsplit("/", 1)[-1].split(".")[0]
    sub_key = wbi["sub_url"].rsplit("/", 1)[-1].split(".")[0]
    return img_key, sub_key


def _mixin_key(img_key, sub_key):
    s = img_key + sub_key
    return "".join(s[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def sign_wbi(params, img_key, sub_key):
    """对参数做 WBI 签名，返回带 w_rid 与 wts 的新字典。"""
    mixin = _mixin_key(img_key, sub_key)
    params = dict(params)
    params["wts"] = int(time.time())
    # 按 key 排序，并过滤掉 value 中的 !'()* 字符
    filtered = {
        k: "".join(c for c in str(v) if c not in "!'()*")
        for k, v in sorted(params.items())
    }
    query = urllib.parse.urlencode(filtered)
    w_rid = hashlib.md5((query + mixin).encode()).hexdigest()
    params["w_rid"] = w_rid
    return params


def get_video_info(session, bvid):
    """获取视频基本信息（标题、cid、分P 等）。"""
    r = session.get(
        "https://api.bilibili.com/x/web-interface/view",
        params={"bvid": bvid},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if data["code"] != 0:
        raise BiliError(f"获取视频信息失败: {data.get('message')} (code={data['code']})")
    return data["data"]


def get_play_url(session, bvid, cid, img_key, sub_key):
    """获取 DASH 播放地址。"""
    params = {
        "bvid": bvid,
        "cid": cid,
        "qn": 127,          # 请求最高清晰度，实际返回受权限限制
        "fnval": 4048,      # DASH 全能标志位
        "fnver": 0,
        "fourk": 1,
    }
    params = sign_wbi(params, img_key, sub_key)
    r = session.get(
        "https://api.bilibili.com/x/player/wbi/playurl",
        params=params,
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if data["code"] != 0:
        raise BiliError(f"获取播放地址失败: {data.get('message')} (code={data['code']})")
    return data["data"]


def _sanitize_filename(name):
    name = re.sub(r'[\\/:*?"<>|\n\r\t]', "_", name)
    return name.strip()[:120] or "video"


def _download_stream(session, url, dest, label, progress_cb=None):
    """流式下载单个文件并显示进度。

    progress_cb: 可选回调 fn(done_bytes, total_bytes)，用于对接外部进度上报；
                 未提供时退化为在 stdout 打印进度（CLI 场景）。
    """
    # DASH 流的下载同样需要 Referer，session 已带上。
    with session.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0))
        done = 0
        chunk = 1024 * 256
        with open(dest, "wb") as f:
            for block in r.iter_content(chunk_size=chunk):
                if not block:
                    continue
                f.write(block)
                done += len(block)
                if progress_cb is not None:
                    progress_cb(done, total)
                    continue
                if total:
                    pct = done * 100 / total
                    sys.stdout.write(
                        f"\r  {label}: {pct:5.1f}%  "
                        f"({done/1048576:.1f}/{total/1048576:.1f} MB)"
                    )
                else:
                    sys.stdout.write(f"\r  {label}: {done/1048576:.1f} MB")
                sys.stdout.flush()
    if progress_cb is None:
        sys.stdout.write("\n")


def _pick_best(streams, target_qn=None):
    """从流列表中挑选目标/最佳清晰度。"""
    if not streams:
        return None
    if target_qn is not None:
        cand = [s for s in streams if s.get("id") == target_qn]
        if cand:
            return max(cand, key=lambda s: s.get("bandwidth", 0))
    # 否则取 id (清晰度) 最高、码率最高的
    return max(streams, key=lambda s: (s.get("id", 0), s.get("bandwidth", 0)))


def _stream_url(s):
    return s.get("baseUrl") or s.get("base_url") or (s.get("backupUrl") or [None])[0]


def merge_av(video_path, audio_path, out_path):
    """用 ffmpeg 合并音视频（直接复制流，不重新编码）。"""
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c", "copy",
        "-loglevel", "error",
        out_path,
    ]
    subprocess.run(cmd, check=True)


BILI_URL_RE = re.compile(r"(?:BV[0-9A-Za-z]{10})|bilibili\.com/video/|b23\.tv/", re.IGNORECASE)


def looks_like_bilibili(text):
    """粗判输入是否为 B 站视频链接 / BV 号，供上层做入口路由。"""
    if not text:
        return False
    return bool(BILI_URL_RE.search(str(text)))


def fetch_basic_meta(url_or_bvid, sessdata=None):
    """仅解析视频标题等基础信息，不触发下载。

    返回 {"bvid", "title", "cid", "duration", "cover"}；失败时抛出 BiliError。
    供上层在真正下载前拿到标题用于命名 / 内容审核。
    """
    bvid = extract_bvid(url_or_bvid)
    session = make_session(
        sessdata or os.environ.get("BILI_SESSDATA") or config_sessdata()
    )
    info = get_video_info(session, bvid)
    return {
        "bvid": bvid,
        "title": info.get("title") or bvid,
        "cid": info.get("cid"),
        "duration": info.get("duration"),
        "cover": info.get("pic"),
    }


def download_to_path(
    url_or_bvid,
    out_path,
    target_qn=None,
    sessdata=None,
    progress_cb=None,
    info=None,
):
    """下载并合并为指定的 out_path（mp4）。返回 out_path。

    progress_cb: 可选 fn(stage, done_bytes, total_bytes)，stage ∈ {"video", "audio"}，
                 用于外部（如网关 SSE）上报下载进度。
    info: 可复用 fetch_basic_meta 之外的 get_video_info 结果，避免重复请求。
    """
    bvid = extract_bvid(url_or_bvid)
    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    session = make_session(
        sessdata or os.environ.get("BILI_SESSDATA") or config_sessdata()
    )

    if info is None:
        info = get_video_info(session, bvid)
    cid = info["cid"]

    img_key, sub_key = get_wbi_keys(session)
    play = get_play_url(session, bvid, cid, img_key, sub_key)
    dash = play.get("dash")
    if not dash:
        raise BiliError("该视频未返回 DASH 流（可能是番剧/试看/需要更高权限）。")

    video = _pick_best(dash.get("video", []), target_qn)
    audio = _pick_best(dash.get("audio", []))
    if video is None or audio is None:
        raise BiliError("未找到可用的视频或音频流。")

    base, _ = os.path.splitext(out_path)
    tmp_v = f"{base}.video.m4s"
    tmp_a = f"{base}.audio.m4s"

    def _cb(stage):
        if progress_cb is None:
            return None
        return lambda done, total: progress_cb(stage, done, total)

    try:
        _download_stream(session, _stream_url(video), tmp_v, "视频", progress_cb=_cb("video"))
        _download_stream(session, _stream_url(audio), tmp_a, "音频", progress_cb=_cb("audio"))
        merge_av(tmp_v, tmp_a, out_path)
    finally:
        for tmp in (tmp_v, tmp_a):
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
    return out_path


def download(url_or_bvid, out_dir="downloads", target_qn=None, sessdata=None):
    bvid = extract_bvid(url_or_bvid)
    os.makedirs(out_dir, exist_ok=True)
    session = make_session(
        sessdata or os.environ.get("BILI_SESSDATA") or config_sessdata()
    )

    print(f"[1/5] 解析视频: {bvid}")
    info = get_video_info(session, bvid)
    title = info["title"]
    cid = info["cid"]
    print(f"      标题: {title}")
    print(f"      cid : {cid}  分P数: {info.get('videos', 1)}")

    print("[2/5] 获取 WBI 签名密钥 ...")
    img_key, sub_key = get_wbi_keys(session)

    print("[3/5] 获取播放地址 ...")
    play = get_play_url(session, bvid, cid, img_key, sub_key)
    dash = play.get("dash")
    if not dash:
        raise BiliError("该视频未返回 DASH 流（可能是番剧/试看/需要更高权限）。")

    video = _pick_best(dash.get("video", []), target_qn)
    audio = _pick_best(dash.get("audio", []))
    if video is None or audio is None:
        raise BiliError("未找到可用的视频或音频流。")

    qn = video.get("id")
    print(f"      选择清晰度: {qn} ({QN_DESC.get(qn, '未知')})")

    safe = _sanitize_filename(title)
    tmp_v = os.path.join(out_dir, f"{safe}.video.m4s")
    tmp_a = os.path.join(out_dir, f"{safe}.audio.m4s")
    out_mp4 = os.path.join(out_dir, f"{safe}.mp4")

    print("[4/5] 下载音视频流 ...")
    _download_stream(session, _stream_url(video), tmp_v, "视频")
    _download_stream(session, _stream_url(audio), tmp_a, "音频")

    print("[5/5] 合并为 MP4 ...")
    merge_av(tmp_v, tmp_a, out_mp4)
    os.remove(tmp_v)
    os.remove(tmp_a)

    size_mb = os.path.getsize(out_mp4) / 1048576
    print(f"\n✅ 完成: {out_mp4}  ({size_mb:.1f} MB)")
    return out_mp4


def main():
    parser = argparse.ArgumentParser(description="哔哩哔哩视频下载器")
    parser.add_argument("url", help="视频链接或 BV 号")
    parser.add_argument("-o", "--out", default="downloads", help="输出目录 (默认: downloads)")
    parser.add_argument(
        "-q", "--qn", type=int, default=None,
        help="目标清晰度代码 (如 80=1080P, 64=720P; 默认自动选最高)",
    )
    parser.add_argument("--sessdata", default=None, help="登录 cookie SESSDATA（解锁高画质）")
    args = parser.parse_args()

    try:
        download(args.url, out_dir=args.out, target_qn=args.qn, sessdata=args.sessdata)
    except (BiliError, requests.RequestException) as e:
        print(f"\n❌ 出错: {e}", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"\n❌ ffmpeg 合并失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
