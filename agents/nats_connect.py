"""Shared NATS connect helper — token / user-pass / nkey.

Env (priority):
  1. NATS_URL with embedded user:pass or :token@
  2. NATS_USER + NATS_PASSWORD
  3. STARSHIP_NATS_TOKEN (token auth)
  4. STARSHIP_NATS_NKEY_SEED or path in STARSHIP_NATS_NKEY_SEED_FILE
"""

from __future__ import annotations

import os
from typing import Any, Optional
from urllib.parse import quote, urlparse, urlunparse


def build_nats_url(
    url: Optional[str] = None,
    *,
    user: Optional[str] = None,
    password: Optional[str] = None,
    token: Optional[str] = None,
) -> str:
    url = url or os.getenv("NATS_URL", "nats://127.0.0.1:4222")
    user = user if user is not None else os.getenv("NATS_USER", "").strip() or None
    password = password if password is not None else os.getenv("NATS_PASSWORD", "").strip() or None
    token = token if token is not None else os.getenv("STARSHIP_NATS_TOKEN", "").strip() or None

    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" in rest:
        return url  # already has credentials

    if user and password:
        return f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}@{rest}"
    if token:
        return f"{scheme}://:{quote(token, safe='')}@{rest}"
    return url


def nkey_seed() -> Optional[str]:
    seed = os.getenv("STARSHIP_NATS_NKEY_SEED", "").strip()
    if seed:
        return seed
    path = os.getenv("STARSHIP_NATS_NKEY_SEED_FILE", "").strip()
    if path and os.path.isfile(path):
        return open(path, encoding="utf-8").read().strip()
    return None


def connect_kwargs() -> dict[str, Any]:
    """Extra kwargs for nats.connect (nkeys if available)."""
    kw: dict[str, Any] = {}
    seed = nkey_seed()
    if not seed:
        return kw
    # nats-py accepts nkeys_seed_str when `nkeys` package installed
    kw["nkeys_seed_str"] = seed
    return kw


async def connect(url: Optional[str] = None, **kwargs):
    """Connect to NATS using env credentials."""
    from nats import connect as nats_connect

    final_url = build_nats_url(url)
    kw = connect_kwargs()
    kw.update(kwargs)
    # If nkeys provided, prefer nkey auth (drop userinfo from URL to avoid conflict)
    if kw.get("nkeys_seed_str") or kw.get("nkeys_seed"):
        try:
            return await nats_connect(final_url.split("@")[-1] if "@" in final_url else final_url, **kw)
        except Exception:
            # Fall back to user/password URL without nkeys
            kw.pop("nkeys_seed_str", None)
            kw.pop("nkeys_seed", None)
            return await nats_connect(final_url, **kw)
    return await nats_connect(final_url, **kw)


def safe_url(url: Optional[str] = None) -> str:
    """URL with password redacted for logs."""
    u = build_nats_url(url)
    if "://" not in u or "@" not in u:
        return u
    scheme, rest = u.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:***@{host}"
    return f"{scheme}://***@{host}"
