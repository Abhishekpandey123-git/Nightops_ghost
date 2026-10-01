"""Security primitives. Standard library only, no extra dependencies.

- Passwords: scrypt (memory-hard), random salt, constant-time compare.
- 2FA: TOTP per RFC 6238 (works with Google Authenticator, Authy, etc.).
- Sessions: random 256-bit ids; only a SHA-256 hash is stored server-side.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time

_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024, "dklen": 32}


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        # Still burn the same CPU so timing doesn't reveal "no password set".
        hashlib.scrypt(password.encode(), salt=b"0" * 16, **_SCRYPT)
        return False
    try:
        algo, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), **_SCRYPT)
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


# ------------------------------------------------------------------------ TOTP
def new_totp_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def totp_code(secret_b32: str, at: float | None = None, step: int = 30, digits: int = 6) -> str:
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    counter = int((time.time() if at is None else at) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return f"{value:0{digits}d}"


def verify_totp(secret_b32: str | None, code: str, window: int = 1) -> bool:
    """Accepts the current code and one step either side (clock drift)."""
    if not secret_b32:
        return False
    code = (code or "").strip().replace(" ", "")
    if not (code.isdigit() and len(code) == 6):
        return False
    now = time.time()
    return any(hmac.compare_digest(totp_code(secret_b32, now + i * 30), code)
               for i in range(-window, window + 1))


def otpauth_uri(secret_b32: str, user: str, issuer: str = "nightops") -> str:
    return (f"otpauth://totp/{issuer}:{user}?secret={secret_b32}"
            f"&issuer={issuer}&digits=6&period=30&algorithm=SHA1")


# --------------------------------------------------------------------- tokens
def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
