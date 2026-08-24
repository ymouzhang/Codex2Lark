from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any


@dataclass(frozen=True, slots=True)
class ResourcePackage:
    package_id: str
    version: str
    instructions: tuple[str, ...] = ()
    policies: tuple[str, ...] = ()
    response_templates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9-]*", self.package_id)
            or not re.fullmatch(r"[1-9]\d*\.\d+\.\d+", self.version)
            or not self.instructions
            or not self.policies
        ):
            raise ValueError("resource package identity is required")


@dataclass(frozen=True, slots=True)
class LoadedResources:
    versions: dict[str, str]
    instructions: tuple[str, ...]
    policies: tuple[str, ...]
    response_templates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IMTemplateResources:
    bundle_id: str
    version: int
    acknowledgement: str
    progress_started: str
    completed_suffix: str
    blocked_suffix: str
    failed_suffix: str
    cancelled_suffix: str


class ResourceLoader:
    def __init__(self, packages: list[ResourcePackage]) -> None:
        self._packages: dict[str, ResourcePackage] = {}
        for package in packages:
            if package.package_id in self._packages:
                raise ValueError(f"duplicate resource package: {package.package_id}")
            self._packages[package.package_id] = package

    def load(self, package_ids: tuple[str, ...]) -> LoadedResources:
        packages: list[ResourcePackage] = []
        for package_id in package_ids:
            package = self._packages.get(package_id)
            if package is None:
                raise LookupError(f"resource package is unavailable: {package_id}")
            packages.append(package)
        return LoadedResources(
            versions={package.package_id: package.version for package in packages},
            instructions=tuple(item for package in packages for item in package.instructions),
            policies=tuple(item for package in packages for item in package.policies),
            response_templates=tuple(
                item for package in packages for item in package.response_templates
            ),
        )

    @classmethod
    def from_package(cls, package: str) -> ResourceLoader:
        root = files(package)
        agents = root.joinpath("agents")
        packages = [cls._resource_package(item) for item in cls._json_files(agents)]
        if not packages:
            raise ValueError("bundled Agent resource package set is empty")
        return cls(packages)

    @classmethod
    def load_im_templates(cls, package: str, locale: str) -> IMTemplateResources:
        if not locale or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-"
            for character in locale
        ):
            raise ValueError("IM template locale is invalid")
        value = cls._json_object(files(package).joinpath("im", f"{locale}.json"))
        expected = {
            "bundle_id",
            "version",
            "acknowledgement",
            "progress_started",
            "completed_suffix",
            "blocked_suffix",
            "failed_suffix",
            "cancelled_suffix",
        }
        if set(value) != expected:
            raise ValueError("IM template bundle fields are invalid")
        strings = {key: value[key] for key in expected - {"version"}}
        if any(not isinstance(item, str) or not item.strip() for item in strings.values()):
            raise ValueError("IM template strings must be non-empty")
        version = value["version"]
        if not isinstance(version, int) or version < 1:
            raise ValueError("IM template version must be positive")
        return IMTemplateResources(
            bundle_id=strings["bundle_id"],
            version=version,
            acknowledgement=strings["acknowledgement"],
            progress_started=strings["progress_started"],
            completed_suffix=strings["completed_suffix"],
            blocked_suffix=strings["blocked_suffix"],
            failed_suffix=strings["failed_suffix"],
            cancelled_suffix=strings["cancelled_suffix"],
        )

    @classmethod
    def _resource_package(cls, resource: Traversable) -> ResourcePackage:
        value = cls._json_object(resource)
        expected = {
            "package_id",
            "version",
            "instructions",
            "policies",
            "response_templates",
        }
        if set(value) != expected:
            raise ValueError(f"resource package fields are invalid: {resource.name}")
        package_id = value["package_id"]
        version = value["version"]
        if not isinstance(package_id, str) or not isinstance(version, str):
            raise ValueError("resource package identity must be text")
        return ResourcePackage(
            package_id,
            version,
            cls._string_tuple(value["instructions"], "instructions"),
            cls._string_tuple(value["policies"], "policies"),
            cls._string_tuple(value["response_templates"], "response_templates"),
        )

    @staticmethod
    def _json_files(root: Traversable) -> tuple[Traversable, ...]:
        if not root.is_dir():
            raise ValueError("bundled resource directory is missing")
        return tuple(
            sorted(
                (item for item in root.iterdir() if item.is_file() and item.name.endswith(".json")),
                key=lambda item: item.name,
            )
        )

    @staticmethod
    def _json_object(resource: Traversable) -> dict[str, Any]:
        try:
            value = json.loads(resource.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise ValueError(f"bundled resource is invalid: {resource.name}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"bundled resource must be an object: {resource.name}")
        return value

    @staticmethod
    def _string_tuple(value: object, field: str) -> tuple[str, ...]:
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            raise ValueError(f"resource package {field} must contain non-empty strings")
        return tuple(value)
