"""JWT bearer tokens (HS256).

The signing secret comes only from ``CAMPUSNEXUS_JWT_SECRET``. When it is not
set (local development), a random secret is generated once per process: tokens
then stop working when the API restarts, which is safe, never a fixed
committed secret. ``CAMPUSNEXUS_JWT_TTL_MINUTES`` sets the lifetime (default 8 h).
"""
from __future__ import annotations

import os
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

ALGORITHM = "HS256"
ISSUER = "campusnexus"
DEFAULT_TTL_MINUTES = 480

_lock = threading.Lock()
_process_secret: Optional[str] = None


class TokenError(Exception):
    """The token is missing, malformed, expired or not ours."""


@dataclass(frozen=True)
class TokenClaims:
    account_id: int
    role: str
    expires_at: datetime


def _secret() -> str:
    configured = os.environ.get("CAMPUSNEXUS_JWT_SECRET")
    if configured:
        return configured
    global _process_secret
    with _lock:
        if _process_secret is None:
            _process_secret = secrets.token_urlsafe(48)
        return _process_secret


def ttl() -> timedelta:
    raw = os.environ.get("CAMPUSNEXUS_JWT_TTL_MINUTES")
    minutes = int(raw) if raw and raw.isdigit() and int(raw) > 0 else DEFAULT_TTL_MINUTES
    return timedelta(minutes=minutes)


def issue_token(account_id: int, role: str, *, now: Optional[datetime] = None, lifetime: Optional[timedelta] = None) -> tuple[str, datetime]:
    issued = now or datetime.now(timezone.utc)
    expires = issued + (lifetime if lifetime is not None else ttl())
    payload = {"sub": str(account_id), "role": role, "iat": issued, "exp": expires, "iss": ISSUER}
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM), expires


def decode_token(token: str) -> TokenClaims:
    try:
        payload = jwt.decode(
            token, _secret(), algorithms=[ALGORITHM], issuer=ISSUER, options={"require": ["sub", "exp", "iat", "iss"]}
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("expired") from exc
    except jwt.PyJWTError as exc:
        raise TokenError("invalid") from exc
    try:
        account_id = int(payload["sub"])
    except (TypeError, ValueError) as exc:
        raise TokenError("invalid") from exc
    return TokenClaims(
        account_id=account_id, role=str(payload.get("role", "")),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
    )
