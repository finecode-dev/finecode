from __future__ import annotations

import dataclasses
import pathlib
import typing

import pytest
from fine_dist_artifacts.list_published_artifacts_action import (
    ListPublishedArtifactsAction,
    ListPublishedArtifactsRunPayload,
    ListPublishedArtifactsRunResult,
)
from finecode_extension_api.interfaces import idataclasscodec
from finecode_extension_api.interfaces.idataclasscodec import IDataclassCodec
from finecode_extension_api.interfaces.ihttpclient import IHttpClient
from finecode_extension_api.interfaces.iprojectinfoprovider import (
    IProjectInfoProvider,
    WorkspacePackage,
)
from finecode_extension_api.interfaces.irepositorycredentialsprovider import (
    IRepositoryCredentialsProvider,
    Repository,
)
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.impls.dataclass_codec import DataclassCodec
from finecode_extension_runner.impls.repository_credentials_provider import (
    ConfigRepositoryCredentialsProvider,
    RepositoryCredentialsProviderConfig,
)
from finecode_extension_runner.testing import handler_test_session, run_handler

from fine_python_package_info.list_published_artifacts_python_handler import (
    ListPublishedArtifactsPythonHandler,
)

_DEF_URI = path_to_resource_uri(
    pathlib.Path(__file__).parent / "pkg" / "pyproject.toml"
)


def _repository_provider(
    name: str,
    index_url: str = "https://pypi.org/simple/",
    upload_url: str = "https://upload.pypi.org/legacy/",
) -> ConfigRepositoryCredentialsProvider:
    return ConfigRepositoryCredentialsProvider(
        RepositoryCredentialsProviderConfig(
            repositories=[
                Repository(name=name, index_url=index_url, upload_url=upload_url)
            ]
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

    async def get_workspace_packages(self) -> dict[str, WorkspacePackage]:
        raise NotImplementedError


def _project_info_provider(name: str = "pkg") -> _StubProjectInfoProvider:
    return _StubProjectInfoProvider({"project": {"name": name}})


@dataclasses.dataclass
class FakeHttpResponse:
    status_code: int
    payload: object
    json_calls: int = 0
    json_error: Exception | None = None

    def json(self) -> object:
        self.json_calls += 1
        if self.json_error is not None:
            raise self.json_error
        return self.payload

    def raise_for_status(self) -> None:
        return None


class FakeHttpSession:
    """Stands in for ``IHttpSession``: an async-context-managed session whose
    ``get`` returns the canned response, recording the URLs it was asked for."""

    def __init__(self, response: FakeHttpResponse) -> None:
        self._response = response
        self.requested_urls: list[str] = []

    async def __aenter__(self) -> FakeHttpSession:
        return self

    async def __aexit__(
        self, exc_type: object, exc_val: object, exc_tb: object
    ) -> None:
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
        src_artifact_def_path=_DEF_URI,
        version="1.0.0",
        registry_name="pypi",
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
        src_artifact_def_path=_DEF_URI,
        version="1.0.0",
        registry_name="pypi",
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
        src_artifact_def_path=_DEF_URI,
        version="2.0.0",
        registry_name="pypi",
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
        src_artifact_def_path=_DEF_URI,
        version="1.0.0",
        registry_name="pypi",
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
        src_artifact_def_path=_DEF_URI,
        version="1.0.0",
        registry_name="pypi",
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
        src_artifact_def_path=_DEF_URI,
        version="1.0.0",
        registry_name="pypi",
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


async def _run_list(
    http_client: FakeHttpClient,
    payload: ListPublishedArtifactsRunPayload,
    provider: _StubProjectInfoProvider | None = None,
) -> ListPublishedArtifactsRunResult | None:
    result = await run_handler(
        ListPublishedArtifactsPythonHandler,
        payload,
        action_cls=ListPublishedArtifactsAction,
        service_overrides={
            IHttpClient: http_client,
            IProjectInfoProvider: provider or _project_info_provider(),
            IRepositoryCredentialsProvider: _repository_provider("pypi"),
        },
    )
    if result is None:
        return None
    return typing.cast(ListPublishedArtifactsRunResult, result)


def _payload(version: str = "1.0.0") -> ListPublishedArtifactsRunPayload:
    return ListPublishedArtifactsRunPayload(
        src_artifact_def_path=_DEF_URI,
        version=version,
        registry_name="pypi",
    )


def _provider_for(name: str) -> _StubProjectInfoProvider:
    return _project_info_provider(name)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 500])
async def test_non_success_status_rejected_before_decoding(status: int) -> None:
    """A non-success listing status must fail the version check so a transient registry error is never mistaken for an unpublished version."""
    response = FakeHttpResponse(
        status_code=status,
        payload={
            "files": [{"filename": "pkg-1.0.0.tar.gz"}],
        },
    )
    http_client = FakeHttpClient(response)
    with pytest.raises(Exception):
        await _run_list(http_client, _payload())
    assert response.json_calls == 0


@pytest.mark.asyncio
async def test_non_json_body_fails() -> None:
    """A 200 response whose body is not JSON must fail loudly so callers do not treat a proxy error page as an empty listing."""
    response = FakeHttpResponse(
        status_code=200, payload={}, json_error=ValueError("not json")
    )
    http_client = FakeHttpClient(response)
    with pytest.raises(Exception):
        await _run_list(http_client, _payload())
    assert response.json_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"versions": []},
        [],
        "files",
        {"files": "pkg-1.0.0.tar.gz"},
        {"files": ["pkg-1.0.0.tar.gz"]},
        {"files": [{}]},
        {"files": [{"filename": 42}]},
        {"files": [{"filename": 42, "size": 1}]},
        {"files": [{"filename": "README.txt"}, {"filename": None}]},
    ],
)
async def test_malformed_bodies_fail_before_filtering(body: object) -> None:
    """Malformed listing bodies must fail the check instead of silently reporting an unpublished version and triggering a duplicate upload."""
    http_client = FakeHttpClient(FakeHttpResponse(status_code=200, payload=body))
    with pytest.raises(Exception) as exc_info:
        await _run_list(http_client, _payload())
    message = str(exc_info.value)
    assert "Registry 'pypi'" in message
    assert "https://pypi.org/simple/pkg/" in message
    assert "malformed listing" in message


@pytest.mark.asyncio
async def test_malformed_entry_after_nonmatching_filename_still_fails() -> None:
    """Shape validation must cover every entry so a malformed record hiding behind a foreign filename cannot slip through filtering."""
    http_client = FakeHttpClient(
        FakeHttpResponse(
            status_code=200,
            payload={
                "files": [
                    {"filename": "other-9.9.9.tar.gz"},
                    {"no-filename": True},
                ]
            },
        )
    )
    with pytest.raises(Exception) as exc_info:
        await _run_list(http_client, _payload())
    message = str(exc_info.value)
    assert "Registry 'pypi'" in message
    assert "https://pypi.org/simple/pkg/" in message
    assert "malformed listing" in message


@pytest.mark.asyncio
async def test_membership_filters_by_canonical_name_and_version() -> None:
    """Only files for the requested package and version count, so foreign packages and stale versions never mark a pending upload as done."""
    http_client = FakeHttpClient(
        FakeHttpResponse(
            status_code=200,
            payload={
                "files": [
                    {"filename": "pkg-1.0.0-py3-none-any.whl"},
                    {"filename": "pkg-1.0.0.tar.gz"},
                    {"filename": "pkg-1.0.0.zip"},
                    {"filename": "other-1.0.0.tar.gz"},
                    {"filename": "pkg-2.0.0.tar.gz"},
                    {"filename": "not-a-distribution.txt"},
                ]
            },
        )
    )
    result = await _run_list(http_client, _payload())
    assert result is not None
    assert list(result.filenames) == [
        "pkg-1.0.0-py3-none-any.whl",
        "pkg-1.0.0.tar.gz",
        "pkg-1.0.0.zip",
    ]


@pytest.mark.asyncio
async def test_equivalent_versions_and_normalized_names_match() -> None:
    """Equivalent version spellings and underscore/dash name variants identify the same release, so a re-published spelling is not uploaded twice."""
    http_client = FakeHttpClient(
        FakeHttpResponse(
            status_code=200,
            payload={
                "files": [{"filename": "my_pkg-1.0.0.tar.gz"}],
            },
        )
    )
    payload = ListPublishedArtifactsRunPayload(
        src_artifact_def_path=_DEF_URI,
        version="1.0",
        registry_name="pypi",
    )
    result = await run_handler(
        ListPublishedArtifactsPythonHandler,
        payload,
        action_cls=ListPublishedArtifactsAction,
        service_overrides={
            IHttpClient: http_client,
            IProjectInfoProvider: _provider_for("my-pkg"),
            IRepositoryCredentialsProvider: _repository_provider("pypi"),
        },
    )
    assert result is not None
    assert list(typing.cast(ListPublishedArtifactsRunResult, result).filenames) == [
        "my_pkg-1.0.0.tar.gz"
    ]


@pytest.mark.asyncio
async def test_versions_key_is_ignored() -> None:
    """The legacy versions gate no longer decides membership, so a stale or missing versions list cannot hide files the index actually holds."""
    for body in (
        {"files": [{"filename": "pkg-1.0.0.tar.gz"}]},
        {
            "versions": typing.cast(object, []),
            "files": [{"filename": "pkg-1.0.0.tar.gz"}],
        },
        {"versions": ["9.9.9"], "files": [{"filename": "pkg-1.0.0.tar.gz"}]},
        {"versions": "1.0.0", "files": [{"filename": "pkg-1.0.0.tar.gz"}]},
        {
            "versions": typing.cast(object, 42),
            "files": [{"filename": "pkg-1.0.0.tar.gz"}],
        },
        {
            "versions": typing.cast(object, {"x": 1}),
            "files": [{"filename": "pkg-1.0.0.tar.gz"}],
        },
    ):
        http_client = FakeHttpClient(FakeHttpResponse(status_code=200, payload=body))
        result = await _run_list(http_client, _payload())
        assert result is not None
        assert list(result.filenames) == ["pkg-1.0.0.tar.gz"]


@pytest.mark.asyncio
async def test_extra_listing_and_file_metadata_is_ignored() -> None:
    """Registry metadata around and inside file entries must not hide the files.

    The index adds per-file hashes and root metadata the version check never
    reads; rejecting them would report a published version as absent and
    invite a duplicate upload."""
    http_client = FakeHttpClient(
        FakeHttpResponse(
            status_code=200,
            payload={
                "meta": {"api-version": "1.1"},
                "versions": {"x": 1},
                "files": [
                    {
                        "filename": "pkg-1.0.0.tar.gz",
                        "size": 1,
                        "hashes": {"sha256": "ab"},
                    },
                    {"filename": "pkg-1.0.0.tar.gz", "yanked": False},
                    {
                        "filename": "pkg-1.0.0-py3-none-any.whl",
                        "upload-time": "x",
                    },
                ],
            },
        )
    )
    result = await _run_list(http_client, _payload())
    assert result is not None
    assert list(result.filenames) == [
        "pkg-1.0.0.tar.gz",
        "pkg-1.0.0.tar.gz",
        "pkg-1.0.0-py3-none-any.whl",
    ]


@pytest.mark.asyncio
async def test_equivalent_versions_and_normalized_names_match_with_metadata() -> None:
    """Name and version matching must work when the listing carries metadata.

    A stale versions value or per-file size next to the filename must not stop
    an equivalent spelling from matching, or a published release would be
    uploaded twice."""
    http_client = FakeHttpClient(
        FakeHttpResponse(
            status_code=200,
            payload={
                "versions": 42,
                "files": [{"filename": "my_pkg-1.0.0.tar.gz", "size": 1}],
            },
        )
    )
    payload = ListPublishedArtifactsRunPayload(
        src_artifact_def_path=_DEF_URI,
        version="1.0",
        registry_name="pypi",
    )
    result = await run_handler(
        ListPublishedArtifactsPythonHandler,
        payload,
        action_cls=ListPublishedArtifactsAction,
        service_overrides={
            IHttpClient: http_client,
            IProjectInfoProvider: _provider_for("my-pkg"),
            IRepositoryCredentialsProvider: _repository_provider("pypi"),
        },
    )
    assert result is not None
    assert list(typing.cast(ListPublishedArtifactsRunResult, result).filenames) == [
        "my_pkg-1.0.0.tar.gz"
    ]


@pytest.mark.asyncio
async def test_handler_resolves_codec_from_default_binding(
    tmp_path: pathlib.Path,
) -> None:
    """The handler must structure listings through the shared codec binding.

    The listing check runs against whatever the registry returns, so it must
    use the workspace's codec rather than a private validator; otherwise a
    metadata field the index adds would fail the check and a published version
    would look absent."""
    calls: list[idataclasscodec.UnknownFields] = []
    real = DataclassCodec()

    class _RecordingCodec:
        def json_schema(self, cls: type) -> dict[str, typing.Any]:
            return real.json_schema(cls)

        def structure(
            self,
            data: typing.Any,
            cls: type,
            *,
            unknown_fields: idataclasscodec.UnknownFields = (
                idataclasscodec.UnknownFields.FORBID
            ),
        ) -> typing.Any:
            calls.append(unknown_fields)
            return real.structure(data, cls, unknown_fields=unknown_fields)

    recorder = _RecordingCodec()
    http_client = FakeHttpClient(
        FakeHttpResponse(
            status_code=200,
            payload={"files": [{"filename": "pkg-1.0.0.tar.gz"}]},
        )
    )
    action_source = (
        f"{ListPublishedArtifactsAction.__module__}."
        f"{ListPublishedArtifactsAction.__qualname__}"
    )
    handler_source = (
        f"{ListPublishedArtifactsPythonHandler.__module__}."
        f"{ListPublishedArtifactsPythonHandler.__qualname__}"
    )
    actions: dict[str, dict[str, typing.Any]] = {
        ListPublishedArtifactsAction.__name__: {
            "source": action_source,
            "handlers": [
                {
                    "name": ListPublishedArtifactsPythonHandler.__name__,
                    "source": handler_source,
                    "config": {},
                }
            ],
        }
    }
    async with handler_test_session(
        project_dir=tmp_path,
        actions=actions,
        service_overrides={
            IHttpClient: http_client,
            IProjectInfoProvider: _project_info_provider(),
            IRepositoryCredentialsProvider: _repository_provider("pypi"),
            IDataclassCodec: recorder,
        },
    ) as session:
        result = await session.run_action(
            ListPublishedArtifactsAction.__name__, _payload()
        )
        assert isinstance(result, ListPublishedArtifactsRunResult)
        assert list(result.filenames) == ["pkg-1.0.0.tar.gz"]
        assert await session.service(IDataclassCodec) is recorder
    assert calls == [idataclasscodec.UnknownFields.IGNORE]


@pytest.mark.asyncio
async def test_unsupported_names_are_omitted() -> None:
    """Unparseable filenames are skipped so one odd registry entry does not fail the whole version check."""
    http_client = FakeHttpClient(
        FakeHttpResponse(
            status_code=200,
            payload={"files": [{"filename": "README.txt"}]},
        )
    )
    result = await _run_list(http_client, _payload())
    assert result is not None
    assert list(result.filenames) == []


@pytest.mark.asyncio
async def test_invalid_requested_version_fails() -> None:
    """An invalid requested version must fail loudly instead of reporting an empty listing that would invite a doomed upload."""
    http_client = FakeHttpClient(
        FakeHttpResponse(status_code=200, payload={"files": []})
    )
    with pytest.raises(Exception):
        await _run_list(http_client, _payload(version="not-a-version!"))


@pytest.mark.asyncio
async def test_404_remains_empty() -> None:
    """A missing package page still means nothing is published, so first-time uploads keep working."""
    http_client = FakeHttpClient(FakeHttpResponse(status_code=404, payload={}))
    result = await _run_list(http_client, _payload())
    assert result is not None
    assert list(result.filenames) == []
