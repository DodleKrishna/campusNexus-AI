"""Password hashing with bcrypt. Plaintext passwords are never stored or logged."""
from __future__ import annotations

import bcrypt

# bcrypt only uses the first 72 bytes of a password; longer inputs are rejected
# explicitly rather than silently truncated.
MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    raw = password.encode("utf-8")
    if not raw or len(raw) > MAX_PASSWORD_BYTES:
        raise ValueError(f"password must be 1-{MAX_PASSWORD_BYTES} bytes")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    raw = password.encode("utf-8")
    if not raw or len(raw) > MAX_PASSWORD_BYTES or not password_hash:
        return False
    try:
        return bcrypt.checkpw(raw, password_hash.encode("ascii"))
    except ValueError:
        return False
