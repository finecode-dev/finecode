from __future__ import annotations

import dataclasses
import pathlib

import pytest

from fine_dist_artifacts.list_published_artifacts_action import (
    ListPublishedArtifactsAction,
    ListPublishedArtifactsRunPayload,
)
from fine_python_package_info.list_published_artifacts_python_handler import (
    ListPublishedArtifactsPythonHandler,
)
from finecode_extension_api.interfaces.ihttpclient import IHttpClient
from finecode_extension_api.interfaces.iprojectinfoprovider import IProjectInfoProvider
from finecode_extension_api.interfaces.irepositorycredentialsprovider import (
    IRepositoryCredentialsProvider,
)
from finecode_extension_api.interfaces.irepositorycredentialsprovider import Repository
from finecode_extension_runner.impls.repository_credentials_provider import (
    ConfigRepositoryCredentialsProvider,
    RepositoryCredentialsProviderConfig,
)
from finecode_extension_runner.testing import run_handler


def _repository_provider(
    name: str,
    index_url: str = "https://pypi.org/simple/",
    upload_url: str = "https://upload.pypi.org/legacy/",
) -> ConfigRepositoryCredentialsProvider:
    return ConfigRepositoryCredentialsProvider(
        RepositoryCredentialsProviderConfig(
            repositories=[Repository(name=name, index_url=index_url, upload_url=upload_url)]
        )
    )


class _StubProjectInfoProvider:
    """Reports a fixed raw config regardless of what's on disk at *project_def_path*.

    Stands in for the real provider's ``get_project_raw_config`` so tests can control
    ``project.name`` directly instead of relying on a real pyproject.toml on disk.
    """

    def __init__(self, raw_config: dict) -> None:
        self._raw_config = raw_config

    def get_current_project_dir_path(self) -> pathlib.Path:
        raise NotImplementedError

    def get_current_project_def_path(self) -> pathlib.Path:
        raise NotImplementedError

    async def get_current_project_package_name(self) -> str:
        raise NotImplementedError

    async def get_project_raw_config(self, project_def_path: pathlib.Path) -> dict:
        return self._raw_config

    async def get_current_project_raw_config(self) -> dict:
        raise NotImplementedError

    def get_current_project_raw_config_version(self) -> int:
        raise NotImplementedError

    async def get_workspace_editable_packages(self) -> dict[str, pathlib.Path]:
        raise NotImplementedError


def _project_info_provider(name: str = "pkg") -> _StubProjectInfoProvider:
    return _StubProjectInfoProvider({"project": {"name": name}})


@dataclasses.dataclass
class FakeHttpResponse:
    status_code: int
    payload: dict[str, object]

    def json(self) -> dict[str, object]:
        return self.payload

    def raise_for_status(self) -> None:
        return None


class FakeHttpSession:
    """Stands in for ``IHttpSession``: an async-context-managed session whose
    ``get`` returns the canned response, recording the URLs it was asked for."""

    def __init__(self, response: FakeHttpResponse) -> None:
        self._response = response
        self.requested_urls: list[str] = []

    async def __aenter__(self) -> "FakeHttpSession":
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        return None

    async def get(self, url: str, **kwargs: object) -> FakeHttpResponse:
        self.requested_urls.append(url)
        return self._response


class FakeHttpClient:
    """Stands in for ``IHttpClient``: a session factory, not a direct GET caller."""

    def __init__(self, response: FakeHttpResponse) -> None:
        self._session = FakeHttpSession(response)

    def session(self) -> FakeHttpSession:
        return self._session


@pytest.mark.asyncio
async def test_filenames_lists_registry_files_when_version_is_published() -> None:
    """When the registry already has files for the requested version, the action reports exactly those filenames without any local distribution paths being supplied, so a dry-run preview can ask the question before anything is built."""
    http_client = FakeHttpClient(
        FakeHttpResponse(
            status_code=200,
            payload={
                "versions": ["1.0.0", "1.1.0"],
                "files": [
                    {"filename": "pkg-1.0.0.tar.gz"},
                    {"filename": "pkg-1.0.0-py3-none-any.whl"},
                ],
            },
        )
    )
    payload = ListPublishedArtifactsRunPayload(
        src_artifact_def_path="pkg/pyproject.toml", version="1.0.0", registry_name="pypi"
    )

    result = await run_handler(
        ListPublishedArtifactsPythonHandler,
        payload,
        action_cls=ListPublishedArtifactsAction,
        service_overrides={
            IHttpClient: http_client,
            IProjectInfoProvider: _project_info_provider(),
            IRepositoryCredentialsProvider: _repository_provider("pypi"),
        },
    )

    assert set(result.filenames) == {"pkg-1.0.0.tar.gz", "pkg-1.0.0-py3-none-any.whl"}


@pytest.mark.asyncio
async def test_filenames_is_empty_when_version_is_absent_from_registry() -> None:
    """When the requested version is not among the registry's known versions, the action reports an empty filename list, which callers use as the unambiguous signal that the version is not published at all."""
    http_client = FakeHttpClient(
        FakeHttpResponse(status_code=200, payload={"versions": ["2.0.0"], "files": []})
    )
    payload = ListPublishedArtifactsRunPayload(
        src_artifact_def_path="pkg/pyproject.toml", version="1.0.0", registry_name="pypi"
    )

    result = await run_handler(
        ListPublishedArtifactsPythonHandler,
        payload,
        action_cls=ListPublishedArtifactsAction,
        service_overrides={
            IHttpClient: http_client,
            IProjectInfoProvider: _project_info_provider(),
            IRepositoryCredentialsProvider: _repository_provider("pypi"),
        },
    )

    assert result.filenames == []


@pytest.mark.asyncio
async def test_filenames_contains_only_the_registrys_actually_published_files() -> None:
    """When a version was only partially published (e.g. the wheel uploaded but not the sdist), the action reports exactly the files the registry holds — never the full set the caller intended to build — so a membership check against it correctly identifies the missing file."""
    http_client = FakeHttpClient(
        FakeHttpResponse(
            status_code=200,
            payload={
                "versions": ["2.0.0"],
                "files": [{"filename": "pkg-2.0.0-py3-none-any.whl"}],
            },
        )
    )
    payload = ListPublishedArtifactsRunPayload(
        src_artifact_def_path="pkg/pyproject.toml", version="2.0.0", registry_name="pypi"
    )

    result = await run_handler(
        ListPublishedArtifactsPythonHandler,
        payload,
        action_cls=ListPublishedArtifactsAction,
        service_overrides={
            IHttpClient: http_client,
            IProjectInfoProvider: _project_info_provider(),
            IRepositoryCredentialsProvider: _repository_provider("pypi"),
        },
    )

    assert result.filenames == ["pkg-2.0.0-py3-none-any.whl"]


@pytest.mark.asyncio
async def test_lookup_appends_the_package_to_the_index_url() -> None:
    """The index URL is a prefix that the package name is appended to, and the configured trailing slash must not be doubled, so that a registry whose index root is given either way is queried at exactly one well-formed URL."""
    http_client = FakeHttpClient(
        FakeHttpResponse(status_code=200, payload={"versions": [], "files": []})
    )
    payload = ListPublishedArtifactsRunPayload(
        src_artifact_def_path="pkg/pyproject.toml", version="1.0.0", registry_name="pypi"
    )

    await run_handler(
        ListPublishedArtifactsPythonHandler,
        payload,
        action_cls=ListPublishedArtifactsAction,
        service_overrides={
            IHttpClient: http_client,
            IProjectInfoProvider: _project_info_provider(),
            IRepositoryCredentialsProvider: _repository_provider(
                "pypi", index_url="https://pypi.org/simple/"
            ),
        },
    )

    assert http_client._session.requested_urls == ["https://pypi.org/simple/pkg/"]


@pytest.mark.asyncio
async def test_index_url_pointing_at_the_upload_host_fails_loudly() -> None:
    """An index lookup against the upload-only host would 404, and 404 is this action's signal for "nothing published" — so the misroute must raise rather than silently report a published version as absent and invite a redundant upload."""
    http_client = FakeHttpClient(FakeHttpResponse(status_code=404, payload={}))
    payload = ListPublishedArtifactsRunPayload(
        src_artifact_def_path="pkg/pyproject.toml", version="1.0.0", registry_name="pypi"
    )

    with pytest.raises(Exception) as exc_info:
        await run_handler(
            ListPublishedArtifactsPythonHandler,
            payload,
            action_cls=ListPublishedArtifactsAction,
            service_overrides={
                IHttpClient: http_client,
                IProjectInfoProvider: _project_info_provider(),
                IRepositoryCredentialsProvider: _repository_provider(
                    "pypi", index_url="https://upload.pypi.org/legacy/"
                ),
            },
        )

    assert "upload.pypi.org" in str(exc_info.value)
    assert http_client._session.requested_urls == []


@pytest.mark.asyncio
async def test_missing_project_name_fails_loudly_instead_of_guessing() -> None:
    """A config with no ``project.name`` must raise rather than fall back to guessing
    the distribution name from the artifact def path's parent directory -- a wrong
    guess could collide with an unrelated package on the registry and make this
    handler silently report someone else's files as already published."""
    http_client = FakeHttpClient(FakeHttpResponse(status_code=200, payload={}))
    payload = ListPublishedArtifactsRunPayload(
        src_artifact_def_path="pkg/pyproject.toml", version="1.0.0", registry_name="pypi"
    )

    with pytest.raises(Exception, match="project.name"):
        await run_handler(
            ListPublishedArtifactsPythonHandler,
            payload,
            action_cls=ListPublishedArtifactsAction,
            service_overrides={
                IHttpClient: http_client,
                IProjectInfoProvider: _StubProjectInfoProvider({}),
                IRepositoryCredentialsProvider: _repository_provider("pypi"),
            },
        )

    assert http_client._session.requested_urls == []
