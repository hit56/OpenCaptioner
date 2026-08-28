"""Persist upload-task history keyed by authenticated owner user id."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Optional

_lock = threading.RLock()


def default_db_path() -> str:
    root = os.environ.get("UPLOAD_TASKS_DB", "").strip()
    if root:
        return root
    auth_db = os.environ.get("LOCAL_AUTH_DB", "").strip()
    if auth_db:
        return auth_db
    return os.path.join("saved_data", "local_users.db")


def _connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Optional[str] = None) -> str:
    path = db_path or default_db_path()
    with _lock:
        conn = _connect(path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS upload_tasks (
                    task_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT,
                    file_url TEXT,
                    original_file_url TEXT,
                    video_url TEXT,
                    media_duration_seconds REAL,
                    detected_lang TEXT,
                    detected_lang_name TEXT,
                    speaker_stats_json TEXT,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_upload_tasks_user_created
                ON upload_tasks(user_id, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id TEXT PRIMARY KEY,
                    user_name TEXT,
                    full_name TEXT,
                    email TEXT,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()
    return path


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    speaker_stats = None
    raw_stats = row["speaker_stats_json"]
    if raw_stats:
        try:
            parsed = json.loads(raw_stats)
            if isinstance(parsed, list):
                speaker_stats = parsed
        except (TypeError, json.JSONDecodeError):
            speaker_stats = None
    return {
        "task_id": row["task_id"],
        "user_id": row["user_id"],
        "file_name": row["file_name"],
        "status": row["status"],
        "message": row["message"] or "",
        "created_at": row["created_at"],
        "file_url": row["file_url"],
        "original_file_url": row["original_file_url"],
        "video_url": row["video_url"],
        "media_duration_seconds": row["media_duration_seconds"],
        "detected_lang": row["detected_lang"],
        "detected_lang_name": row["detected_lang_name"],
        "speaker_stats": speaker_stats,
        "updated_at": row["updated_at"],
    }


def upsert_task(
    db_path: str,
    *,
    task_id: str,
    user_id: str,
    file_name: str,
    status: str,
    message: str = "",
    created_at: str | None = None,
    file_url: str | None = None,
    original_file_url: str | None = None,
    video_url: str | None = None,
    media_duration_seconds: float | None = None,
    detected_lang: str | None = None,
    detected_lang_name: str | None = None,
    speaker_stats: list | None = None,
) -> dict[str, Any]:
    now = time.time()
    speaker_stats_json = json.dumps(speaker_stats, ensure_ascii=False) if speaker_stats is not None else None
    with _lock:
        conn = _connect(db_path)
        try:
            existing = conn.execute(
                "SELECT * FROM upload_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if existing:
                # Do not allow changing ownership silently.
                if existing["user_id"] != user_id:
                    raise PermissionError("task belongs to another user")
                conn.execute(
                    """
                    UPDATE upload_tasks SET
                        file_name = COALESCE(?, file_name),
                        status = COALESCE(?, status),
                        message = COALESCE(?, message),
                        created_at = COALESCE(?, created_at),
                        file_url = COALESCE(?, file_url),
                        original_file_url = COALESCE(?, original_file_url),
                        video_url = COALESCE(?, video_url),
                        media_duration_seconds = COALESCE(?, media_duration_seconds),
                        detected_lang = COALESCE(?, detected_lang),
                        detected_lang_name = COALESCE(?, detected_lang_name),
                        speaker_stats_json = COALESCE(?, speaker_stats_json),
                        updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        file_name or None,
                        status or None,
                        message if message is not None else None,
                        created_at,
                        file_url,
                        original_file_url,
                        video_url,
                        media_duration_seconds,
                        detected_lang,
                        detected_lang_name,
                        speaker_stats_json,
                        now,
                        task_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO upload_tasks (
                        task_id, user_id, file_name, status, message, created_at,
                        file_url, original_file_url, video_url, media_duration_seconds,
                        detected_lang, detected_lang_name, speaker_stats_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        user_id,
                        file_name,
                        status,
                        message or "",
                        created_at,
                        file_url,
                        original_file_url,
                        video_url,
                        media_duration_seconds,
                        detected_lang,
                        detected_lang_name,
                        speaker_stats_json,
                        now,
                    ),
                )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM upload_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        finally:
            conn.close()
    return _row_to_dict(row)


def update_task_fields(
    db_path: str,
    task_id: str,
    *,
    user_id: str | None = None,
    **fields: Any,
) -> dict[str, Any] | None:
    allowed = {
        "file_name",
        "status",
        "message",
        "created_at",
        "file_url",
        "original_file_url",
        "video_url",
        "media_duration_seconds",
        "detected_lang",
        "detected_lang_name",
        "speaker_stats",
    }
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return get_task(db_path, task_id, user_id=user_id)

    cols: list[str] = []
    values: list[Any] = []
    if "speaker_stats" in updates:
        cols.append("speaker_stats_json = ?")
        values.append(json.dumps(updates.pop("speaker_stats"), ensure_ascii=False))
    for key, value in updates.items():
        cols.append(f"{key} = ?")
        values.append(value)
    cols.append("updated_at = ?")
    values.append(time.time())
    values.append(task_id)

    with _lock:
        conn = _connect(db_path)
        try:
            existing = conn.execute(
                "SELECT * FROM upload_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if not existing:
                return None
            if user_id and existing["user_id"] != user_id:
                raise PermissionError("task belongs to another user")
            conn.execute(
                f"UPDATE upload_tasks SET {', '.join(cols)} WHERE task_id = ?",
                values,
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM upload_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        finally:
            conn.close()
    return _row_to_dict(row) if row else None


def get_task(
    db_path: str,
    task_id: str,
    *,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    with _lock:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                "SELECT * FROM upload_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    if user_id and row["user_id"] != user_id:
        return None
    return _row_to_dict(row)


def list_tasks_for_user(db_path: str, user_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 200), 500))
    with _lock:
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                """
                SELECT * FROM upload_tasks
                WHERE user_id = ?
                ORDER BY created_at DESC, updated_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        finally:
            conn.close()
    return [_row_to_dict(row) for row in rows]


def delete_task(db_path: str, task_id: str, *, user_id: str) -> bool:
    with _lock:
        conn = _connect(db_path)
        try:
            cur = conn.execute(
                "DELETE FROM upload_tasks WHERE task_id = ? AND user_id = ?",
                (task_id, user_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def upsert_user_profile(
    db_path: str,
    *,
    user_id: str,
    user_name: str | None = None,
    full_name: str | None = None,
    email: str | None = None,
) -> None:
    """Persist the display name of an authenticated owner id (scnet:{userId})."""
    user_id = (user_id or "").strip()
    if not user_id:
        return
    now = time.time()
    with _lock:
        conn = _connect(db_path)
        try:
            conn.execute(
                """
                INSERT INTO user_profiles (user_id, user_name, full_name, email, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    user_name = COALESCE(excluded.user_name, user_profiles.user_name),
                    full_name = COALESCE(excluded.full_name, user_profiles.full_name),
                    email = COALESCE(excluded.email, user_profiles.email),
                    updated_at = excluded.updated_at
                """,
                (user_id, user_name or None, full_name or None, email or None, now),
            )
            conn.commit()
        finally:
            conn.close()


def get_usage_stats(db_path: str) -> dict[str, Any]:
    """Aggregate completed upload tasks into operation statistics."""
    with _lock:
        conn = _connect(db_path)
        try:
            totals = conn.execute(
                """
                SELECT
                    COUNT(*) AS task_count,
                    COUNT(DISTINCT user_id) AS user_count,
                    COALESCE(SUM(media_duration_seconds), 0) AS total_duration
                FROM upload_tasks
                WHERE status = 'done'
                """
            ).fetchone()
            rows = conn.execute(
                """
                SELECT
                    t.user_id AS user_id,
                    p.user_name AS user_name,
                    p.full_name AS full_name,
                    COUNT(*) AS task_count,
                    COALESCE(SUM(t.media_duration_seconds), 0) AS total_duration,
                    MAX(COALESCE(t.created_at, '')) AS last_active
                FROM upload_tasks AS t
                LEFT JOIN user_profiles AS p ON p.user_id = t.user_id
                WHERE t.status = 'done'
                GROUP BY t.user_id
                ORDER BY total_duration DESC, task_count DESC
                """
            ).fetchall()
        finally:
            conn.close()

    users = []
    for row in rows:
        user_id = row["user_id"]
        display_name = (row["full_name"] or "").strip() or (row["user_name"] or "").strip() or user_id
        users.append(
            {
                "user_id": user_id,
                "display_name": display_name,
                "user_name": row["user_name"] or None,
                "task_count": int(row["task_count"] or 0),
                "total_duration_seconds": float(row["total_duration"] or 0.0),
                "last_active": row["last_active"] or None,
            }
        )

    return {
        "total_duration_seconds": float(totals["total_duration"] or 0.0) if totals else 0.0,
        "total_users": int(totals["user_count"] or 0) if totals else 0,
        "total_tasks": int(totals["task_count"] or 0) if totals else 0,
        "users": users,
    }


def get_task_owner(db_path: str, task_id: str) -> str | None:
    with _lock:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                "SELECT user_id FROM upload_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        finally:
            conn.close()
    return str(row["user_id"]) if row else None
