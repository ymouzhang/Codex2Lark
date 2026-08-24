from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from codex2lark.core.models import Identity
from codex2lark.storage.crypto import MasterKey


def resolve_data_dir(environment: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environment is None else environment
    state_root = values.get("XDG_STATE_HOME")
    default_dir = (
        Path(state_root).expanduser() if state_root else Path.home() / ".local" / "state"
    ) / "codex2lark"
    data_dir = Path(values.get("CODEX2LARK_DATA_DIR", str(default_dir))).expanduser()
    return data_dir if data_dir.is_absolute() else (Path.cwd() / data_dir).resolve()


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    feishu_app_id: str
    feishu_app_secret: str = field(repr=False)
    openai_api_key: str = field(repr=False)
    model: str
    master_key: MasterKey = field(repr=False)
    data_dir: Path
    authoring_identity: Identity = Identity.USER
    openai_base_url: str | None = None
    poll_interval_ms: int = 200
    task_concurrency: int = 4

    def __post_init__(self) -> None:
        required = (
            self.feishu_app_id,
            self.feishu_app_secret,
            self.openai_api_key,
            self.model,
        )
        if any(not value.strip() for value in required):
            raise ValueError("Gateway credentials and model are required")
        if self.poll_interval_ms < 10 or self.task_concurrency < 1:
            raise ValueError("Gateway worker configuration is invalid")
        if not self.data_dir.is_absolute():
            raise ValueError("CODEX2LARK_DATA_DIR must be an absolute path")

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> GatewayConfig:
        values = os.environ if environment is None else environment
        data_dir = resolve_data_dir(values)
        return cls(
            feishu_app_id=cls._required(values, "CODEX2LARK_FEISHU_APP_ID"),
            feishu_app_secret=cls._required(values, "CODEX2LARK_FEISHU_APP_SECRET"),
            openai_api_key=cls._required(values, "OPENAI_API_KEY"),
            model=cls._required(values, "CODEX2LARK_MODEL"),
            master_key=MasterKey.from_base64(
                key_id=cls._required(values, "CODEX2LARK_MASTER_KEY_ID"),
                encoded_key=cls._required(values, "CODEX2LARK_MASTER_KEY_BASE64"),
            ),
            data_dir=data_dir,
            authoring_identity=Identity(values.get("CODEX2LARK_AUTHORING_IDENTITY", "user")),
            openai_base_url=values.get("OPENAI_BASE_URL") or None,
            poll_interval_ms=cls._integer(values, "CODEX2LARK_POLL_INTERVAL_MS", 200),
            task_concurrency=cls._integer(values, "CODEX2LARK_TASK_CONCURRENCY", 4),
        )

    @staticmethod
    def _required(values: Mapping[str, str], name: str) -> str:
        value = values.get(name, "").strip()
        if not value:
            raise ValueError(f"required environment variable is missing: {name}")
        return value

    @staticmethod
    def _integer(values: Mapping[str, str], name: str, default: int) -> int:
        raw = values.get(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"environment variable must be an integer: {name}") from exc
