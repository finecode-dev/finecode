import dataclasses
import sys

if sys.version_info < (3, 12):
    from typing_extensions import override
else:
    from typing import override


from finecode_extension_api.interfaces.irepositorycredentialsprovider import (
    IRepositoryCredentialsProvider,
    Repository,
    RepositoryCredentials,
)


@dataclasses.dataclass
class RepositoryCredentialsProviderConfig:
    repositories: list[Repository] = dataclasses.field(default_factory=list)
    credentials_by_repository: dict[str, RepositoryCredentials] = dataclasses.field(
        default_factory=dict
    )


class ConfigRepositoryCredentialsProvider(IRepositoryCredentialsProvider):
    """
    Repository credentials provider that stores credentials and repositories in memory.

    Seeded statically from ``config``.
    ``add_repository``/``set_credentials`` remain for the optional dynamic-seeding
    action (``init_repository_provider``) and are concrete-only -- they are not
    part of ``IRepositoryCredentialsProvider``.
    """

    def __init__(self, config: RepositoryCredentialsProviderConfig) -> None:
        self._repositories: dict[str, Repository] = {
            repository.name: repository for repository in config.repositories
        }
        self._credentials_by_repository: dict[str, RepositoryCredentials] = dict(
            config.credentials_by_repository
        )

    @override
    def get_credentials(self, repository_name: str) -> RepositoryCredentials | None:
        return self._credentials_by_repository.get(repository_name)

    def set_credentials(
        self, repository_name: str, username: str, password: str
    ) -> None:
        self._credentials_by_repository[repository_name] = RepositoryCredentials(
            username=username, password=password
        )

    def add_repository(self, name: str, index_url: str, upload_url: str) -> None:
        self._repositories[name] = Repository(
            name=name, index_url=index_url, upload_url=upload_url
        )

    @override
    def get_repository(self, name: str) -> Repository | None:
        return self._repositories.get(name)

    @override
    def get_all_repositories(self) -> list[Repository]:
        return list(self._repositories.values())
