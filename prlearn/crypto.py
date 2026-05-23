from __future__ import annotations

import base64
import getpass
import json
import os
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes


ENCRYPTED_PREFIX = "prlearnenc:v1:"
DEFAULT_PASSPHRASE_ENV = "PRLEARN_PASSPHRASE"
KDF_ITERATIONS = 600_000


class EncryptionError(RuntimeError):
    pass


class EncryptionLocked(EncryptionError):
    pass


@dataclass(frozen=True)
class PrivacyConfig:
    encrypt_raw_json: bool = False
    passphrase_env: str = DEFAULT_PASSPHRASE_ENV


def privacy_config(config: dict[str, Any] | None) -> PrivacyConfig:
    privacy = (config or {}).get("privacy") or {}
    if not isinstance(privacy, dict):
        privacy = {}
    return PrivacyConfig(
        encrypt_raw_json=bool(privacy.get("encrypt_raw_json", False)),
        passphrase_env=str(privacy.get("passphrase_env") or DEFAULT_PASSPHRASE_ENV),
    )


def is_encrypted_text(value: str | None) -> bool:
    return bool(value and value.startswith(ENCRYPTED_PREFIX))


def passphrase_from_env(config: dict[str, Any] | None = None, *, prompt: bool = False) -> str:
    privacy = privacy_config(config)
    value = os.environ.get(privacy.passphrase_env)
    if value:
        return value
    if prompt:
        value = getpass.getpass(f"{privacy.passphrase_env}: ")
        if value:
            return value
    raise EncryptionLocked(f"missing passphrase; set {privacy.passphrase_env}")


def encrypt_text(value: str, passphrase: str) -> str:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = derive_key(passphrase, salt)
    ciphertext = AESGCM(key).encrypt(nonce, value.encode("utf-8"), ENCRYPTED_PREFIX.encode("utf-8"))
    envelope = {
        "alg": "AES-256-GCM",
        "kdf": "PBKDF2-HMAC-SHA256",
        "iter": KDF_ITERATIONS,
        "salt": b64e(salt),
        "nonce": b64e(nonce),
        "ciphertext": b64e(ciphertext),
    }
    payload = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return ENCRYPTED_PREFIX + b64e(payload)


def decrypt_text(value: str, passphrase: str) -> str:
    if not is_encrypted_text(value):
        return value
    payload = value[len(ENCRYPTED_PREFIX) :]
    try:
        envelope = json.loads(b64d(payload).decode("utf-8"))
        salt = b64d(envelope["salt"])
        nonce = b64d(envelope["nonce"])
        ciphertext = b64d(envelope["ciphertext"])
        iterations = int(envelope.get("iter") or KDF_ITERATIONS)
        key = derive_key(passphrase, salt, iterations=iterations)
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, ENCRYPTED_PREFIX.encode("utf-8"))
        return plaintext.decode("utf-8")
    except (KeyError, ValueError, json.JSONDecodeError, InvalidTag) as exc:
        raise EncryptionError("could not decrypt encrypted payload") from exc


def protect_text(value: str | None, config: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    if is_encrypted_text(value):
        return value
    if not privacy_config(config).encrypt_raw_json:
        return value
    return encrypt_text(value, passphrase_from_env(config))


def reveal_text(value: str | None, config: dict[str, Any] | None = None) -> str | None:
    if value is None or not is_encrypted_text(value):
        return value
    return decrypt_text(value, passphrase_from_env(config))


def maybe_reveal_text(value: str | None, config: dict[str, Any] | None = None) -> str | None:
    if value is None:
        return None
    return reveal_text(value, config)


def derive_key(passphrase: str, salt: bytes, *, iterations: int = KDF_ITERATIONS) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(passphrase.encode("utf-8"))


def b64e(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def b64d(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)

