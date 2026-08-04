from __future__ import annotations

import pathlib

from finecode_extension_api.interfaces.irepositorycredentialsprovider import (
    IRepositoryCredentialsProvider,
)
from finecode_extension_runner import schemas
from finecode_extension_runner.impls.repository_credentials_provider import (
    ConfigRepositoryCredentialsProvider,
)
from finecode_extension_runner.testing import handler_test_session

_INTERFACE_SOURCE = (
    "finecode_extension_api.interfaces.irepositorycredentialsprovider"
    ".IRepositoryCredentialsProvider"
)
_IMPL_SOURCE = (
    "finecode_extension_runner.impls.repository_credentials_provider"
    ".ConfigRepositoryCredentialsProvider"
)


async def test_service_config_seeds_the_default_provider(
    tmp_path: pathlib.Path,
) -> None:
    """A ``[[tool.finecode.service]]`` declaration for ``IRepositoryCredentialsProvider``
    is observable through the interface's read-only getters -- no init action
    required (ADR-0068)."""
    declaration = schemas.ServiceDeclaration(
        interface=_INTERFACE_SOURCE,
        source=_IMPL_SOURCE,
        config={
            "repositories": [
                {
                    "name": "testpypi",
                    "index_url": "https://test.pypi.org/simple/",
                    "upload_url": "https://test.pypi.org/legacy/",
                },
            ],
            "credentials_by_repository": {
                "testpypi": {"username": "__token__", "password": "pypi-token"},
            },
        },
    )

    async with handler_test_session(
        project_dir=tmp_path,
        actions={},
        service_declarations=[declaration],
    ) as session:
        provider = await session.service(IRepositoryCredentialsProvider)

        repository = provider.get_repository("testpypi")
        assert repository is not None
        assert repository.index_url == "https://test.pypi.org/simple/"
        assert repository.upload_url == "https://test.pypi.org/legacy/"

        credentials = provider.get_credentials("testpypi")
        assert credentials is not None
        assert credentials.username == "__token__"
        assert credentials.password == "pypi-token"


async def test_readers_and_concrete_injected_seeding_handler_share_one_instance(
    tmp_path: pathlib.Path,
) -> None:
    """Load-bearing detail for the optional dynamic-runtime-seeding action
    (ADR-0068): the provider must be registered so that
    resolving through the interface (the read-only consumers) and resolving
    through the concrete class (the seeding handler's constructor injection)
    return the SAME instance -- otherwise a seed written through the concrete
    type would be invisible to readers going through the interface."""
    async with handler_test_session(
        project_dir=tmp_path,
        actions={},
        service_declarations=[],
    ) as session:
        via_interface = await session.service(IRepositoryCredentialsProvider)
        via_concrete_type = await session.service(ConfigRepositoryCredentialsProvider)

        assert via_interface is via_concrete_type

        # A write through the concrete type (what the seeding handler does) must
        # be observable through the interface (what the readers do).
        via_concrete_type.add_repository(
            "pypi", "https://pypi.org/simple/", "https://upload.pypi.org/legacy/"
        )
        assert via_interface.get_repository("pypi") is via_concrete_type.get_repository(
            "pypi"
        )


async def test_env_override_reaches_the_provider_without_touching_declared_siblings(
    tmp_path: pathlib.Path,
) -> None:
    """The credential case the override format exists for: a password supplied
    from the environment reaches the provider, while the username and every
    other repository declared in config survive (ADR-0070)."""
    declaration = schemas.ServiceDeclaration(
        interface=_INTERFACE_SOURCE,
        source=_IMPL_SOURCE,
        config={
            "repositories": [
                {
                    "name": "testpypi",
                    "index_url": "https://test.pypi.org/simple/",
                    "upload_url": "https://test.pypi.org/legacy/",
                },
            ],
            "credentials_by_repository": {
                "testpypi": {"username": "__token__", "password": "placeholder"},
            },
        },
    )

    async with handler_test_session(
        project_dir=tmp_path,
        actions={},
        service_declarations=[declaration],
        service_config_overrides={
            "repository_credentials_provider": {
                "credentials_by_repository": {"testpypi": {"password": "from-env"}}
            }
        },
    ) as session:
        provider = await session.service(IRepositoryCredentialsProvider)

        credentials = provider.get_credentials("testpypi")
        assert credentials is not None
        assert credentials.password == "from-env"
        # Sibling field and the repository definition are untouched.
        assert credentials.username == "__token__"
        assert provider.get_repository("testpypi") is not None


async def test_config_only_entry_configures_a_binding_it_does_not_declare(
    tmp_path: pathlib.Path,
) -> None:
    """An entry with no ``source`` carries config for a binding the runner's own
    bootstrap owns, without restating (or pinning) the implementation."""
    config_only = schemas.ServiceDeclaration(
        interface=_INTERFACE_SOURCE,
        config={
            "repositories": [
                {
                    "name": "pypi",
                    "index_url": "https://pypi.org/simple/",
                    "upload_url": "https://upload.pypi.org/legacy/",
                },
            ],
        },
    )

    async with handler_test_session(
        project_dir=tmp_path,
        actions={},
        service_declarations=[config_only],
    ) as session:
        provider = await session.service(IRepositoryCredentialsProvider)

        repository = provider.get_repository("pypi")
        assert repository is not None
        assert repository.index_url == "https://pypi.org/simple/"
