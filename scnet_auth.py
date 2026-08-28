"""SCNet OAuth2 helpers for gateway login."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlencode, urlparse

import requests

SCNET_OAUTH_AUTHORIZE_URL = os.environ.get(
    "SCNET_OAUTH_AUTHORIZE_URL",
    "https://www.scnet.cn/acx/user/oauth/authorize",
).strip()
SCNET_OAUTH_TOKEN_URL = os.environ.get(
    "SCNET_OAUTH_TOKEN_URL",
    "https://www.scnet.cn/acx/user/v1/oauth/token",
).strip()
SCNET_OAUTH_USERINFO_URL = os.environ.get(
    "SCNET_OAUTH_USERINFO_URL",
    "https://www.scnet.cn/acx/user/v1/oauth/userinfo",
).strip()
SCNET_OAUTH_CLIENT_ID = os.environ.get("SCNET_OAUTH_CLIENT_ID", "").strip()
SCNET_OAUTH_CLIENT_SECRET = os.environ.get("SCNET_OAUTH_CLIENT_SECRET", "").strip()
SCNET_OAUTH_REDIRECT_URI = os.environ.get("SCNET_OAUTH_REDIRECT_URI", "").strip()
SCNET_OAUTH_TIMEOUT_S = float(os.environ.get("SCNET_OAUTH_TIMEOUT_S", "15"))


def _oauth_proxies() -> dict[str, str] | None:
    """可选 HTTP 代理。显式传入 proxies，避免被 NO_PROXY 误旁路。

    优先级：SCNET_OAUTH_PROXY > https_proxy/HTTPS_PROXY/http_proxy/HTTP_PROXY。
    """
    for key in (
        "SCNET_OAUTH_PROXY",
        "https_proxy",
        "HTTPS_PROXY",
        "http_proxy",
        "HTTP_PROXY",
    ):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return {"http": value, "https": value}
    return None


def _oauth_request(method: str, url: str, **kwargs):
    """对 SCNet OAuth 端点发请求：若已配置代理则强制走代理，忽略 NO_PROXY。"""
    proxies = _oauth_proxies()
    kwargs.setdefault("timeout", SCNET_OAUTH_TIMEOUT_S)
    session = requests.Session()
    session.trust_env = False
    if proxies is not None:
        session.proxies.update(proxies)
    try:
        return session.request(method, url, **kwargs)
    finally:
        session.close()


def _is_local_or_internal_host(host: str) -> bool:
    host = (host or "").strip().lower().split(":", 1)[0]
    if not host:
        return True
    if host in {"localhost", "::1"}:
        return True
    if host.startswith("127."):
        return True
    if host.startswith("10."):
        return True
    if host.startswith("192.168."):
        return True
    if host.startswith("172."):
        parts = host.split(".")
        if len(parts) >= 2 and parts[1].isdigit():
            second = int(parts[1])
            if 16 <= second <= 31:
                return True
    return False


def _is_local_or_internal_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return True
    return _is_local_or_internal_host(parsed.hostname or "")


def resolve_public_url_from_request(request) -> str | None:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    host = forwarded_host or request.headers.get("host", "").strip()
    if not host or _is_local_or_internal_host(host.split(":", 1)[0]):
        return None
    scheme = forwarded_proto or request.url.scheme or "https"
    return f"{scheme}://{host}".rstrip("/")


def resolve_scnet_redirect_uri(
    gateway_public_url: str,
    request_public_url: str | None = None,
) -> str:
    candidates = [SCNET_OAUTH_REDIRECT_URI, request_public_url, gateway_public_url]
    redirect_base = ""
    for candidate in candidates:
        url = (candidate or "").strip().rstrip("/")
        if url and not _is_local_or_internal_url(url):
            redirect_base = url
            break
    if not redirect_base:
        for candidate in candidates:
            url = (candidate or "").strip().rstrip("/")
            if url:
                redirect_base = url
                break
    return redirect_base.rstrip("/") + "/"


def build_scnet_authorize_url(
    redirect_uri: str,
    *,
    silent: bool = False,
    state: str | None = None,
) -> str:
    params = {
        "response_type": "code",
        "client_id": SCNET_OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri,
    }
    if silent:
        params["prompt"] = "none"
    if state:
        params["state"] = state
    return f"{SCNET_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


def exchange_scnet_code_for_tokens(code: str, redirect_uri: str) -> dict[str, Any]:
    if not SCNET_OAUTH_CLIENT_SECRET:
        raise RuntimeError("SCNET OAuth client secret is not configured")
    params = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": SCNET_OAUTH_CLIENT_ID,
        "client_secret": SCNET_OAUTH_CLIENT_SECRET,
        "redirect_uri": redirect_uri,
    }
    response = _oauth_request("POST", SCNET_OAUTH_TOKEN_URL, params=params)
    if response.status_code != 200:
        raise RuntimeError(f"SCNet token exchange failed: HTTP {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise RuntimeError("SCNet token exchange returned invalid payload")
    return payload


def fetch_scnet_userinfo(access_token: str) -> dict[str, Any]:
    response = _oauth_request(
        "GET",
        SCNET_OAUTH_USERINFO_URL,
        params={"access_token": access_token},
    )
    if response.status_code != 200:
        raise RuntimeError(f"SCNet userinfo failed: HTTP {response.status_code}")
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("userId") is None:
        raise RuntimeError("SCNet userinfo returned invalid payload")
    return payload
