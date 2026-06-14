"""Symmetric encryption for provider credentials at rest.

Uses Fernet (AES-128-CBC + HMAC-SHA256). The key comes from SECRET_KEY if set,
otherwise it is generated once and stored in DATA_DIR/secret.key with 0600 perms.
"""
from __future__ import annotations

import json
import os
from typing import Any

from cryptography.fernet import Fernet

from .config import settings


def _load_key() -> bytes:
    if settings.secret_key:
        return settings.secret_key.encode()

    key_path = settings.key_path
    if key_path.exists():
        return key_path.read_bytes().strip()

    key = Fernet.generate_key()
    key_path.write_bytes(key)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return key


_fernet = Fernet(_load_key())


def encrypt_json(data: dict[str, Any]) -> bytes:
    return _fernet.encrypt(json.dumps(data).encode())


def decrypt_json(token: bytes) -> dict[str, Any]:
    return json.loads(_fernet.decrypt(token).decode())
