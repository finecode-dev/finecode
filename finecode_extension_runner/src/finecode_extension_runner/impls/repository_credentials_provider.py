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


class ConfigRepositoryCredentialsProvider(IRepositoryCredentialsProvider):
    """
    Repository credentials provider that stores credentials and repositories in memory.
    """

    def __init__(self) -> None:
        self._credentials_by_repository: dict[str, RepositoryCredentials] = {}
        self._repositories: dict[str, Repository] = {}

    @override
    def get_credentials(self, repository_name: str) -> RepositoryCredentials | None:
        return self._credentials_by_repository.get(repository_name)

    @override
    def set_credentials(
        self, repository_name: str, username: str, password: str
    ) -> None:
        self._credentials_by_repository[repository_name] = RepositoryCredentials(
            username=username, password=password
        )

    @override
    def add_repository(self, name: str, url: str) -> None:
        self._repositories[name] = Repository(name=name, url=url)

    @override
    def get_repository(self, name: str) -> Repository | None:
        return self._repositories.get(name)

    @override
    def get_all_repositories(self) -> list[Repository]:
        return list(self._repositories.values())
