"""Shared NATS connect helper — token / user-pass / nkey / TLS.

Env (priority):
  1. NATS_URL with embedded user:pass or :token@
  2. NATS_USER + NATS_PASSWORD
  3. STARSHIP_NATS_TOKEN (token auth)
  4. STARSHIP_NATS_NKEY_SEED or path in STARSHIP_NATS_NKEY_SEED_FILE
  5. STARSHIP_NATS_TLS=1 + STARSHIP_NATS_CA[/CERT/KEY] for TLS
"""

from __future__ import annotations

import os
import ssl
from typing import Any, Optional
from urllib.parse import quote


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

    # Upgrade scheme when TLS requested
    tls_on = os.getenv("STARSHIP_NATS_TLS", "").strip().lower() in ("1", "true", "yes", "on")
    if tls_on and url.startswith("nats://"):
        url = "tls://" + url[len("nats://"):]

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


def tls_context() -> Optional[ssl.SSLContext]:
    """Build SSL context when STARSHIP_NATS_TLS is enabled."""
    flag = os.getenv("STARSHIP_NATS_TLS", "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return None
    ca = os.getenv("STARSHIP_NATS_CA", "").strip()
    cert = os.getenv("STARSHIP_NATS_CERT", "").strip()
    key = os.getenv("STARSHIP_NATS_KEY", "").strip()
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    if ca and os.path.isfile(ca):
        ctx.load_verify_locations(cafile=ca)
    else:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if cert and key and os.path.isfile(cert) and os.path.isfile(key):
        ctx.load_cert_chain(certfile=cert, keyfile=key)
    return ctx


def connect_kwargs() -> dict[str, Any]:
    """Extra kwargs for nats.connect (nkeys / tls if available)."""
    kw: dict[str, Any] = {}
    seed = nkey_seed()
    if seed:
        kw["nkeys_seed_str"] = seed
    tls = tls_context()
    if tls is not None:
        kw["tls"] = tls
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
            host = final_url.split("@")[-1] if "@" in final_url else final_url
            return await nats_connect(host, **kw)
        except Exception:
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
