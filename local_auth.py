"""Local account registration / password login helpers."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff-]{3,32}$")

CODE_TTL_SECONDS = 300
CODE_COOLDOWN_SECONDS = 60
SESSION_TTL_SECONDS = 30 * 24 * 3600
MIN_PASSWORD_LEN = 6

_lock = threading.RLock()
_code_store: dict[str, dict] = {}
_sessions: dict[str, dict] = {}


@dataclass
class LocalUser:
    user_id: str
    username: str
    full_name: str
    email: str

    def to_auth_user(self) -> dict:
        return {
            "userId": self.user_id,
            "userName": self.username,
            "fullName": self.full_name,
            "email": self.email,
            "mobile": None,
        }


def default_db_path() -> str:
    root = os.environ.get("LOCAL_AUTH_DB", "").strip()
    if root:
        return root
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
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    full_name TEXT NOT NULL,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()
    return path


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email.strip()))


def is_valid_username(username: str) -> bool:
    return bool(USERNAME_RE.match(username.strip()))


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return f"pbkdf2_sha256${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, digest_hex = stored.split("$", 2)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    salt = bytes.fromhex(salt_hex)
    expected = hash_password(password, salt)
    return secrets.compare_digest(expected, stored)


def _email_key(email: str) -> str:
    return email.strip().lower()


def create_verification_code(email: str) -> tuple[str, int]:
    """Create (or refresh) a verification code. Returns (code, cooldown_seconds)."""
    key = _email_key(email)
    now = time.time()
    with _lock:
        existing = _code_store.get(key)
        if existing and now - existing["sent_at"] < CODE_COOLDOWN_SECONDS:
            remain = int(CODE_COOLDOWN_SECONDS - (now - existing["sent_at"]))
            raise ValueError(f"请稍后再试（{max(remain, 1)} 秒）")
        code = f"{secrets.randbelow(1_000_000):06d}"
        _code_store[key] = {
            "code": code,
            "sent_at": now,
            "expires_at": now + CODE_TTL_SECONDS,
            "attempts": 0,
        }
    return code, CODE_COOLDOWN_SECONDS


def verify_and_consume_code(email: str, code: str) -> bool:
    key = _email_key(email)
    now = time.time()
    with _lock:
        entry = _code_store.get(key)
        if not entry:
            return False
        if now > entry["expires_at"]:
            _code_store.pop(key, None)
            return False
        entry["attempts"] += 1
        if entry["attempts"] > 8:
            _code_store.pop(key, None)
            return False
        if not secrets.compare_digest(str(entry["code"]), str(code).strip()):
            return False
        _code_store.pop(key, None)
        return True


def get_user_by_username(db_path: str, username: str) -> Optional[LocalUser]:
    with _lock:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                "SELECT user_id, username, full_name, email FROM users WHERE username = ? COLLATE NOCASE",
                (username.strip(),),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    return LocalUser(
        user_id=row["user_id"],
        username=row["username"],
        full_name=row["full_name"],
        email=row["email"],
    )


def get_user_by_email(db_path: str, email: str) -> Optional[LocalUser]:
    with _lock:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                "SELECT user_id, username, full_name, email FROM users WHERE email = ? COLLATE NOCASE",
                (email.strip(),),
            ).fetchone()
        finally:
            conn.close()
    if not row:
        return None
    return LocalUser(
        user_id=row["user_id"],
        username=row["username"],
        full_name=row["full_name"],
        email=row["email"],
    )


def register_user(
    db_path: str,
    *,
    username: str,
    full_name: str,
    email: str,
    password: str,
) -> LocalUser:
    username = username.strip()
    full_name = full_name.strip()
    email = email.strip()
    if not is_valid_username(username):
        raise ValueError("用户名需为 3-32 位字母、数字、下划线或中文")
    if not full_name or len(full_name) > 64:
        raise ValueError("请输入有效姓名")
    if not is_valid_email(email):
        raise ValueError("邮箱格式不正确")
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"密码至少 {MIN_PASSWORD_LEN} 位")

    user_id = f"local_{uuid.uuid4().hex[:16]}"
    password_hash = hash_password(password)
    now = time.time()

    with _lock:
        conn = _connect(db_path)
        try:
            if conn.execute(
                "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)
            ).fetchone():
                raise ValueError("用户名已被注册")
            if conn.execute(
                "SELECT 1 FROM users WHERE email = ? COLLATE NOCASE", (email,)
            ).fetchone():
                raise ValueError("邮箱已被注册")
            conn.execute(
                """
                INSERT INTO users (user_id, username, full_name, email, password_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, username, full_name, email, password_hash, now),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("用户名或邮箱已被注册") from exc
        finally:
            conn.close()

    return LocalUser(user_id=user_id, username=username, full_name=full_name, email=email)


def authenticate_user(db_path: str, username: str, password: str) -> LocalUser:
    username = username.strip()
    with _lock:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                """
                SELECT user_id, username, full_name, email, password_hash
                FROM users
                WHERE username = ? COLLATE NOCASE OR email = ? COLLATE NOCASE
                """,
                (username, username),
            ).fetchone()
        finally:
            conn.close()
    if not row or not verify_password(password, row["password_hash"]):
        raise ValueError("用户名或密码错误")
    return LocalUser(
        user_id=row["user_id"],
        username=row["username"],
        full_name=row["full_name"],
        email=row["email"],
    )


def create_session(user: LocalUser) -> str:
    token = f"local_{secrets.token_urlsafe(32)}"
    with _lock:
        _sessions[token] = {
            "user": user.to_auth_user(),
            "expires_at": time.time() + SESSION_TTL_SECONDS,
        }
    return token


def validate_session(token: str) -> Optional[dict]:
    token = (token or "").strip()
    if not token.startswith("local_"):
        return None
    now = time.time()
    with _lock:
        entry = _sessions.get(token)
        if not entry:
            return None
        if now > entry["expires_at"]:
            _sessions.pop(token, None)
            return None
        return entry["user"]


def revoke_session(token: str) -> None:
    with _lock:
        _sessions.pop((token or "").strip(), None)


def send_verification_email(email: str, code: str) -> None:
    """Send registration verification code. Raises RuntimeError on failure."""
    subject = "【智能语音转写】邮箱验证码"
    message = (
        f"您的注册验证码是：{code}，{CODE_TTL_SECONDS // 60} 分钟内有效。"
        "如非本人操作请忽略。"
    )

    smtp_host = os.environ.get("SMTP_HOST", "").strip()
    dev_mode = os.environ.get("AUTH_EMAIL_DEV_MODE", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }

    try:
        from smtp_mail import send_email
    except ImportError:
        send_email = None  # type: ignore

    if send_email is not None:
        result = send_email(message, subject, email)
        if result == 0:
            return
        if smtp_host:
            raise RuntimeError("验证码邮件发送失败，请检查 SMTP/代理配置或稍后重试")

    if dev_mode:
        return

    if not smtp_host:
        raise RuntimeError(
            "邮件服务未配置：请设置 SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM"
        )
    raise RuntimeError("验证码邮件发送失败，请稍后重试")
