"""Admin role helpers: decide which authenticated users are administrators."""

from __future__ import annotations

import os

# Comma-separated usernames from ADMIN_USERNAMES. Matching is case-insensitive.
# Do not hardcode production accounts here.


def get_admin_usernames() -> set[str]:
    raw = os.environ.get("ADMIN_USERNAMES", "").strip()
    if not raw:
        return set()
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def is_admin_username(username: str | None) -> bool:
    if not username:
        return False
    return username.strip().lower() in get_admin_usernames()


def is_admin_user(user: dict | None) -> bool:
    """Decide admin status from an auth user dict (matches on userName)."""
    if not user:
        return False
    return is_admin_username(str(user.get("userName") or ""))
