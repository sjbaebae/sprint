from pathlib import Path

from sprint.constants import DEFAULT_REGISTRY_URL
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sprint.registry.client.base import BaseRegistryClient


class RegistryClientFactory:
    @staticmethod
    def create(
        registry_url: str | None = None,
        registry_path: Path | None = None,
        repo: str | None = None,
        path: Path | None = None,
    ) -> "BaseRegistryClient":
        if repo is not None:
            from sprint.registry.client.git_repo import (
                GitRepoRegistryClient,
                resolve_repo_source,
            )

            return GitRepoRegistryClient(
                resolve_repo_source(repo),
                path=path,
                registry_path=registry_path,
            )
        if registry_path is not None:
            from sprint.registry.client.json import JsonRegistryClient

            return JsonRegistryClient(path=registry_path)
        elif registry_url is not None:
            if registry_url == DEFAULT_REGISTRY_URL:
                from sprint.registry.client.harbor.harbor import HarborRegistryClient

                return HarborRegistryClient()
            else:
                from sprint.registry.client.json import JsonRegistryClient

                return JsonRegistryClient(url=registry_url)
        else:
            from sprint.registry.client.harbor.harbor import HarborRegistryClient

            return HarborRegistryClient()
