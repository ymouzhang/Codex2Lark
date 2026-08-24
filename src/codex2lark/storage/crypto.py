from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_ALGORITHM = "AES-256-GCM"
_ENVELOPE_VERSION = 1
_NONCE_BYTES = 12
_DATA_KEY_BYTES = 32


@dataclass(frozen=True, slots=True)
class MasterKey:
    key_id: str
    key: bytes

    def __post_init__(self) -> None:
        if not self.key_id:
            raise ValueError("master key id must be non-empty")
        if len(self.key) != _DATA_KEY_BYTES:
            raise ValueError("master key must contain exactly 32 bytes")

    @classmethod
    def from_base64(cls, *, key_id: str, encoded_key: str) -> MasterKey:
        try:
            key = base64.b64decode(encoded_key, validate=True)
        except ValueError as exc:
            raise ValueError("master key must be valid base64") from exc
        return cls(key_id=key_id, key=key)


class EnvelopeCipher:
    def __init__(self, master_key: MasterKey) -> None:
        self._master_key = master_key

    @property
    def key_id(self) -> str:
        return self._master_key.key_id

    def encrypt(self, plaintext: bytes, *, associated_data: bytes) -> bytes:
        data_key = os.urandom(_DATA_KEY_BYTES)
        data_nonce = os.urandom(_NONCE_BYTES)
        wrap_nonce = os.urandom(_NONCE_BYTES)
        ciphertext = AESGCM(data_key).encrypt(data_nonce, plaintext, associated_data)
        wrapped_key = AESGCM(self._master_key.key).encrypt(
            wrap_nonce,
            data_key,
            self._wrapping_aad(associated_data),
        )
        envelope = {
            "version": _ENVELOPE_VERSION,
            "algorithm": _ALGORITHM,
            "key_id": self._master_key.key_id,
            "data_nonce": self._encode(data_nonce),
            "wrap_nonce": self._encode(wrap_nonce),
            "wrapped_key": self._encode(wrapped_key),
            "ciphertext": self._encode(ciphertext),
        }
        return json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def decrypt(self, envelope_bytes: bytes, *, associated_data: bytes) -> bytes:
        try:
            envelope = json.loads(envelope_bytes)
            if envelope["version"] != _ENVELOPE_VERSION:
                raise ValueError("unsupported encrypted envelope version")
            if envelope["algorithm"] != _ALGORITHM:
                raise ValueError("unsupported encrypted envelope algorithm")
            if envelope["key_id"] != self._master_key.key_id:
                raise ValueError("encrypted envelope requires a different master key")
            data_key = AESGCM(self._master_key.key).decrypt(
                self._decode(envelope["wrap_nonce"]),
                self._decode(envelope["wrapped_key"]),
                self._wrapping_aad(associated_data),
            )
            return AESGCM(data_key).decrypt(
                self._decode(envelope["data_nonce"]),
                self._decode(envelope["ciphertext"]),
                associated_data,
            )
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid encrypted envelope") from exc

    def opaque_digest(self, content: bytes) -> str:
        return hmac.new(self._master_key.key, content, hashlib.sha256).hexdigest()

    def _wrapping_aad(self, associated_data: bytes) -> bytes:
        digest = hashlib.sha256(associated_data).digest()
        return b"codex2lark:keywrap:v1:" + self._master_key.key_id.encode("utf-8") + b":" + digest

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.b64encode(value).decode("ascii")

    @staticmethod
    def _decode(value: object) -> bytes:
        if not isinstance(value, str):
            raise ValueError("encrypted envelope field must be text")
        try:
            return base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise ValueError("encrypted envelope field must be valid base64") from exc
