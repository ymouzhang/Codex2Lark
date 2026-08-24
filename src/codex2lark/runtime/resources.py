from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResourcePackage:
    package_id: str
    version: str
    instructions: tuple[str, ...] = ()
    policies: tuple[str, ...] = ()
    response_templates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.package_id or not self.version:
            raise ValueError("resource package identity is required")


@dataclass(frozen=True, slots=True)
class LoadedResources:
    versions: dict[str, str]
    instructions: tuple[str, ...]
    policies: tuple[str, ...]
    response_templates: tuple[str, ...]


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
