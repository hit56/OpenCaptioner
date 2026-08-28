"""Lightweight SMTP helpers for auth verification emails."""

from __future__ import annotations

import base64
import logging
import os
import smtplib
import socket
import ssl
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from urllib.parse import urlparse

logger = logging.getLogger("smtp_mail")


def _smtp_proxy_url() -> str:
    for key in ("SMTP_HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def _open_smtp_socket(smtp_host: str, smtp_port: int, timeout: float = 20):
    """Open a TCP socket to SMTP, optionally via HTTP CONNECT proxy."""
    proxy = _smtp_proxy_url()
    if not proxy:
        return socket.create_connection((smtp_host, smtp_port), timeout=timeout)

    parsed = urlparse(proxy)
    proxy_host = parsed.hostname
    proxy_port = parsed.port or 3120
    if not proxy_host:
        raise RuntimeError(f"invalid SMTP proxy URL: {proxy}")

    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    req = f"CONNECT {smtp_host}:{smtp_port} HTTP/1.1\r\nHost: {smtp_host}:{smtp_port}\r\n"
    if parsed.username is not None:
        token = base64.b64encode(f"{parsed.username}:{parsed.password or ''}".encode()).decode()
        req += f"Proxy-Authorization: Basic {token}\r\n"
    req += "\r\n"
    sock.sendall(req.encode())

    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            break
        response += chunk
    status_line = response.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
    if "200" not in status_line:
        sock.close()
        raise RuntimeError(f"SMTP proxy CONNECT failed: {status_line}")
    return sock


def send_email(message: str, subject: str, recipient: str) -> int:
    """
    Send email via SMTP env config.

    Returns:
        0 on success, -1 on failure
    """
    smtp_host = os.environ.get("SMTP_HOST", "").strip()
    if not smtp_host:
        return -1

    smtp_port = int(os.environ.get("SMTP_PORT", "465") or "465")
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_password = os.environ.get("SMTP_PASSWORD", "").strip()
    smtp_from = os.environ.get("SMTP_FROM", "").strip() or smtp_user or recipient
    use_ssl = os.environ.get("SMTP_SSL", "1").strip().lower() not in {"0", "false", "no"}
    use_tls = os.environ.get("SMTP_TLS", "").strip().lower() in {"1", "true", "yes"}
    if use_tls:
        use_ssl = False

    msg = MIMEText(message, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("智能语音转写", "utf-8")), smtp_from))
    msg["To"] = recipient

    try:
        raw_sock = _open_smtp_socket(smtp_host, smtp_port, timeout=20)
        if use_ssl:
            context = ssl.create_default_context()
            sock = context.wrap_socket(raw_sock, server_hostname=smtp_host)
            server = smtplib.SMTP_SSL()
            server.sock = sock
            server.file = None
            code, resp = server.getreply()
            if code != 220:
                raise smtplib.SMTPConnectError(code, resp)
        else:
            server = smtplib.SMTP()
            server.sock = raw_sock
            server.file = None
            code, resp = server.getreply()
            if code != 220:
                raise smtplib.SMTPConnectError(code, resp)
            server.ehlo()
            if use_tls:
                server.starttls()
                server.ehlo()

        with server:
            if smtp_user:
                server.login(smtp_user, smtp_password)
            server.sendmail(smtp_from, [recipient], msg.as_string())
        return 0
    except Exception as exc:
        logger.warning("SMTP send failed to %s via %s: %s", recipient, smtp_host, exc)
        print(f"[SMTP] send failed to {recipient}: {exc}", flush=True)
        return -1
