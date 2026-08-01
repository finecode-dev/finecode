import dataclasses
from typing import Protocol


@dataclasses.dataclass
class RepositoryCredentials:
    username: str
    password: str


@dataclasses.dataclass
class Repository:
    """
    A package registry, addressed by its two endpoints.

    Reading the index and uploading are separate APIs. Most registries
    (Artifactory, Nexus, devpi, GitLab) serve both from one host and differ only
    by path, but PyPI splits them across hosts: the index lives on pypi.org and
    uploads go to upload.pypi.org. Both endpoints are therefore given
    explicitly, never derived from one another.

    Both URLs are complete, but they are *not* used the same way:

    - ``index_url`` is a prefix. Callers append what they are looking up, so a
      package lookup against ``https://pypi.org/simple/`` requests
      ``https://pypi.org/simple/<package>/``. A trailing slash is optional.
    - ``upload_url`` is terminal. It is the endpoint itself and is used
      verbatim, e.g. ``https://upload.pypi.org/legacy/``.

    Both values match what the ecosystem's own tools already use --
    ``index_url`` is pip's ``index-url``, ``upload_url`` is twine's
    ``repository``.
    """

    name: str
    index_url: str
    upload_url: str


class IRepositoryCredentialsProvider(Protocol):
    """
    Read-only contract for supplying package-registry definitions and
    credentials to the artifact-publishing handlers.

    How an implementation is provisioned with data is its own concern, not
    part of this interface (ADR-0068): the default implementation
    (``ConfigRepositoryCredentialsProvider``) is provisioned via
    ``[[tool.finecode.service]]`` config; a pull-based implementation (Vault,
    keyring, a CI secret store) is free to fetch its data on demand instead,
    with no push-seed API to fake.
    """

    def get_credentials(self, repository_name: str) -> RepositoryCredentials | None:
        """
        Get credentials for a repository by name.

        Args:
            repository_name: The name of the repository (e.g., "testpypi", "pypi")

        Returns:
            RepositoryCredentials if found, None otherwise
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
