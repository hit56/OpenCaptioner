import json
import os
import re
import subprocess
from functools import lru_cache

from subtitles import config
from subtitles._runtime import get_logger
from subtitles.wrap import _subtitle_play_metrics

@lru_cache(maxsize=256)
def _get_video_dimensions_cached(video_path):
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:stream_tags=rotate", "-of", "json", video_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
        s = json.loads(res.stdout or '{}').get('streams', [{}])[0]
        w, h, rot = s.get('width'), s.get('height'), 0
        if 'tags' in s and 'rotate' in s['tags']:
            rot = int(s['tags']['rotate'])
        else:
            # side_data=rotation 在部分视频上会触发很慢的深度探测；这里限时兜底，
            # 超时则直接使用原始宽高，避免“正在生成字幕视频...”卡很多分钟。
            try:
                res = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "side_data=rotation", "-of", "json", video_path],
                    capture_output=True,
                    text=True,
                    timeout=1.5,
                )
                side_data_stream = json.loads(res.stdout or '{}').get('streams', [{}])[0]
                for sd in side_data_stream.get('side_data_list', []):
                    if 'rotation' in sd:
                        rot = int(sd['rotation'])
                        break
            except subprocess.TimeoutExpired:
                get_logger().warning(
                    f"ffprobe rotation side_data timeout for {os.path.basename(video_path)}, fallback to raw dimensions."
                )
        if abs(rot) in [90, 270]:
            w, h = h, w
        if w and h:
            return int(w), int(h)
    except subprocess.TimeoutExpired:
        get_logger().warning(
            f"ffprobe size probe timeout for {os.path.basename(video_path)}, fallback to 1280x720."
        )
    except Exception:
        pass
    return 1280, 720

def get_video_dimensions(video_path):
    return _get_video_dimensions_cached(os.path.abspath(video_path))

def get_dynamic_style_conf(video_path=None, dimensions=None, font_name=None):
    if dimensions is None:
        w, h = get_video_dimensions(video_path)
    else:
        w, h = dimensions
    fs, mv, mlr, _prx, _z, _e = _subtitle_play_metrics(w, h)
    fn = (font_name or config.SUBTITLE_CJK_FONTNAME).strip() or config.SUBTITLE_CJK_FONTNAME
    return (
        f"force_style='Fontname={fn},Fontsize={fs},PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
        f"BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV={mv},MarginL={mlr},MarginR={mlr},WrapStyle=1'"
    )

def split_segment_by_sentence(text, start_ts, end_ts):
    if not text: return []
    parts = re.split(r'([。？！.?!])', text)
    raw = [curr.strip() for curr in (p1+p2 for p1, p2 in zip(parts[0::2], parts[1::2] + [''])) if curr.strip()]
    tot_len = sum(len(s) for s in raw)
    res, c_start = [], start_ts
    for s in raw:
        dur = (end_ts - start_ts) * (len(s) / tot_len) if tot_len else 0
        s_end = c_start + dur
        if len(s) > 20 and re.search(r'[，,、]', s):
            mid = len(s) / 2
            commas = [m.start() for m in re.finditer(r'[，,、]', s)]
            idx = min(commas, key=lambda x: abs(x - mid)) + 1
            dur1 = dur * (len(s[:idx]) / len(s))
            res.extend([
                {"text": s[:idx].rstrip("。，,、 "), "start": c_start, "end": c_start + dur1},
                {"text": s[idx:].rstrip("。，,、 "), "start": c_start + dur1, "end": s_end}
            ])
        else: res.append({"text": s.rstrip("。，,、 "), "start": c_start, "end": s_end})
        c_start = s_end
    return [r for r in res if r['text']]
