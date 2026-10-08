from __future__ import annotations

import base64
import hashlib
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    secret = (getattr(settings, 'CREDENTIAL_ENCRYPTION_KEY', '') or settings.SECRET_KEY).encode()
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    if not value:
        return ''
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    if not value:
        return ''
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        raise RuntimeError('Stored credential cannot be decrypted. Check CREDENTIAL_ENCRYPTION_KEY / SECRET_KEY.')
