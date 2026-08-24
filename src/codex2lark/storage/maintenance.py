from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .locking import DataDirectoryLock
from .migrations import SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class StorageStatus:
    ok: bool
    integrity: str
    schema_version: int
    database_bytes: int
    blob_count: int
    blob_bytes: int
    task_states: dict[str, int]
    outbox_states: dict[str, int]


@dataclass(frozen=True, slots=True)
class BackupResult:
    ok: bool
    archive: str
    schema_version: int
    file_count: int
    total_bytes: int


class StorageMaintenance:
    def __init__(self, data_dir: Path) -> None:
        if not data_dir.is_absolute():
            raise ValueError("data directory must be absolute")
        self.data_dir = data_dir.resolve()
        self.database_path = self.data_dir / "runtime.db"
        self.blob_root = self.data_dir / "blobs"

    def status(self) -> StorageStatus:
        if not self.database_path.is_file():
            return StorageStatus(False, "database_missing", 0, 0, 0, 0, {}, {})
        with self._readonly(self.database_path) as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            schema_version = self._schema_version(connection)
            task_states = self._state_counts(connection, "runtime_tasks")
            outbox_states = self._state_counts(connection, "runtime_outbox")
            blob_ids = self._referenced_blob_ids(connection)
        blob_paths = [self._blob_path(self.blob_root, blob_id) for blob_id in blob_ids]
        present = [path for path in blob_paths if path.is_file()]
        blobs_complete = len(present) == len(blob_paths)
        return StorageStatus(
            ok=integrity == "ok" and blobs_complete and schema_version <= SCHEMA_VERSION,
            integrity=(integrity if blobs_complete else "referenced_blob_missing"),
            schema_version=schema_version,
            database_bytes=self.database_path.stat().st_size,
            blob_count=len(present),
            blob_bytes=sum(path.stat().st_size for path in present),
            task_states=task_states,
            outbox_states=outbox_states,
        )

    def backup(self, archive: Path) -> BackupResult:
        archive = self._absolute(archive, "backup archive")
        if archive.exists():
            raise FileExistsError(f"backup archive already exists: {archive}")
        if not self.database_path.is_file():
            raise FileNotFoundError(f"runtime database does not exist: {self.database_path}")
        archive.parent.mkdir(parents=True, exist_ok=True)
        with (
            DataDirectoryLock(self.data_dir),
            tempfile.TemporaryDirectory(prefix=".codex2lark-backup-", dir=archive.parent) as raw,
        ):
            staging = Path(raw)
            snapshot = staging / "runtime.db"
            with sqlite3.connect(self.database_path) as source, sqlite3.connect(snapshot) as target:
                source.backup(target)
            with self._readonly(snapshot) as connection:
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                if integrity != "ok":
                    raise RuntimeError(f"backup database integrity failed: {integrity}")
                schema_version = self._schema_version(connection)
                blob_ids = self._referenced_blob_ids(connection)

            files: dict[str, Path] = {"runtime.db": snapshot}
            for blob_id in blob_ids:
                source_blob = self._blob_path(self.blob_root, blob_id)
                if not source_blob.is_file():
                    raise RuntimeError(f"referenced encrypted blob is missing: {blob_id}")
                files[f"blobs/{blob_id[:2]}/{blob_id}.blob"] = source_blob
            manifest = {
                "format": "codex2lark-backup-v1",
                "created_at_ms": int(time.time() * 1000),
                "schema_version": schema_version,
                "files": {
                    name: {"bytes": path.stat().st_size, "sha256": self._digest(path)}
                    for name, path in sorted(files.items())
                },
            }
            total_bytes = sum(path.stat().st_size for path in files.values())
            temporary = staging / "backup.zip"
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as bundle:
                for name, path in files.items():
                    bundle.write(path, name)
                bundle.writestr(
                    "manifest.json",
                    json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                )
            os.chmod(temporary, 0o600)
            os.replace(temporary, archive)
        return BackupResult(
            True,
            str(archive),
            schema_version,
            len(files),
            total_bytes,
        )

    @classmethod
    def verify_backup(cls, archive: Path) -> BackupResult:
        archive = cls._absolute(archive, "backup archive")
        manifest = cls._verified_manifest(archive)
        cls._verify_archived_database(archive)
        files = manifest["files"]
        return BackupResult(
            True,
            str(archive),
            int(manifest["schema_version"]),
            len(files),
            sum(int(value["bytes"]) for value in files.values()),
        )

    @classmethod
    def restore(cls, archive: Path, target: Path) -> BackupResult:
        archive = cls._absolute(archive, "backup archive")
        target = cls._absolute(target, "restore data directory")
        manifest = cls._verified_manifest(archive)
        with DataDirectoryLock(target):
            if target.exists() and (not target.is_dir() or any(target.iterdir())):
                raise FileExistsError("restore data directory must be new or empty")
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".codex2lark-restore-", dir=target.parent))
            try:
                with zipfile.ZipFile(archive) as bundle:
                    for name in manifest["files"]:
                        destination = staging.joinpath(*PurePosixPath(name).parts)
                        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        with bundle.open(name) as source, destination.open("wb") as output:
                            shutil.copyfileobj(source, output)
                        os.chmod(destination, 0o600)
                with cls._readonly(staging / "runtime.db") as connection:
                    integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                    if integrity != "ok":
                        raise RuntimeError(f"restored database integrity failed: {integrity}")
                os.chmod(staging, 0o700)
                if target.exists():
                    target.rmdir()
                os.replace(staging, target)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        return BackupResult(
            True,
            str(target),
            int(manifest["schema_version"]),
            len(manifest["files"]),
            sum(int(value["bytes"]) for value in manifest["files"].values()),
        )

    @staticmethod
    def as_json(result: StorageStatus | BackupResult) -> str:
        return json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _absolute(path: Path, label: str) -> Path:
        if not path.is_absolute():
            raise ValueError(f"{label} must be an absolute path")
        return path.resolve()

    @staticmethod
    def _readonly(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _schema_version(connection: sqlite3.Connection) -> int:
        row = connection.execute("SELECT MAX(version) FROM runtime_migrations").fetchone()
        return int(row[0] or 0)

    @staticmethod
    def _state_counts(connection: sqlite3.Connection, table: str) -> dict[str, int]:
        if table not in {"runtime_tasks", "runtime_outbox"}:
            raise ValueError("unsupported state table")
        return {
            str(row["state"]): int(row["count"])
            for row in connection.execute(
                f"SELECT state, COUNT(*) AS count FROM {table} GROUP BY state"
            )
        }

    @staticmethod
    def _referenced_blob_ids(connection: sqlite3.Connection) -> tuple[str, ...]:
        rows = connection.execute(
            "SELECT DISTINCT blob_id FROM im_attachments WHERE blob_id IS NOT NULL"
        ).fetchall()
        values = tuple(sorted(str(row[0]) for row in rows))
        for value in values:
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise RuntimeError("database contains an invalid blob identifier")
        return values

    @staticmethod
    def _blob_path(root: Path, blob_id: str) -> Path:
        return root / blob_id[:2] / f"{blob_id}.blob"

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _verified_manifest(cls, archive: Path) -> dict[str, Any]:
        if not archive.is_file():
            raise FileNotFoundError(f"backup archive does not exist: {archive}")
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            if len(names) != len(set(names)) or "manifest.json" not in names:
                raise RuntimeError("backup archive has duplicate entries or no manifest")
            manifest = json.loads(bundle.read("manifest.json"))
            if not isinstance(manifest, dict) or manifest.get("format") != "codex2lark-backup-v1":
                raise RuntimeError("unsupported backup manifest")
            schema_version = manifest.get("schema_version")
            files = manifest.get("files")
            if not isinstance(schema_version, int) or schema_version > SCHEMA_VERSION:
                raise RuntimeError("backup schema is newer than this Codex2Lark version")
            if not isinstance(files, dict) or set(names) != {"manifest.json", *files}:
                raise RuntimeError("backup archive entries do not match the manifest")
            if "runtime.db" not in files:
                raise RuntimeError("backup manifest has no runtime database")
            for name, expected in files.items():
                cls._validate_archive_name(name)
                if not isinstance(expected, dict):
                    raise RuntimeError("backup file metadata is invalid")
                digest = hashlib.sha256()
                size = 0
                with bundle.open(name) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        size += len(chunk)
                        digest.update(chunk)
                if size != expected.get("bytes") or digest.hexdigest() != expected.get("sha256"):
                    raise RuntimeError(f"backup file verification failed: {name}")
            return manifest

    @staticmethod
    def _validate_archive_name(name: str) -> None:
        path = PurePosixPath(name)
        allowed_blob = (
            len(path.parts) == 3
            and path.parts[0] == "blobs"
            and len(path.parts[1]) == 2
            and path.parts[2].endswith(".blob")
            and path.parts[2][:-5].startswith(path.parts[1])
        )
        if path.is_absolute() or ".." in path.parts or (name != "runtime.db" and not allowed_blob):
            raise RuntimeError(f"unsafe or unsupported backup path: {name}")

    @classmethod
    def _verify_archived_database(cls, archive: Path) -> None:
        with tempfile.TemporaryDirectory(prefix="codex2lark-verify-") as raw:
            snapshot = Path(raw) / "runtime.db"
            with (
                zipfile.ZipFile(archive) as bundle,
                bundle.open("runtime.db") as source,
                snapshot.open("wb") as output,
            ):
                shutil.copyfileobj(source, output)
            with cls._readonly(snapshot) as connection:
                integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
                if integrity != "ok":
                    raise RuntimeError(f"backup database integrity failed: {integrity}")
