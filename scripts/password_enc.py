"""Encrypt passwords with a string key. Run directly for interactive input."""

import base64
import binascii
import os
from getpass import getpass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


def _derive_key(key: str, salt: bytes) -> bytes:
    if not isinstance(key, str) or not key:
        raise ValueError("The encryption key must be a non-empty string.")
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(
        key.encode("utf-8")
    )


def encrypt_password(password: str, key: str) -> str:
    """Return a URL-safe token; keep the string key secret to decrypt it later."""
    if not isinstance(password, str):
        raise TypeError("The password must be a string.")
    salt = os.urandom(16)
    nonce = os.urandom(12)
    version = b"\x01"
    ciphertext = AESGCM(_derive_key(key, salt)).encrypt(
        nonce, password.encode("utf-8"), version
    )
    return base64.urlsafe_b64encode(version + salt + nonce + ciphertext).decode("ascii")


def decrypt_password(token: str, key: str) -> str:
    """Recover a password, rejecting an incorrect key or a damaged token."""
    try:
        data = base64.b64decode(token, altchars=b"-_", validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError("Invalid encrypted password token.") from exc
    if len(data) < 45 or data[:1] != b"\x01":
        raise ValueError("Invalid or unsupported encrypted password token.")
    salt, nonce, ciphertext = data[1:17], data[17:29], data[29:]
    try:
        return AESGCM(_derive_key(key, salt)).decrypt(
            nonce, ciphertext, data[:1]
        ).decode("utf-8")
    except InvalidTag as exc:
        raise ValueError("Incorrect key or damaged encrypted password.") from exc


if __name__ == "__main__":
    password = getpass("Password: ")
    key = getpass("Encryption key: ")
    try:
        print(encrypt_password(password, key))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
