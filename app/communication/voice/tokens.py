"""Signed, expiring tokens for the provider's callbacks and the voice stream (AgentOS V2 Phase 5).

A token names one organization and one communication attempt, its purpose (``callback`` or ``stream``) and an
expiry, signed with HMAC-SHA256. The secret comes only from ``CAMPUSNEXUS_VOICE_TOKEN_SECRET``; unset, a random
secret is generated once per process (tokens then stop working on restart -- safe, never a committed secret). The
token is the only thing that ties an unauthenticated provider request to a tenant: it is verified before any
database access, and the attempt it names must still match the provider's call reference.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

ENV_SECRET = "CAMPUSNEXUS_VOICE_TOKEN_SECRET"
Purpose = Literal["callback", "stream"]

_lock = threading.Lock()
_process_secret: Optional[bytes] = None


class VoiceTokenError(Exception):
    """Missing, malformed, tampered, expired or for another purpose."""


@dataclass(frozen=True)
class VoiceTokenClaims:
    purpose: str
    organization_id: int
    attempt_id: int
    expires_at: int  # epoch seconds


def _secret() -> bytes:
    configured = os.environ.get(ENV_SECRET)
    if configured:
        return configured.encode("utf-8")
    global _process_secret
    with _lock:
        if _process_secret is None:
            _process_secret = secrets.token_bytes(32)
        return _process_secret


def _mac(body: str) -> str:
    return hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).hexdigest()


def sign(purpose: Purpose, organization_id: int, attempt_id: int, expires_at: datetime) -> str:
    body = f"{purpose}.{int(organization_id)}.{int(attempt_id)}.{int(expires_at.timestamp())}"
    encoded = base64.urlsafe_b64encode(body.encode("ascii")).decode("ascii").rstrip("=")
    return f"{encoded}.{_mac(body)}"


def verify(token: Optional[str], purpose: Purpose, now: datetime) -> VoiceTokenClaims:
    try:
        encoded, mac = (token or "").split(".")
        body = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("ascii")
        kind, org, attempt, expires = body.split(".")
        claims = VoiceTokenClaims(kind, int(org), int(attempt), int(expires))
    except (ValueError, UnicodeDecodeError):
        raise VoiceTokenError("MALFORMED") from None
    if not hmac.compare_digest(mac, _mac(body)):
        raise VoiceTokenError("BAD_SIGNATURE")
    if claims.purpose != purpose:
        raise VoiceTokenError("WRONG_PURPOSE")
    if now.timestamp() >= claims.expires_at:
        raise VoiceTokenError("EXPIRED")
    return claims
