"""Restricted sharing URL encryption shared by document workflows."""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


prefix = "enc:"


def encrypt(url, config):
    """Encrypt a URL for restricted sharing."""
    key = base64.urlsafe_b64decode(config["encryption_key"])
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    encrypted = aesgcm.encrypt(nonce, url.encode(), None)
    ciphercode = base64.urlsafe_b64encode(nonce + encrypted).decode()
    return f"{prefix}{ciphercode}"


def decrypt(ciphertext, config):
    """Decrypt a previously encrypted URL."""
    encrypted_url = ciphertext.removeprefix(prefix)
    data = base64.urlsafe_b64decode(encrypted_url)
    nonce, ct = data[:12], data[12:]
    key = base64.urlsafe_b64decode(config["encryption_key"])
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode()


__all__ = [
    "decrypt",
    "encrypt",
    "prefix",
]
