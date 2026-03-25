import dataclasses
from typing import Protocol


@dataclasses.dataclass
class RepositoryCredentials:
    username: str
    password: str


@dataclasses.dataclass
class Repository:
    name: str
    url: str


class IRepositoryCredentialsProvider(Protocol):
    def get_credentials(self, repository_name: str) -> RepositoryCredentials | None:
        """
        Get credentials for a repository by name.

        Args:
            repository_name: The name of the repository (e.g., "testpypi", "pypi")

        Returns:
            RepositoryCredentials if found, None otherwise
        """
        ...

    def set_credentials(
        self, repository_name: str, username: str, password: str
    ) -> None:
        """
        Store credentials for a repository.

        Args:
            repository_name: The name of the repository
            username: The username for authentication
            password: The password or token for authentication
        """
        ...

    def add_repository(self, name: str, url: str) -> None:
        """
        Add a repository.

        Args:
            name: The name of the repository (e.g., "testpypi", "pypi")
            url: The URL of the repository
        """
        ...

    def get_repository(self, name: str) -> Repository | None:
        """
        Get a repository by name.

        Args:
            name: The name of the repository

        Returns:
            Repository if found, None otherwise
        """
        ...

    def get_all_repositories(self) -> list[Repository]:
        """
        Get all registered repositories.

        Returns:
            List of all repositories
        """
        ...
