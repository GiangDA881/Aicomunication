"""Credential encryption, API keys, and secret redaction."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

from cryptography.fernet import Fernet, InvalidToken

from agenthub.config import get_settings


def _fernet(key: str | None = None) -> Fernet:
    key = key or get_settings().credential_key
    if not key:
        raise RuntimeError(
            "AGENTHUB_CREDENTIAL_KEY is not set; generate one with "
            "`python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`"
        )
    return Fernet(key.encode())


def encrypt_secret(plaintext: str, key: str | None = None) -> bytes:
    return _fernet(key).encrypt(plaintext.encode())


def decrypt_secret(ciphertext: bytes, key: str | None = None) -> str:
    try:
        return _fernet(key).decrypt(ciphertext).decode()
    except InvalidToken as exc:  # pragma: no cover - key rotation problem
        raise RuntimeError("credential could not be decrypted with the configured key") from exc


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[-8:]


def new_api_key() -> tuple[str, str]:
    """Return (plaintext, sha256 hex). Only the hash is stored."""
    key = "ah_" + secrets.token_urlsafe(32)
    return key, hash_api_key(key)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def verify_github_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"ah_[A-Za-z0-9_\-]{20,}"),
    re.compile(r"xox[abpr]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"[MN][A-Za-z\d]{23,25}\.[\w-]{6}\.[\w-]{27,}"),  # Discord bot token shape
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
]


def redact(text: str, extra_secrets: list[str] | None = None) -> str:
    for s in extra_secrets or []:
        if s:
            text = text.replace(s, "[REDACTED]")
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text
