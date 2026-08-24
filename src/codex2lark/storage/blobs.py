from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .crypto import EnvelopeCipher


class EncryptedBlobStore:
    def __init__(self, root: Path, cipher: EnvelopeCipher) -> None:
        self.root = root.resolve()
        self._cipher = cipher
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def put(self, content: bytes) -> str:
        blob_id = self._cipher.opaque_digest(content)
        target = self._path(blob_id)
        if target.exists():
            return blob_id
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(target.parent, 0o700)
        encrypted = self._cipher.encrypt(content, associated_data=self._aad(blob_id))
        descriptor, temporary_name = tempfile.mkstemp(prefix=".blob-", dir=target.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as temporary:
                temporary.write(encrypted)
                temporary.flush()
                os.fsync(temporary.fileno())
            try:
                os.link(temporary_name, target)
            except FileExistsError:
                pass
            else:
                os.chmod(target, 0o600)
        finally:
            Path(temporary_name).unlink(missing_ok=True)
        return blob_id

    def get(self, blob_id: str) -> bytes:
        self._validate_id(blob_id)
        encrypted = self._path(blob_id).read_bytes()
        return self._cipher.decrypt(encrypted, associated_data=self._aad(blob_id))

    def delete(self, blob_id: str) -> bool:
        self._validate_id(blob_id)
        path = self._path(blob_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def exists(self, blob_id: str) -> bool:
        self._validate_id(blob_id)
        return self._path(blob_id).is_file()

    def _path(self, blob_id: str) -> Path:
        self._validate_id(blob_id)
        return self.root / blob_id[:2] / f"{blob_id}.blob"

    @staticmethod
    def _validate_id(blob_id: str) -> None:
        if len(blob_id) != 64 or any(character not in "0123456789abcdef" for character in blob_id):
            raise ValueError("invalid blob id")

    @staticmethod
    def _aad(blob_id: str) -> bytes:
        return f"blob:{blob_id}:v1".encode()
