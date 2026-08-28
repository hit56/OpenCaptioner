#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
哔哩哔哩视频投稿 (Cookie 登录投稿)

基于网页端 member.bilibili.com 上传链路：
  preupload → 分片 PUT → 合片 → 封面 → /x/vu/web/add

依赖 bilibili_config.json：推荐通过 cookie_file 指向 Netscape cookie
（如 member.bilibili.com_cookies.txt），也可直接填 sessdata + bili_jct。
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
from typing import Callable, Optional

import requests

import bilibili_downloader
import utils

ProgressCallback = Optional[Callable[[str, float], None]]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://member.bilibili.com/",
    "Origin": "https://member.bilibili.com",
}

# 日常分区；可通过配置 publish_tid 覆盖。常见分区：
# 21=日常, 122=野生技能协会, 208=科技·计算机技术, 17=单机游戏, 65=网络游戏
DEFAULT_TID = 21
DEFAULT_TAGS = "#字幕 #双语字幕 #AI字幕"
MAX_TITLE_LEN = 80
CHUNK_FALLBACK = 4 * 1024 * 1024  # 4 MiB


class BiliUploadError(Exception):
    """投稿流程业务/网络错误。"""


def config_bili_jct(config=None) -> Optional[str]:
    """取出 CSRF token (bili_jct)。支持独立字段 / cookie 字符串 / cookie_file。"""
    return bilibili_downloader.config_bili_jct(config)


def config_dede_user_id(config=None) -> Optional[str]:
    return bilibili_downloader.config_dede_user_id(config)


def publish_defaults(config=None) -> dict:
    """返回投稿默认分区 / 标签 / 版权等。"""
    cfg = config if config is not None else bilibili_downloader.load_config()
    try:
        tid = int(cfg.get("publish_tid") or DEFAULT_TID)
    except (TypeError, ValueError):
        tid = DEFAULT_TID
    tags = str(cfg.get("publish_tags") or DEFAULT_TAGS).strip() or DEFAULT_TAGS
    try:
        copyright_ = int(cfg.get("publish_copyright") or 1)
    except (TypeError, ValueError):
        copyright_ = 1
    if copyright_ not in (1, 2):
        copyright_ = 1
    no_reprint = 1 if str(cfg.get("publish_no_reprint", "1")).strip() not in ("0", "false", "False") else 0
    source = str(cfg.get("publish_source") or "").strip()
    return {
        "tid": tid,
        "tags": tags,
        "copyright": copyright_,
        "no_reprint": no_reprint,
        "source": source,
    }


def sanitize_title(title: str, fallback: str = "字幕视频") -> str:
    text = re.sub(r"\s+", " ", (title or "").strip())
    text = re.sub(r'[\\/:*?"<>|]', "", text)
    if not text:
        text = fallback
    return text[:MAX_TITLE_LEN]


def make_publish_session(sessdata=None, bili_jct=None, dede_user_id=None, cookie=None, cookie_map=None):
    """构造带登录 cookie 的 Session。"""
    s = requests.Session()
    s.headers.update(HEADERS)

    merged = {}
    if cookie_map:
        merged.update(cookie_map)
    else:
        merged.update(bilibili_downloader.load_cookie_map())

    raw_cookie = (cookie or "").strip()
    if raw_cookie:
        for part in raw_cookie.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k:
                merged[k] = v

    if sessdata:
        merged["SESSDATA"] = sessdata
    if bili_jct:
        merged["bili_jct"] = bili_jct
    if dede_user_id:
        merged["DedeUserID"] = str(dede_user_id)
    if not merged.get("buvid3"):
        merged["buvid3"] = "0"

    for name, value in merged.items():
        if name and value is not None and str(value) != "":
            s.cookies.set(str(name), str(value), domain=".bilibili.com")
    return s


def verify_login(session) -> dict:
    """校验登录态，返回 nav data；失败抛 BiliUploadError。"""
    r = session.get("https://api.bilibili.com/x/web-interface/nav", timeout=20)
    r.raise_for_status()
    payload = r.json()
    if payload.get("code") != 0 or not (payload.get("data") or {}).get("isLogin"):
        raise BiliUploadError(
            "B 站登录态无效：请在 bilibili_config.json 中配置有效的 sessdata 与 bili_jct。"
        )
    return payload["data"]


def _report(progress: ProgressCallback, message: str, ratio: float = 0.0):
    if progress:
        try:
            progress(message, max(0.0, min(1.0, float(ratio))))
        except Exception:
            pass


def extract_cover_jpeg(video_path: str, out_path: str, seek_sec: float = 1.0) -> str:
    """用 ffmpeg 截取一帧作为封面 JPEG。"""
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        str(max(0.0, seek_sec)),
        "-i",
        video_path,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        out_path,
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if proc.returncode != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) < 64:
        # 再试片头
        cmd[4] = "0"
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if proc.returncode != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) < 64:
        err = (proc.stderr or b"").decode("utf-8", errors="ignore")[-400:]
        raise BiliUploadError(f"无法从视频截取封面: {err}")
    return out_path


def upload_cover(session, bili_jct: str, cover_path: str) -> str:
    """上传封面，返回可用的 cover URL（通常以 // 开头）。"""
    with open(cover_path, "rb") as f:
        raw = f.read()
    payload = {
        "cover": "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"),
        "csrf": bili_jct,
    }
    r = session.post(
        "https://member.bilibili.com/x/vu/web/cover/up",
        data=payload,
        timeout=60,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise BiliUploadError(f"封面上传失败: {data.get('message') or data}")
    url = ((data.get("data") or {}).get("url") or "").strip()
    if not url:
        raise BiliUploadError("封面上传成功但未返回 URL")
    # 部分接口返回 https://...，投稿接口更偏好 //...
    if url.startswith("https:"):
        url = url[6:]
    elif url.startswith("http:"):
        url = url[5:]
    return url


def _upload_url(preupload: dict) -> str:
    endpoint = preupload.get("endpoint") or ""
    upos_uri = preupload.get("upos_uri") or ""
    if not endpoint or not upos_uri:
        raise BiliUploadError(f"preupload 响应缺少 endpoint/upos_uri: {preupload}")
    path = upos_uri.replace("upos://", "", 1)
    if endpoint.startswith("//"):
        return f"https:{endpoint}/{path}"
    if endpoint.startswith("http"):
        return f"{endpoint.rstrip('/')}/{path}"
    return f"https://{endpoint}/{path}"


def upload_video_file(
    session,
    video_path: str,
    progress: ProgressCallback = None,
) -> dict:
    """
    上传视频文件到 UPOS，返回 {"filename", "cid"} 供投稿使用。
    """
    if not os.path.isfile(video_path):
        raise BiliUploadError(f"视频文件不存在: {video_path}")

    file_name = os.path.basename(video_path)
    file_size = os.path.getsize(video_path)
    if file_size <= 0:
        raise BiliUploadError("视频文件为空，无法投稿")

    _report(progress, "正在申请上传凭证…", 0.05)
    params = {
        "name": file_name,
        "size": file_size,
        "r": "upos",
        "profile": "ugcupos/bup",
        "ssl": "0",
        "version": "2.14.0",
        "build": "2140000",
        "webVersion": "2.14.0",
    }
    r = session.get("https://member.bilibili.com/preupload", params=params, timeout=30)
    r.raise_for_status()
    pre = r.json()
    if pre.get("OK") != 1 and pre.get("OK") != "1":
        raise BiliUploadError(f"preupload 失败: {pre}")

    auth = pre.get("auth")
    biz_id = pre.get("biz_id")
    chunk_size = int(pre.get("chunk_size") or CHUNK_FALLBACK)
    if not auth or biz_id is None:
        raise BiliUploadError(f"preupload 缺少 auth/biz_id: {pre}")

    upload_url = _upload_url(pre)
    upos_name = os.path.basename(str(pre.get("upos_uri") or ""))
    filename_id = os.path.splitext(upos_name)[0]
    if not filename_id:
        raise BiliUploadError(f"无法解析 upos 文件名: {pre.get('upos_uri')}")

    headers = {"X-Upos-Auth": auth}

    _report(progress, "正在初始化分片上传…", 0.08)
    init_r = session.post(
        upload_url,
        params={
            "uploads": "",
            "output": "json",
            "profile": "ugcupos/bup",
            "filesize": file_size,
            "partsize": chunk_size,
            "biz_id": biz_id,
        },
        headers=headers,
        timeout=60,
    )
    init_r.raise_for_status()
    init_data = init_r.json()
    upload_id = init_data.get("upload_id")
    if not upload_id:
        raise BiliUploadError(f"获取 upload_id 失败: {init_data}")

    total_chunks = max(1, math.ceil(file_size / chunk_size))
    parts = []
    with open(video_path, "rb") as f:
        for chunk_idx in range(total_chunks):
            start = chunk_idx * chunk_size
            f.seek(start)
            data = f.read(chunk_size)
            if not data:
                break
            end = start + len(data)
            part_number = chunk_idx + 1
            put_r = session.put(
                upload_url,
                params={
                    "partNumber": part_number,
                    "uploadId": upload_id,
                    "chunk": chunk_idx,
                    "chunks": total_chunks,
                    "size": len(data),
                    "start": start,
                    "end": end,
                    "total": file_size,
                },
                headers=headers,
                data=data,
                timeout=300,
            )
            if put_r.status_code >= 400:
                raise BiliUploadError(
                    f"分片上传失败 part={part_number}: HTTP {put_r.status_code} {put_r.text[:200]}"
                )
            parts.append({"partNumber": part_number, "eTag": "etag"})
            ratio = 0.1 + 0.7 * (part_number / total_chunks)
            _report(progress, f"正在上传视频分片 {part_number}/{total_chunks}…", ratio)

    _report(progress, "正在合并分片…", 0.82)
    complete_r = session.post(
        upload_url,
        params={
            "output": "json",
            "name": file_name,
            "profile": "ugcupos/bup",
            "uploadId": upload_id,
            "biz_id": biz_id,
        },
        headers={**headers, "Content-Type": "application/json"},
        data=json.dumps({"parts": parts}),
        timeout=120,
    )
    complete_r.raise_for_status()
    complete_data = complete_r.json()
    if complete_data.get("OK") != 1 and complete_data.get("OK") != "1":
        raise BiliUploadError(f"合片失败: {complete_data}")

    return {"filename": filename_id, "cid": biz_id}


def submit_archive(
    session,
    bili_jct: str,
    *,
    title: str,
    tid: int,
    tag: str,
    desc: str,
    cover: str,
    filename: str,
    cid,
    copyright: int = 1,
    source: str = "",
    no_reprint: int = 1,
) -> dict:
    """提交稿件，成功返回含 aid/bvid 的 data。"""
    title = sanitize_title(title)
    tag = _tags_for_bilibili_api(tag)
    desc = (desc or "").strip()
    if copyright == 2 and not source:
        raise BiliUploadError("转载稿件必须填写来源 source")

    body = {
        "copyright": copyright,
        "videos": [
            {
                "filename": filename,
                "title": title,
                "desc": "",
                "cid": cid,
            }
        ],
        "cover": cover,
        "title": title,
        "tid": tid,
        "tag": tag,
        "desc_format_id": 0,
        "desc": desc,
        "recreate": -1,
        "dynamic": "",
        "interactive": 0,
        "act_reserve_create": 0,
        "no_disturbance": 0,
        "no_reprint": no_reprint,
        "subtitle": {"open": 0, "lan": ""},
        "dolby": 0,
        "lossless_music": 0,
        "up_selection_reply": False,
        "up_close_reply": False,
        "up_close_danmu": False,
        "web_os": 1,
    }
    if copyright == 2:
        body["source"] = source

    url = f"https://member.bilibili.com/x/vu/web/add/v3?t={int(time.time() * 1000)}&csrf={urllib.parse.quote(bili_jct)}"
    r = session.post(
        url,
        headers={**HEADERS, "Content-Type": "application/json;charset=UTF-8"},
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        timeout=60,
    )
    # 部分环境 v3 不可用，回退旧接口
    if r.status_code >= 400:
        url = f"https://member.bilibili.com/x/vu/web/add?csrf={urllib.parse.quote(bili_jct)}"
        r = session.post(
            url,
            headers={**HEADERS, "Content-Type": "application/json;charset=UTF-8"},
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            timeout=60,
        )
    r.raise_for_status()
    payload = r.json()
    if payload.get("code") != 0:
        # 再试一次旧接口（若刚才走的是 v3）
        if "/add/v3" in url:
            url2 = f"https://member.bilibili.com/x/vu/web/add?csrf={urllib.parse.quote(bili_jct)}"
            r2 = session.post(
                url2,
                headers={**HEADERS, "Content-Type": "application/json;charset=UTF-8"},
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                timeout=60,
            )
            r2.raise_for_status()
            payload = r2.json()
        if payload.get("code") != 0:
            raise BiliUploadError(f"投稿提交失败: [{payload.get('code')}] {payload.get('message')}")
    data = payload.get("data") or {}
    if not data.get("bvid") and not data.get("aid"):
        raise BiliUploadError(f"投稿返回异常: {payload}")
    return data


def publish_video(
    video_path: str,
    *,
    title: str,
    desc: str = "",
    tags: Optional[str] = None,
    tid: Optional[int] = None,
    copyright: Optional[int] = None,
    source: Optional[str] = None,
    no_reprint: Optional[int] = None,
    progress: ProgressCallback = None,
) -> dict:
    """
    一键投稿入口。

    Returns:
        {
          "aid": int,
          "bvid": str,
          "url": "https://www.bilibili.com/video/BVxxx",
          "title": str,
        }
    """
    cfg = bilibili_downloader.load_config()
    cookie_map = bilibili_downloader.load_cookie_map(cfg)
    sessdata = cookie_map.get("SESSDATA") or os.environ.get("BILI_SESSDATA")
    bili_jct = cookie_map.get("bili_jct") or os.environ.get("BILI_JCT")
    dede = cookie_map.get("DedeUserID")
    cookie_path = bilibili_downloader.resolve_cookie_file_path(cfg)
    defaults = publish_defaults(cfg)

    if not sessdata:
        hint = (
            f"当前 cookie_file={cookie_path} 未解析到 SESSDATA"
            if cookie_path
            else "请在 bilibili_config.json 设置 cookie_file，或填写 sessdata / bili_jct"
        )
        raise BiliUploadError(f"未配置 B 站登录凭证：{hint}")
    if not bili_jct:
        hint = (
            f"当前 cookie_file={cookie_path} 未解析到 bili_jct"
            if cookie_path
            else "投稿需要 bili_jct，请使用含 bili_jct 的 Netscape cookie 文件或在配置中填写"
        )
        raise BiliUploadError(f"未配置 bili_jct：{hint}")

    tid = int(tid if tid is not None else defaults["tid"])
    tags = (tags if tags is not None else defaults["tags"]) or DEFAULT_TAGS
    copyright = int(copyright if copyright is not None else defaults["copyright"])
    source = source if source is not None else defaults["source"]
    no_reprint = int(no_reprint if no_reprint is not None else defaults["no_reprint"])
    title = sanitize_title(title)

    session = make_publish_session(
        sessdata=sessdata,
        bili_jct=bili_jct,
        dede_user_id=dede,
        cookie_map=cookie_map,
    )
    if not bili_jct:
        bili_jct = session.cookies.get("bili_jct")
    if not bili_jct:
        raise BiliUploadError("缺少 bili_jct，无法投稿")

    _report(progress, "正在校验 B 站登录态…", 0.02)
    verify_login(session)

    cover_path = None
    try:
        _report(progress, "正在生成封面…", 0.04)
        fd, cover_path = tempfile.mkstemp(prefix="bili_cover_", suffix=".jpg")
        os.close(fd)
        extract_cover_jpeg(video_path, cover_path)

        video_meta = upload_video_file(session, video_path, progress=progress)

        _report(progress, "正在上传封面…", 0.88)
        cover_url = upload_cover(session, bili_jct, cover_path)

        _report(progress, "正在提交稿件…", 0.94)
        result = submit_archive(
            session,
            bili_jct,
            title=title,
            tid=tid,
            tag=tags,
            desc=desc or "",
            cover=cover_url,
            filename=video_meta["filename"],
            cid=video_meta["cid"],
            copyright=copyright,
            source=source or "",
            no_reprint=no_reprint,
        )
    finally:
        if cover_path and os.path.isfile(cover_path):
            try:
                os.remove(cover_path)
            except OSError:
                pass

    bvid = result.get("bvid") or ""
    aid = result.get("aid")
    url = f"https://www.bilibili.com/video/{bvid}" if bvid else (
        f"https://www.bilibili.com/video/av{aid}" if aid else ""
    )
    _report(progress, "投稿成功", 1.0)
    return {
        "aid": aid,
        "bvid": bvid,
        "url": url,
        "title": title,
    }


# ---------------------------------------------------------------------------
# 投稿元数据（标题 / 简介 / 标签）LLM 生成
# ---------------------------------------------------------------------------

META_MAX_TRANSCRIPT_CHARS = 12000

_META_SYSTEM_PROMPT = """你是哔哩哔哩（B站）投稿文案助手。
根据视频转写文本，生成适合 B 站投稿的标题、简介与标签。
要求：
1. 只输出一个 JSON 对象，必须同时包含 title、desc、tags 三个字段；不要输出多个 JSON，不要 Markdown，不要其它说明文字。
2. 正确示例：{"title":"...","desc":"...","tags":"#标签1 #标签2 #标签3"}
3. title：吸引点击、信息准确，不超过 80 字，不要加书名号或引号包裹整句。
4. desc：2～5 句简介，概括内容亮点，不超过 500 字；可用 \\n 表示换行。
5. tags：3～8 个中文标签，每个以井号 # 开头、中间用空格分隔，例如「#科技 #教程 #字幕」。
6. 不要编造转写中不存在的事实；不要出现违法违规内容。"""


def resolve_meta_llm_chat_url() -> str:
    """投稿元数据专用 LLM；需设置 BILI_META_LLM_URL 或 BILI_PUBLISH_LLM_URL。"""
    raw = (
        os.environ.get("BILI_META_LLM_URL")
        or os.environ.get("BILI_PUBLISH_LLM_URL")
        or ""
    )
    raw = (raw or "").strip()
    if not raw:
        return ""
    return utils.normalize_llm_chat_url(raw)


def _post_llm_chat(
    chat_url: str,
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int = 600,
    temperature: float = 0.4,
    timeout: int = 90,
) -> str:
    """调用 OpenAI 兼容 chat/completions，返回 assistant content。"""
    model = os.environ.get("BILI_META_LLM_MODEL") or os.environ.get("LLM_MODEL") or "llm"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    r = requests.post(
        chat_url,
        headers={"Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    r.raise_for_status()
    body = r.json()
    return (
        (((body.get("choices") or [{}])[0].get("message") or {}).get("content"))
        or ""
    ).strip()


def _parse_meta_json(text: str) -> dict:
    """解析 LLM 返回；兼容单个 JSON，或多个各含部分字段的 JSON 片段。"""
    text = (text or "").strip()
    if not text:
        return {}

    def _as_dict(raw: str):
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    data = _as_dict(text)
    if data:
        return data

    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        data = _as_dict(m.group(1))
        if data:
            return data

    merged: dict = {}
    # 非贪婪匹配多个简单对象并合并（模型有时会拆成 3 个 JSON）
    for m in re.finditer(r"\{[^{}]*\}", text, re.DOTALL):
        part = _as_dict(m.group(0))
        if part:
            merged.update(part)
    if merged:
        return merged

    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        data = _as_dict(m.group(0))
        if data:
            return data
    return {}


def _split_meta_tag_parts(raw) -> list[str]:
    """从逗号 / 空格 / 井号混排文本中拆出标签词。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        text = " ".join(str(x).strip() for x in raw if str(x).strip())
    else:
        text = str(raw).strip()
    if not text:
        return []
    text = text.replace("，", " ").replace("、", " ").replace(",", " ")
    parts: list[str] = []
    for token in text.split():
        token = token.strip().lstrip("#").strip()
        if token:
            parts.append(token)
        if len(parts) >= 10:
            break
    return parts


def _normalize_meta_tags(raw) -> str:
    """展示/复制用：#音乐 #情感 #歌词。兼容旧的逗号分隔缓存。"""
    return " ".join(f"#{p}" for p in _split_meta_tag_parts(raw))


def _tags_for_bilibili_api(raw) -> str:
    """B 站投稿接口要求逗号分隔、不含 #。"""
    parts = _split_meta_tag_parts(raw) or _split_meta_tag_parts(DEFAULT_TAGS)
    return ",".join(parts)


def generate_publish_meta(
    transcript: str,
    *,
    file_name: str = "",
    fallback_title: str = "",
) -> dict:
    """调用专用 LLM 生成 title / desc / tags。

    Returns:
        {"title": str, "desc": str, "tags": str}
    """
    text = re.sub(r"\s+", " ", (transcript or "").strip())
    if len(text) > META_MAX_TRANSCRIPT_CHARS:
        text = text[:META_MAX_TRANSCRIPT_CHARS] + "…"

    fallback = sanitize_title(fallback_title or file_name or "字幕视频")
    if not text:
        return {
            "title": fallback,
            "desc": "",
            "tags": DEFAULT_TAGS,
        }

    chat_url = resolve_meta_llm_chat_url()
    if not chat_url:
        raise BiliUploadError("未配置投稿元数据 LLM 地址（BILI_META_LLM_URL）")

    user_prompt = (
        f"视频文件名：{file_name or '未知'}\n"
        f"转写文本：\n{text}\n\n"
        "请生成投稿 JSON。"
    )
    try:
        content = _post_llm_chat(
            chat_url,
            _META_SYSTEM_PROMPT,
            user_prompt,
            max_tokens=600,
            temperature=0.4,
        )
    except Exception as e:
        raise BiliUploadError(f"生成投稿文案失败: {e}") from e

    data = _parse_meta_json(content)
    title = sanitize_title(str(data.get("title") or "").strip() or fallback)
    desc = str(data.get("desc") or "").strip()[:2000]
    tags = _normalize_meta_tags(data.get("tags")) or DEFAULT_TAGS
    return {"title": title, "desc": desc, "tags": tags}


def publish_meta_cache_path(cache_dir: str, task_id: str) -> str:
    return os.path.join(cache_dir, f"{task_id}_publish_meta.json")


def load_publish_meta_cache(cache_dir: str, task_id: str, source_mtime: float | None = None) -> dict | None:
    """读取已缓存投稿文案；source_mtime 不一致时视为失效。"""
    path = publish_meta_cache_path(cache_dir, task_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    title = str(data.get("title") or "").strip()
    if not title:
        return None
    if source_mtime is not None:
        try:
            cached_mtime = float(data.get("source_mtime"))
        except (TypeError, ValueError):
            cached_mtime = None
        if cached_mtime is None or abs(cached_mtime - float(source_mtime)) > 1e-6:
            return None
    return {
        "title": sanitize_title(title),
        "desc": str(data.get("desc") or "").strip()[:2000],
        "tags": _normalize_meta_tags(data.get("tags")),
        "cover_path": data.get("cover_path"),
        "cached": True,
    }


def save_publish_meta_cache(
    cache_dir: str,
    task_id: str,
    *,
    title: str,
    desc: str = "",
    tags: str = "",
    source_mtime: float | None = None,
    cover_path: str | None = None,
) -> dict:
    """落盘保存投稿文案，供下次直接复用。"""
    os.makedirs(cache_dir, exist_ok=True)
    path = publish_meta_cache_path(cache_dir, task_id)
    # 保留已有 cover_path，除非本次显式传入
    existing = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                existing = loaded
    except (OSError, ValueError, TypeError):
        existing = {}

    resolved_cover = cover_path
    if resolved_cover is None:
        resolved_cover = existing.get("cover_path")

    payload = {
        "title": sanitize_title(title),
        "desc": str(desc or "").strip()[:2000],
        "tags": _normalize_meta_tags(tags) or DEFAULT_TAGS,
        "source_mtime": float(source_mtime) if source_mtime is not None else None,
        "updated_at": time.time(),
        "cover_path": resolved_cover,
    }
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
    return {
        "title": payload["title"],
        "desc": payload["desc"],
        "tags": payload["tags"],
        "cover_path": payload.get("cover_path"),
        "cached": True,
    }


# ---------------------------------------------------------------------------
# 投稿封面（Qwen-Image）
# ---------------------------------------------------------------------------

DEFAULT_COVER_WIDTH = 1280
DEFAULT_COVER_HEIGHT = 720
COVER_SUMMARY_CORE_CHARS = 800
COVER_VISUAL_PROMPT_MAX_CHARS = 600

_COVER_NO_TEXT_RULE = (
    "no text, no letters, no Chinese characters, no numbers, no title, no subtitle, "
    "no caption, no watermark, no logo, no QR code, no typography, no written words."
)

_COVER_VISUAL_SYSTEM_PROMPT = """你是视频封面视觉导演。根据标题与摘要核心，写出给文生图模型用的英文画面描述。
要求：
1. 只输出英文画面描述，不要解释、不要 Markdown、不要 JSON、不要中文。
2. 抓住摘要的核心主题，转成具体可视意象（人物、物体、场景、光影、色彩、构图）；不要罗列观点，不要复述原文句子或关键词列表。
3. 风格：cinematic poster or modern illustration，16:9 横版，主体突出，色彩鲜明。
4. 提示词里不要包含任何打算画在图片上的文字、标题、口号或段落。
5. 以这句结尾：no text, no letters, no Chinese characters, no watermark, no logo, no QR code.
6. 40～90 个英文单词。"""


def resolve_cover_image_url() -> str:
    """封面生成服务根地址，需设置 BILI_COVER_IMAGE_URL 或 QWEN_IMAGE_URL。"""
    raw = (
        os.environ.get("BILI_COVER_IMAGE_URL")
        or os.environ.get("QWEN_IMAGE_URL")
        or ""
    )
    return (raw or "").strip().rstrip("/")


def publish_cover_path(cache_dir: str, task_id: str) -> str:
    return os.path.join(cache_dir, f"{task_id}_publish_cover.png")


def extract_summary_core(summary: str, *, max_chars: int = COVER_SUMMARY_CORE_CHARS) -> str:
    """从结构化摘要中抽取核心主题（及少量要点），避免把整篇摘要塞进后续 Prompt。"""
    text = (summary or "").strip()
    if not text:
        return ""

    theme = ""
    m = re.search(r"【核心主题】\s*(.+?)(?=【|$)", text, re.DOTALL)
    if m:
        theme = re.sub(r"\s+", " ", m.group(1)).strip()

    bullets: list[str] = []
    p = re.search(r"【关键要点】\s*(.+?)(?=【|$)", text, re.DOTALL)
    if p:
        for raw_line in p.group(1).splitlines():
            line = re.sub(r"^[\s\-•\d\.、）)]+", "", raw_line).strip()
            if line:
                bullets.append(line)
            if len(bullets) >= 3:
                break

    parts = [x for x in (theme, *bullets) if x]
    core = "\n".join(parts).strip() if parts else text
    core = re.sub(r"\s+", " ", core).strip()
    if len(core) > max_chars:
        core = core[:max_chars].rstrip() + "…"
    return core


def _clean_cover_visual_prompt(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    fenced = re.search(r"```(?:\w+)?\s*(.*?)\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            text = str(data.get("prompt") or data.get("visual_prompt") or "").strip()
    text = text.strip().strip('"').strip("'")
    text = re.sub(r"[\u4e00-\u9fff]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > COVER_VISUAL_PROMPT_MAX_CHARS:
        text = text[:COVER_VISUAL_PROMPT_MAX_CHARS].rstrip() + "…"
    return text


def rewrite_cover_visual_prompt(
    *,
    title: str = "",
    desc: str = "",
    tags: str = "",
    summary: str = "",
) -> str:
    """用 LLM 把摘要核心改写成适合文生图的短画面描述；失败时返回空串。"""
    core = extract_summary_core(summary) or re.sub(r"\s+", " ", (desc or "").strip())
    if len(core) > COVER_SUMMARY_CORE_CHARS:
        core = core[:COVER_SUMMARY_CORE_CHARS].rstrip() + "…"
    title = (title or "").strip()
    tags = (tags or "").strip()
    if not (title or core):
        return ""

    chat_url = resolve_meta_llm_chat_url()
    if not chat_url:
        return ""

    user_parts = []
    if title:
        user_parts.append(f"标题：{title}")
    if core:
        user_parts.append(f"摘要核心：\n{core}")
    if tags:
        user_parts.append(f"标签：{tags}")
    user_parts.append("请根据核心主题写出封面画面描述，不要复述原文。")
    try:
        raw = _post_llm_chat(
            chat_url,
            _COVER_VISUAL_SYSTEM_PROMPT,
            "\n".join(user_parts),
            max_tokens=220,
            temperature=0.5,
        )
    except Exception:
        return ""
    cleaned = _clean_cover_visual_prompt(raw)
    for leak in (title, core):
        if leak and len(leak) >= 8 and leak in cleaned:
            cleaned = cleaned.replace(leak, " ")
    return re.sub(r"\s+", " ", cleaned).strip()


def build_cover_prompt(*, visual_prompt: str = "", title: str = "") -> str:
    """组装发给图像服务的 Prompt：只含画面描述，不含标题/简介原文。"""
    parts = [
        "A 16:9 cinematic video cover illustration, clear focal subject, vivid colors, modern poster composition.",
        _COVER_NO_TEXT_RULE,
    ]
    visual = (visual_prompt or "").strip()
    if visual:
        parts.append(visual)
    elif (title or "").strip():
        # LLM 不可用时仍不把标题原文交给图像模型，避免被画成大字。
        parts.append("Visualize the video's core idea as a single striking metaphor; do not paint any words.")
    return "\n".join(parts)


def generate_publish_cover(
    cache_dir: str,
    task_id: str,
    *,
    title: str = "",
    desc: str = "",
    tags: str = "",
    summary: str = "",
    width: int | None = None,
    height: int | None = None,
) -> dict:
    """先把摘要核心改写成画面 Prompt，再调用 Qwen-Image /generate，保存封面 PNG。

    Returns:
        {"cover_path": str, "cover_url": "/media/{task_id}/publish_cover", "prompt": str}
    """
    base = resolve_cover_image_url()
    if not base:
        raise BiliUploadError("未配置封面生成服务地址（BILI_COVER_IMAGE_URL）")

    if not (title or desc or summary or tags):
        raise BiliUploadError("缺少简介/标题，无法生成封面")

    visual_prompt = rewrite_cover_visual_prompt(
        title=title, desc=desc, tags=tags, summary=summary,
    )
    prompt = build_cover_prompt(visual_prompt=visual_prompt, title=title)

    try:
        w = int(width or os.environ.get("BILI_COVER_WIDTH") or DEFAULT_COVER_WIDTH)
    except (TypeError, ValueError):
        w = DEFAULT_COVER_WIDTH
    try:
        h = int(height or os.environ.get("BILI_COVER_HEIGHT") or DEFAULT_COVER_HEIGHT)
    except (TypeError, ValueError):
        h = DEFAULT_COVER_HEIGHT
    try:
        steps = int(os.environ.get("BILI_COVER_STEPS") or 20)
    except (TypeError, ValueError):
        steps = 20

    url = f"{base}/generate"
    payload = {
        "prompt": prompt,
        "width": w,
        "height": h,
        "num_inference_steps": steps,
        "cfg_scale": float(os.environ.get("BILI_COVER_CFG_SCALE") or 4.0),
    }
    try:
        r = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=300,
        )
        r.raise_for_status()
    except Exception as e:
        raise BiliUploadError(f"封面生成失败: {e}") from e

    content_type = (r.headers.get("Content-Type") or "").lower()
    raw = r.content or b""
    if not raw:
        raise BiliUploadError("封面生成返回为空")

    # 服务默认直接返回 PNG；也兼容 JSON base64
    if "application/json" in content_type or raw[:1] in (b"{", b"["):
        try:
            data = r.json()
        except ValueError as e:
            raise BiliUploadError(f"封面生成响应无法解析: {e}") from e
        b64 = None
        if isinstance(data, dict):
            b64 = data.get("image") or data.get("b64_json") or data.get("data")
            if isinstance(b64, list) and b64:
                first = b64[0]
                b64 = first.get("b64_json") if isinstance(first, dict) else first
        if not b64 or not isinstance(b64, str):
            raise BiliUploadError(f"封面生成响应缺少图像数据: {list(data) if isinstance(data, dict) else type(data)}")
        if "," in b64 and b64.strip().startswith("data:"):
            b64 = b64.split(",", 1)[1]
        try:
            raw = base64.b64decode(b64)
        except Exception as e:
            raise BiliUploadError(f"封面 base64 解码失败: {e}") from e

    if raw[:8] != b"\x89PNG\r\n\x1a\n" and raw[:2] != b"\xff\xd8":
        # 仍尝试按 PNG 落盘；若明显不是图片则报错
        if len(raw) < 64:
            raise BiliUploadError("封面生成返回内容过短，不是有效图片")

    os.makedirs(cache_dir, exist_ok=True)
    out_path = publish_cover_path(cache_dir, task_id)
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(raw)
    os.replace(tmp_path, out_path)

    return {
        "cover_path": out_path,
        "cover_url": f"/media/{task_id}/publish_cover",
        "prompt": prompt,
    }
