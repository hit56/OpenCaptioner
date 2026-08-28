"""Sync ASR artifacts from offline worker to gateway (distributed deployment)."""

import io
import json
import os
import zipfile

import requests

from subtitles import config


def sync_session_artifacts_to_gateway(
    session_id: str,
    session_seg_dir: str,
    final_results_list: list,
    gateway_base_url: str,
    *,
    internal_token: str = "",
    logger=None,
) -> bool:
    """
    Pack final_result.json + segment wav files into a zip and POST to gateway.
    Returns True on success.
    """
    log = logger
    gateway_base_url = (gateway_base_url or "").rstrip("/")
    if not gateway_base_url:
        if log:
            log.error(f"[{session_id}] sync_session_artifacts: missing gateway_base_url")
        return False

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "final_result.json",
            json.dumps(final_results_list, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        if os.path.isdir(session_seg_dir):
            for name in sorted(os.listdir(session_seg_dir)):
                if not name.lower().endswith(".wav"):
                    continue
                full = os.path.join(session_seg_dir, name)
                if os.path.isfile(full):
                    zf.write(full, arcname=name)

    headers = {}
    if internal_token:
        headers["X-Internal-Worker-Token"] = internal_token

    url = f"{gateway_base_url}/internal/sync_session_artifacts"
    try:
        resp = requests.post(
            url,
            params={"session_id": session_id},
            files={"archive": (f"{session_id}_artifacts.zip", buf.getvalue(), "application/zip")},
            headers=headers,
            timeout=600,
        )
        resp.raise_for_status()
        if log:
            log.info(f"[{session_id}] Artifacts synced to gateway ({len(buf.getvalue())} bytes)")
        return True
    except Exception as e:
        if log:
            log.error(f"[{session_id}] Artifact sync failed: {e}")
        return False
