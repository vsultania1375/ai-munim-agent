"""Encryption for secrets we must keep (ERP sign-in passwords, Razorpay keys), with key rotation.

MUNIM_DATA_KEYS lists the keys, newest first, comma-separated. New values are always written with the newest;
any listed key still reads old values. To rotate: put a new key in front, restart (stored secrets are re-encrypted
with it at start), then drop the old key. Without MUNIM_DATA_KEYS the server secret is the key.
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

LEGACY_LABEL = b"munim-razorpay-v1:"  # how Razorpay secrets were encrypted before this module; read-only


def _fernet(label: bytes, secret: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(label + secret.encode()).digest()))


class SecretBox:
    def __init__(self, keys: list[str]) -> None:
        keys = [k for k in (k.strip() for k in keys) if k]
        if not keys:
            raise ValueError("at least one data key is needed")
        current = [_fernet(b"munim-data-v1:", k) for k in keys]
        legacy = [_fernet(LEGACY_LABEL, k) for k in keys]
        self._write = current[0]
        self._read = MultiFernet(current + legacy)

    @classmethod
    def from_env(cls, fallback: str) -> SecretBox:
        listed = [k for k in os.environ.get("MUNIM_DATA_KEYS", "").split(",") if k.strip()]
        return cls(listed + [fallback])  # the server secret stays readable, so turning rotation on loses nothing

    def seal(self, value: str) -> str:
        return self._write.encrypt(value.encode()).decode()

    def open(self, token: str | None) -> str | None:
        """The value, or None if no listed key can read it (a key was dropped too early)."""
        if not token:
            return None
        try:
            return self._read.decrypt(token.encode()).decode()
        except InvalidToken:
            return None

    def reseal(self, token: str) -> str | None:
        """The same value under the newest key (None if unreadable)."""
        value = self.open(token)
        return None if value is None else self.seal(value)
