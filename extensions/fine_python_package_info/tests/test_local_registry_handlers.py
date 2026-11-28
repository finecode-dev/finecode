"""Handler integration test: real registry handlers against ``LocalRegistry``.

The unit tests in ``test_list_published_artifacts_python_handler.py`` fake the
HTTP layer and ``test_publish_artifact_to_registry_py_handler.py`` never
reaches twine, so neither proves the handlers speak the real wire protocol.
This test runs the production list and publish handlers with a real HTTP
client and a real twine upload against ``LocalRegistry``: nothing is listed
before the upload, the upload succeeds, the version is listed after it, a
forced re-upload fails, and the configured credentials reach the server.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import json as _json
import pathlib
import tarfile
import typing
import urllib.parse as _urlparse
import zipfile

import pytest
from fine_dist_artifacts.list_published_artifacts_action import (
    ListPublishedArtifactsAction,
    ListPublishedArtifactsRunPayload,
    ListPublishedArtifactsRunResult,
)
from fine_dist_artifacts.publish_artifact_action import (
    PublishArtifactAction,
    PublishArtifactRunPayload,
)
from fine_dist_artifacts.publish_artifact_handler import PublishArtifactHandler
from fine_dist_artifacts.publish_artifact_to_registry_action import (
    PublishArtifactToRegistryAction,
    PublishArtifactToRegistryRunPayload,
    PublishArtifactToRegistryRunResult,
)
from fine_dist_artifacts.verify_artifact_published_to_registry_action import (
    VerifyArtifactPublishedToRegistryAction,
    VerifyArtifactPublishedToRegistryRunPayload,
)
from fine_dist_artifacts.verify_artifact_published_to_registry_handler import (
    VerifyArtifactPublishedToRegistryHandler,
)
from fine_src_artifacts.get_src_artifact_registries_action import (
    GetSrcArtifactRegistriesAction,
    GetSrcArtifactRegistriesRunResult,
)
from fine_src_artifacts.get_src_artifact_version_action import (
    GetSrcArtifactVersionAction,
    GetSrcArtifactVersionRunResult,
)
from finecode_extension_api.interfaces.ihttpclient import IHttpClient
from finecode_extension_api.interfaces.iprojectactionrunner import ActionRunFailed
from finecode_extension_api.interfaces.iprojectinfoprovider import (
    IProjectInfoProvider,
    WorkspacePackage,
)
from finecode_extension_api.interfaces.irepositorycredentialsprovider import (
    IRepositoryCredentialsProvider,
    Repository,
    RepositoryCredentials,
)
from finecode_extension_api.resource_uri import (
    ResourceUri,
    path_to_resource_uri,
    resource_uri_to_path,
)
from finecode_extension_runner.impls.repository_credentials_provider import (
    ConfigRepositoryCredentialsProvider,
    RepositoryCredentialsProviderConfig,
)
from finecode_extension_runner.testing import (
    NoOpLogger,
    handler_test_session,
    run_handler,
)
from finecode_httpclient.client import HttpClient

from fine_python_package_info.list_published_artifacts_python_handler import (
    ListPublishedArtifactsPythonHandler,
)
from fine_python_package_info.publish_artifact_to_registry_py_handler import (
    PublishArtifactToRegistryPyHandler,
)
from fine_python_package_info.testing.local_registry import LocalRegistry

_PACKAGE_NAME = "tiny-local-registry-pkg"
_PACKAGE_VERSION = "0.1.0"
_DEF_PATH = pathlib.Path(__file__).parent / "pkg" / "pyproject.toml"
_REGISTRY_NAME = "local"
_USERNAME = "__token__"
_PASSWORD = "test-secret"


class _StubProjectInfoProvider:
    """Reports a fixed raw config regardless of what's on disk at *project_def_path*."""

    def __init__(self, raw_config: dict[str, typing.Any]) -> None:
        self._raw_config = raw_config

    def get_current_project_dir_path(self) -> pathlib.Path:
        raise NotImplementedError

    def get_current_project_def_path(self) -> pathlib.Path:
        raise NotImplementedError

    async def get_current_project_package_name(self) -> str:
        raise NotImplementedError

    async def get_project_raw_config(
        self, project_def_path: pathlib.Path
    ) -> dict[str, typing.Any]:
        assert project_def_path == _DEF_PATH
        return self._raw_config

    async def get_current_project_raw_config(self) -> dict[str, typing.Any]:
        raise NotImplementedError

    def get_current_project_raw_config_version(self) -> int:
        raise NotImplementedError

    async def get_workspace_packages(self) -> dict[str, WorkspacePackage]:
        raise NotImplementedError


def _build_wheel(tmp_path: pathlib.Path) -> pathlib.Path:
    """Assemble a minimal valid wheel with stdlib ``zipfile``.

    The testing env ships no build backend, so ``python -m build`` cannot run
    here. Twine only needs a ``.whl`` whose dist-info METADATA parses, which a
    small hand-built archive satisfies without leaving the test process.
    """
    outdir = tmp_path / "dist café%20"
    outdir.mkdir()
    wheel_path = outdir / "tiny_local_registry_pkg-0.1.0-py3-none-any.whl"
    dist_info = "tiny_local_registry_pkg-0.1.0.dist-info"
    entries = {
        "tiny_local_registry_pkg/__init__.py": b"",
        f"{dist_info}/METADATA": (
            b"Metadata-Version: 2.1\nName: tiny-local-registry-pkg\nVersion: 0.1.0\n"
        ),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\n"
            b"Generator: local-registry-test\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
    }
    record_lines = []
    for arcname, content in entries.items():
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(content).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        record_lines.append(f"{arcname},sha256={digest},{len(content)}")
    record_lines.append(f"{dist_info}/RECORD,,")
    entries[f"{dist_info}/RECORD"] = ("\n".join(record_lines) + "\n").encode("utf-8")
    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for arcname, content in entries.items():
            _ = archive.writestr(arcname, content)
    return wheel_path


def _provider(registry: LocalRegistry) -> ConfigRepositoryCredentialsProvider:
    return ConfigRepositoryCredentialsProvider(
        RepositoryCredentialsProviderConfig(
            repositories=[
                Repository(
                    name=_REGISTRY_NAME,
                    index_url=registry.index_url,
                    upload_url=registry.upload_url,
                )
            ],
            credentials_by_repository={
                _REGISTRY_NAME: RepositoryCredentials(
                    username=_USERNAME, password=_PASSWORD
                )
            },
        )
    )


def _overrides(registry: LocalRegistry) -> dict[type, typing.Any]:
    return {
        IHttpClient: HttpClient(NoOpLogger()),
        IProjectInfoProvider: _StubProjectInfoProvider(
            {"project": {"name": _PACKAGE_NAME}}
        ),
        IRepositoryCredentialsProvider: _provider(registry),
    }


def _list_payload() -> ListPublishedArtifactsRunPayload:
    return ListPublishedArtifactsRunPayload(
        src_artifact_def_path=path_to_resource_uri(_DEF_PATH),
        version=_PACKAGE_VERSION,
        registry_name=_REGISTRY_NAME,
    )


def _publish_payload(
    dist_paths: list[ResourceUri],
    *,
    force: bool,
) -> PublishArtifactToRegistryRunPayload:
    return PublishArtifactToRegistryRunPayload(
        src_artifact_def_path=path_to_resource_uri(_DEF_PATH),
        dist_artifact_paths=dist_paths,
        registry_name=_REGISTRY_NAME,
        force=force,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_publish_then_list_round_trip_against_local_registry(
    tmp_path: pathlib.Path,
    force: bool,
) -> None:
    """The production list/publish handlers must complete a real upload cycle.

    Fake-client tests prove each handler handles canned responses, but the
    release rehearsal depends on the full loop working over the wire: twine
    multipart against the upload endpoint, and the JSON simple index for the
    before/after checks. If this loop breaks, Phase L rehearsal results
    cannot be trusted.
    """
    wheel_path = _build_wheel(tmp_path)
    dist_paths = [path_to_resource_uri(wheel_path)]
    expected_auth = (
        "Basic " + base64.b64encode(f"{_USERNAME}:{_PASSWORD}".encode()).decode()
    )
    with LocalRegistry(tmp_path / "registry") as registry:
        service_overrides = _overrides(registry)

        listed_raw = await run_handler(
            ListPublishedArtifactsPythonHandler,
            _list_payload(),
            action_cls=ListPublishedArtifactsAction,
            service_overrides=service_overrides,
        )
        assert listed_raw is not None
        listed_before = typing.cast(ListPublishedArtifactsRunResult, listed_raw)
        assert listed_before.filenames == []
        assert all(record.method != "POST" for record in registry.request_log), (
            "a version check must read the index, never upload"
        )

        published_raw = await run_handler(
            PublishArtifactToRegistryPyHandler,
            _publish_payload(dist_paths, force=force),
            action_cls=PublishArtifactToRegistryAction,
            service_overrides=service_overrides,
        )
        assert published_raw is not None
        published = typing.cast(PublishArtifactToRegistryRunResult, published_raw)
        assert published.error is None
        assert published.published_paths == dist_paths

        listed_raw = await run_handler(
            ListPublishedArtifactsPythonHandler,
            _list_payload(),
            action_cls=ListPublishedArtifactsAction,
            service_overrides=service_overrides,
        )
        assert listed_raw is not None
        listed_after = typing.cast(ListPublishedArtifactsRunResult, listed_raw)
        assert wheel_path.name in listed_after.filenames

        republished_raw = await run_handler(
            PublishArtifactToRegistryPyHandler,
            _publish_payload(dist_paths, force=force),
            action_cls=PublishArtifactToRegistryAction,
            service_overrides=service_overrides,
        )
        assert republished_raw is not None
        republished = typing.cast(PublishArtifactToRegistryRunResult, republished_raw)
        assert republished.error is not None
        assert republished.published_paths == []

        posts = [record for record in registry.request_log if record.method == "POST"]
        assert posts != []
        assert all(record.authorization == expected_auth for record in posts)
        assert any(record.filename == wheel_path.name for record in posts)


# ---------------------------------------------------------------------------
# Verification-era composition tests (issue #931).
#
# The tests above prove each handler against canned responses. The tests below
# run the production list, publish, skip-gate and verification handlers
# against ``LocalRegistry`` over real HTTP/Twine, so the release rehearsal can
# trust the full loop: upload, scoped listing, filename membership and the
# duplicate/wrong-path failure modes.
# ---------------------------------------------------------------------------


def _dist_basename(package_name: str, version: str, kind: str) -> str:
    stem = package_name.replace("-", "_") if kind == "wheel" else package_name
    if kind == "wheel":
        return f"{stem}-{version}-py3-none-any.whl"
    if kind == "zip":
        return f"{stem}-{version}.zip"
    return f"{stem}-{version}.tar.gz"


def _build_sdist_for(
    outdir: pathlib.Path, package_name: str, version: str, ext: str = ".tar.gz"
) -> pathlib.Path:
    """Assemble a minimal valid sdist whose PKG-INFO twine can upload."""
    outdir.mkdir(parents=True, exist_ok=True)
    filename = f"{package_name}-{version}{ext}"
    sdist_path = outdir / filename
    top_dir = f"{package_name}-{version}"
    pkg_info = (
        f"Metadata-Version: 2.1\nName: {package_name}\nVersion: {version}\n".encode()
    )
    module_name = package_name.replace("-", "_")
    if ext == ".zip":
        with zipfile.ZipFile(
            sdist_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr(f"{top_dir}/PKG-INFO", pkg_info)
            archive.writestr(f"{top_dir}/{module_name}.py", b"")
    else:
        with tarfile.open(sdist_path, "w:gz") as archive:
            for arcname, content in (
                (f"{top_dir}/PKG-INFO", pkg_info),
                (f"{top_dir}/{module_name}.py", b""),
            ):
                info = tarfile.TarInfo(arcname)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    return sdist_path


def _build_wheel_for(
    outdir: pathlib.Path, package_name: str, version: str
) -> pathlib.Path:
    """Assemble a minimal valid wheel like :func:`_build_wheel` for any version."""
    outdir.mkdir(parents=True, exist_ok=True)
    stem = package_name.replace("-", "_")
    wheel_path = outdir / f"{stem}-{version}-py3-none-any.whl"
    dist_info = f"{stem}-{version}.dist-info"
    entries = {
        f"{stem}/__init__.py": b"",
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {package_name}\nVersion: {version}\n".encode()
        ),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\n"
            b"Generator: local-registry-test\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
    }
    record_lines = []
    for arcname, content in entries.items():
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(content).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        record_lines.append(f"{arcname},sha256={digest},{len(content)}")
    record_lines.append(f"{dist_info}/RECORD,,")
    entries[f"{dist_info}/RECORD"] = ("\n".join(record_lines) + "\n").encode("utf-8")
    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for arcname, content in entries.items():
            _ = archive.writestr(arcname, content)
    return wheel_path


def _encode_multipart(
    boundary: str, fields: dict[str, str], filename: str, data: bytes
) -> bytes:
    """Encode a twine-style multipart upload body (mirrors the wire fixture)."""
    body = b""
    for key, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        body += value.encode() + b"\r\n"
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="content"; filename="{filename}"\r\n'.encode()
    body += b"Content-Type: application/octet-stream\r\n\r\n"
    body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body


def _post_upload(
    registry: LocalRegistry,
    *,
    name: str,
    version: str,
    filename: str,
    data: bytes,
) -> tuple[int, str]:
    boundary = "test-boundary"
    body = _encode_multipart(
        boundary, {"name": name, "version": version}, filename, data
    )
    connection = http.client.HTTPConnection(registry.host, registry.port)
    try:
        connection.request(
            "POST",
            "/legacy/",
            body,
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        response = connection.getresponse()
        return (response.status, response.read().decode("utf-8", errors="replace"))
    finally:
        connection.close()


def _get_index_json(
    registry: LocalRegistry, package_name: str
) -> tuple[int, typing.Any]:
    connection = http.client.HTTPConnection(registry.host, registry.port)
    try:
        connection.request("GET", f"/simple/{_urlparse.quote(package_name)}/")
        response = connection.getresponse()
        raw = response.read().decode("utf-8", errors="replace")
        if response.status != 200:
            return (response.status, raw)
        return (response.status, _json.loads(raw))
    finally:
        connection.close()


def _test_actions(
    extra: dict[type[typing.Any], list[tuple[str, type[typing.Any]]]] | None = None,
) -> dict[str, typing.Any]:
    def _entry(
        action_cls: type[typing.Any], handlers: list[tuple[str, type[typing.Any]]]
    ) -> dict[str, typing.Any]:
        return {
            "source": f"{action_cls.__module__}.{action_cls.__qualname__}",
            "handlers": [
                {
                    "name": name,
                    "source": f"{cls.__module__}.{cls.__qualname__}",
                    "env": "test",
                }
                for name, cls in handlers
            ],
        }

    actions = {
        PublishArtifactAction.__name__: _entry(
            PublishArtifactAction, [("publish", PublishArtifactHandler)]
        ),
        VerifyArtifactPublishedToRegistryAction.__name__: _entry(
            VerifyArtifactPublishedToRegistryAction,
            [("verify", VerifyArtifactPublishedToRegistryHandler)],
        ),
        ListPublishedArtifactsAction.__name__: _entry(
            ListPublishedArtifactsAction,
            [("listing", ListPublishedArtifactsPythonHandler)],
        ),
        PublishArtifactToRegistryAction.__name__: _entry(
            PublishArtifactToRegistryAction,
            [("upload", PublishArtifactToRegistryPyHandler)],
        ),
    }
    for action_cls, handlers in (extra or {}).items():
        actions[action_cls.__name__] = _entry(action_cls, handlers)
    return actions


_SESSION_VERSION = _PACKAGE_VERSION
_SESSION_INDEX_URL = ""
_SESSION_UPLOAD_URL = ""


class _SessionVersionHandler:
    """Reports the session script's version regardless of disk state."""

    async def run(self, payload: typing.Any, run_context: typing.Any) -> typing.Any:
        _ = payload
        _ = run_context
        return GetSrcArtifactVersionRunResult(version=_SESSION_VERSION)


class _SessionRegistriesHandler:
    """Reports the session script's registry regardless of project config."""

    async def run(self, payload: typing.Any, run_context: typing.Any) -> typing.Any:
        _ = payload
        _ = run_context
        from fine_src_artifacts.get_src_artifact_registries_action import Registry

        return GetSrcArtifactRegistriesRunResult(
            registries=[
                Registry(
                    name=_REGISTRY_NAME,
                    index_url=_SESSION_INDEX_URL,
                    upload_url=_SESSION_UPLOAD_URL,
                )
            ]
        )


async def _publish_session(
    tmp_path: pathlib.Path,
    registry: LocalRegistry,
    dist_paths: list[ResourceUri],
    *,
    force: bool = False,
    version: str = _PACKAGE_VERSION,
    index_url: str | None = None,
    upload_url: str | None = None,
) -> typing.Any:
    """Run the real publish handler (skip gate + twine) against the registry."""
    resolved_index = index_url if index_url is not None else registry.index_url
    resolved_upload = upload_url if upload_url is not None else registry.upload_url

    global _SESSION_VERSION, _SESSION_INDEX_URL, _SESSION_UPLOAD_URL
    _SESSION_VERSION = version
    _SESSION_INDEX_URL = resolved_index
    _SESSION_UPLOAD_URL = resolved_upload

    actions = _test_actions(
        {
            GetSrcArtifactVersionAction: [("version", _SessionVersionHandler)],
            GetSrcArtifactRegistriesAction: [("regs", _SessionRegistriesHandler)],
        }
    )
    provider = ConfigRepositoryCredentialsProvider(
        RepositoryCredentialsProviderConfig(
            repositories=[
                Repository(
                    name=_REGISTRY_NAME,
                    index_url=resolved_index,
                    upload_url=resolved_upload,
                )
            ],
            credentials_by_repository={
                _REGISTRY_NAME: RepositoryCredentials(
                    username=_USERNAME, password=_PASSWORD
                )
            },
        )
    )
    overrides = _overrides(registry)
    overrides[IRepositoryCredentialsProvider] = provider
    async with handler_test_session(
        project_dir=tmp_path,
        actions=actions,
        service_overrides=overrides,
    ) as session:
        return await session.run_action(
            PublishArtifactAction.__name__,
            PublishArtifactRunPayload(
                src_artifact_def_path=path_to_resource_uri(_DEF_PATH),
                dist_artifact_paths=dist_paths,
                force=force,
            ),
        )


async def _verify_session(
    tmp_path: pathlib.Path,
    registry: LocalRegistry,
    version: str,
    dist_paths: list[ResourceUri],
) -> typing.Any:
    """Run the real verification handler (bounded retries + listing) against the registry."""
    actions = _test_actions()
    async with handler_test_session(
        project_dir=tmp_path,
        actions=actions,
        service_overrides=_overrides(registry),
    ) as session:
        return await session.run_action(
            VerifyArtifactPublishedToRegistryAction.__name__,
            VerifyArtifactPublishedToRegistryRunPayload(
                src_artifact_def_path=path_to_resource_uri(_DEF_PATH),
                dist_artifact_paths=dist_paths,
                registry_name=_REGISTRY_NAME,
                version=version,
            ),
        )


async def _list_session(
    registry: LocalRegistry,
    version: str,
) -> ListPublishedArtifactsRunResult:
    actions = _test_actions()
    async with handler_test_session(
        project_dir=pathlib.Path(__file__).parent,
        actions=actions,
        service_overrides=_overrides(registry),
    ) as session:
        result = await session.run_action(
            ListPublishedArtifactsAction.__name__,
            ListPublishedArtifactsRunPayload(
                src_artifact_def_path=path_to_resource_uri(_DEF_PATH),
                version=version,
                registry_name=_REGISTRY_NAME,
            ),
        )
    return typing.cast(ListPublishedArtifactsRunResult, result)


@pytest.mark.asyncio
async def test_wheel_and_sdist_upload_then_verify(tmp_path: pathlib.Path) -> None:
    """Uploading the wheel and sdist makes both visible to the scoped listing, so verification confirms the release."""
    dist_dir = tmp_path / "dist café%20"
    wheel_path = _build_wheel(tmp_path)
    sdist_path = _build_sdist_for(dist_dir, _PACKAGE_NAME, _PACKAGE_VERSION)
    dist_paths = [path_to_resource_uri(wheel_path), path_to_resource_uri(sdist_path)]
    with LocalRegistry(tmp_path / "registry") as registry:
        published = await _publish_session(tmp_path, registry, dist_paths)
        assert typing.cast(typing.Any, published).published_registries == [
            _REGISTRY_NAME
        ]
        assert typing.cast(typing.Any, published).failed_registries == {}

        listed = await _list_session(registry, _PACKAGE_VERSION)
        assert set(listed.filenames) == {wheel_path.name, sdist_path.name}

        verified = await _verify_session(
            tmp_path, registry, _PACKAGE_VERSION, dist_paths
        )
        assert typing.cast(typing.Any, verified).errors == []


@pytest.mark.asyncio
async def test_zip_sdist_upload_then_verify(tmp_path: pathlib.Path) -> None:
    """A zip sdist is a first-class distribution: it uploads, lists and verifies like a tarball."""
    dist_dir = tmp_path / "dist"
    zip_path = _build_sdist_for(dist_dir, _PACKAGE_NAME, _PACKAGE_VERSION, ext=".zip")
    dist_paths = [path_to_resource_uri(zip_path)]
    with LocalRegistry(tmp_path / "registry") as registry:
        published = await _publish_session(tmp_path, registry, dist_paths)
        assert typing.cast(typing.Any, published).published_registries == [
            _REGISTRY_NAME
        ]

        listed = await _list_session(registry, _PACKAGE_VERSION)
        assert listed.filenames == [zip_path.name]

        verified = await _verify_session(
            tmp_path, registry, _PACKAGE_VERSION, dist_paths
        )
        assert typing.cast(typing.Any, verified).errors == []


@pytest.mark.asyncio
async def test_partial_upload_resume_uploads_only_missing_files(
    tmp_path: pathlib.Path,
) -> None:
    """A resumed publish uploads exactly the files the current version still lacks, never re-sending what is there."""
    dist_dir = tmp_path / "dist"
    wheel_path = _build_wheel_for(dist_dir, _PACKAGE_NAME, _PACKAGE_VERSION)
    sdist_path = _build_sdist_for(dist_dir, _PACKAGE_NAME, _PACKAGE_VERSION)
    full_set = [path_to_resource_uri(wheel_path), path_to_resource_uri(sdist_path)]
    with LocalRegistry(tmp_path / "registry") as registry:
        first = await _publish_session(tmp_path, registry, full_set[:1])
        assert typing.cast(typing.Any, first).published_registries == [_REGISTRY_NAME]

        incomplete = await _verify_session(
            tmp_path, registry, _PACKAGE_VERSION, full_set
        )
        errors = typing.cast(typing.Any, incomplete).errors
        assert len(errors) == 1
        assert sdist_path.name in errors[0]
        assert _REGISTRY_NAME in errors[0]

        posts_before = len([r for r in registry.request_log if r.method == "POST"])
        second = await _publish_session(tmp_path, registry, full_set)
        assert typing.cast(typing.Any, second).failed_registries == {}
        new_posts = [r for r in registry.request_log if r.method == "POST"][
            posts_before:
        ]
        assert [r.filename for r in new_posts] == [sdist_path.name]

        complete = await _verify_session(tmp_path, registry, _PACKAGE_VERSION, full_set)
        assert typing.cast(typing.Any, complete).errors == []


@pytest.mark.asyncio
async def test_versions_are_scoped_to_the_requested_one(tmp_path: pathlib.Path) -> None:
    """Files from another version never satisfy the requested one, so a new release is not marked done by the old files."""
    dist_dir = tmp_path / "dist"
    v1_wheel = _build_wheel_for(dist_dir, _PACKAGE_NAME, "0.1.0")
    v1_sdist = _build_sdist_for(dist_dir, _PACKAGE_NAME, "0.1.0")
    v2_wheel = _build_wheel_for(dist_dir, _PACKAGE_NAME, "0.2.0")
    v2_sdist = _build_sdist_for(dist_dir, _PACKAGE_NAME, "0.2.0")
    with LocalRegistry(tmp_path / "registry") as registry:
        for paths in (
            [path_to_resource_uri(v1_wheel), path_to_resource_uri(v1_sdist)],
            [path_to_resource_uri(v2_wheel), path_to_resource_uri(v2_sdist)],
        ):
            published = await _publish_session(tmp_path, registry, paths)
            assert typing.cast(typing.Any, published).failed_registries == {}

        v1_verified = await _verify_session(
            tmp_path,
            registry,
            "0.1.0",
            [path_to_resource_uri(v1_wheel), path_to_resource_uri(v1_sdist)],
        )
        assert typing.cast(typing.Any, v1_verified).errors == []
        v2_verified = await _verify_session(
            tmp_path,
            registry,
            "0.2.0",
            [path_to_resource_uri(v2_wheel), path_to_resource_uri(v2_sdist)],
        )
        assert typing.cast(typing.Any, v2_verified).errors == []

        v1_listed = await _list_session(registry, "0.1.0")
        assert set(v1_listed.filenames) == {v1_wheel.name, v1_sdist.name}


@pytest.mark.asyncio
async def test_foreign_filename_is_visible_raw_but_excluded_scoped(
    tmp_path: pathlib.Path,
) -> None:
    """A foreign file stored under this package's upload name is visible on the wire but never counts toward it."""
    foreign_name = "foreign-1.0.0.tar.gz"
    with LocalRegistry(tmp_path / "registry") as registry:
        status, _ = _post_upload(
            registry,
            name=_PACKAGE_NAME,
            version="1.0.0",
            filename=foreign_name,
            data=b"foreign-bytes",
        )
        assert status == 200

        raw_status, raw_body = _get_index_json(registry, _PACKAGE_NAME)
        assert raw_status == 200
        assert foreign_name in [f["filename"] for f in raw_body["files"]]

        listed = await _list_session(registry, "1.0.0")
        assert listed.filenames == []

        verified = await _verify_session(
            tmp_path,
            registry,
            "1.0.0",
            [path_to_resource_uri(tmp_path / foreign_name)],
        )
        errors = typing.cast(typing.Any, verified).errors
        assert len(errors) == 1
        assert foreign_name in errors[0]
        assert _REGISTRY_NAME in errors[0]


@pytest.mark.asyncio
async def test_stale_payload_offered_again_fails_duplicate(
    tmp_path: pathlib.Path,
) -> None:
    """Re-offering an already-published payload with force fails on duplicate rejection instead of publishing again."""
    dist_dir = tmp_path / "dist"
    wheel_path = _build_wheel_for(dist_dir, _PACKAGE_NAME, _PACKAGE_VERSION)
    dist_paths = [path_to_resource_uri(wheel_path)]
    with LocalRegistry(tmp_path / "registry") as registry:
        first = await _publish_session(tmp_path, registry, dist_paths)
        assert typing.cast(typing.Any, first).published_registries == [_REGISTRY_NAME]

        forced = await _publish_session(tmp_path, registry, dist_paths, force=True)
        failed = typing.cast(typing.Any, forced).failed_registries
        assert typing.cast(typing.Any, forced).published_registries == []
        assert _REGISTRY_NAME in failed
        assert "already exists" in failed[_REGISTRY_NAME]


@pytest.mark.asyncio
async def test_wrong_index_path_never_recovers_until_corrected(
    tmp_path: pathlib.Path,
) -> None:
    """A persistent wrong index prefix keeps verification failing and turns the retry into a duplicate failure, not a silent skip."""
    dist_dir = tmp_path / "dist"
    wheel_path = _build_wheel_for(dist_dir, _PACKAGE_NAME, _PACKAGE_VERSION)
    sdist_path = _build_sdist_for(dist_dir, _PACKAGE_NAME, _PACKAGE_VERSION)
    dist_paths = [path_to_resource_uri(wheel_path), path_to_resource_uri(sdist_path)]
    with LocalRegistry(tmp_path / "registry") as registry:
        bad_provider = ConfigRepositoryCredentialsProvider(
            RepositoryCredentialsProviderConfig(
                repositories=[
                    Repository(
                        name=_REGISTRY_NAME,
                        index_url=f"{registry.index_url}wrong/",
                        upload_url=registry.upload_url,
                    )
                ],
                credentials_by_repository={
                    _REGISTRY_NAME: RepositoryCredentials(
                        username=_USERNAME, password=_PASSWORD
                    ),
                },
            )
        )

        async def _bad_list() -> ListPublishedArtifactsRunResult:
            actions = _test_actions()
            async with handler_test_session(
                project_dir=pathlib.Path(__file__).parent,
                actions=actions,
                service_overrides={
                    IHttpClient: HttpClient(NoOpLogger()),
                    IProjectInfoProvider: _StubProjectInfoProvider(
                        {"project": {"name": _PACKAGE_NAME}}
                    ),
                    IRepositoryCredentialsProvider: bad_provider,
                },
            ) as session:
                result = await session.run_action(
                    ListPublishedArtifactsAction.__name__,
                    ListPublishedArtifactsRunPayload(
                        src_artifact_def_path=path_to_resource_uri(_DEF_PATH),
                        version=_PACKAGE_VERSION,
                        registry_name=_REGISTRY_NAME,
                    ),
                )
            return typing.cast(ListPublishedArtifactsRunResult, result)

        published = await _publish_session(tmp_path, registry, dist_paths)
        assert typing.cast(typing.Any, published).published_registries == [
            _REGISTRY_NAME
        ]

        assert (await _bad_list()).filenames == []

        retry = await _publish_session(
            tmp_path,
            registry,
            dist_paths,
            index_url=f"{registry.index_url}wrong/",
        )
        retry_failed = typing.cast(typing.Any, retry).failed_registries
        assert typing.cast(typing.Any, retry).published_registries == []
        assert _REGISTRY_NAME in retry_failed

        listed = await _list_session(registry, _PACKAGE_VERSION)
        assert set(listed.filenames) == {wheel_path.name, sdist_path.name}
        reconciled = await _publish_session(tmp_path, registry, dist_paths)
        assert typing.cast(typing.Any, reconciled).published_registries == []
        assert typing.cast(typing.Any, reconciled).failed_registries == {}


@pytest.mark.asyncio
async def test_dry_run_listing_inputs(tmp_path: pathlib.Path) -> None:
    """The dry-run preview reads the scoped listing: files present means already released, absent means it would publish, an index fault means failed."""
    dist_dir = tmp_path / "dist"
    wheel_path = _build_wheel_for(dist_dir, _PACKAGE_NAME, _PACKAGE_VERSION)
    dist_paths = [path_to_resource_uri(wheel_path)]
    with LocalRegistry(tmp_path / "registry") as registry:
        # Only other versions present: the scoped listing is empty, so the
        # preview would report WOULD_PUBLISH for this version.
        other = _build_sdist_for(dist_dir, _PACKAGE_NAME, "9.9.9")
        published = await _publish_session(
            tmp_path, registry, [path_to_resource_uri(other)], version="9.9.9"
        )
        assert typing.cast(typing.Any, published).failed_registries == {}
        assert (await _list_session(registry, _PACKAGE_VERSION)).filenames == []

        # Current version present (even partially): the scoped listing is
        # non-empty, so the preview reports SKIPPED. The preview is
        # presence-based and cannot see that the sdist is still missing.
        first = await _publish_session(tmp_path, registry, dist_paths)
        assert typing.cast(typing.Any, first).published_registries == [_REGISTRY_NAME]
        assert (await _list_session(registry, _PACKAGE_VERSION)).filenames != []

        # An index fault raises instead of returning a listing, so the preview
        # reports FAILED for that registry.
        bad_provider = ConfigRepositoryCredentialsProvider(
            RepositoryCredentialsProviderConfig(
                repositories=[
                    Repository(
                        name=_REGISTRY_NAME,
                        index_url="https://upload.pypi.org/legacy/",
                        upload_url=registry.upload_url,
                    )
                ]
            )
        )
        actions = _test_actions()
        async with handler_test_session(
            project_dir=pathlib.Path(__file__).parent,
            actions=actions,
            service_overrides={
                IHttpClient: HttpClient(NoOpLogger()),
                IProjectInfoProvider: _StubProjectInfoProvider(
                    {"project": {"name": _PACKAGE_NAME}}
                ),
                IRepositoryCredentialsProvider: bad_provider,
            },
        ) as session:
            with pytest.raises(ActionRunFailed, match="upload.pypi.org"):
                await session.run_action(
                    ListPublishedArtifactsAction.__name__,
                    ListPublishedArtifactsRunPayload(
                        src_artifact_def_path=path_to_resource_uri(_DEF_PATH),
                        version=_PACKAGE_VERSION,
                        registry_name=_REGISTRY_NAME,
                    ),
                )


@pytest.mark.asyncio
async def test_real_build_handler_names_parse_and_upload(
    tmp_path: pathlib.Path,
) -> None:
    """The real build handler returns actual distribution files whose names parse, so the registry loop starts from genuine artifacts."""
    from fine_python_lang.build_python_artifact_action import (
        BuildPythonArtifactAction,
        BuildPythonArtifactRunPayload,
    )
    from packaging.utils import (
        InvalidSdistFilename,
        InvalidWheelFilename,
        canonicalize_name,
        parse_sdist_filename,
        parse_wheel_filename,
    )
    from packaging.version import Version

    from fine_python_package_info.build_artifact_py_handler import (
        BuildArtifactPyHandler,
    )

    fixture = tmp_path / "fixture"
    package_dir = fixture / "tiny_local_registry_pkg"
    package_dir.mkdir(parents=True)
    _ = (package_dir / "__init__.py").write_text("", encoding="utf-8")
    _ = (fixture / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["setuptools>=61"]\n'
        'build-backend = "setuptools.build_meta"\n'
        "[project]\n"
        'name = "tiny-local-registry-pkg"\n'
        'version = "0.3.0"\n',
        encoding="utf-8",
    )
    outdir = tmp_path / "built"
    import sys as _sys

    from finecode_extension_api.interfaces import iextensionrunnerinfoprovider

    class _StubRunnerInfo:
        def get_current_env_name(self) -> str:
            return "test"

        def get_cache_dir_path(self) -> pathlib.Path:
            return tmp_path / ".cache"

        def get_venv_dir_path_of_env(self, env_name: str) -> pathlib.Path:
            _ = env_name
            return pathlib.Path(_sys.prefix)

        def get_current_venv_dir_path(self) -> pathlib.Path:
            return pathlib.Path(_sys.prefix)

        def get_venv_site_packages(
            self, venv_dir_path: pathlib.Path
        ) -> list[pathlib.Path]:
            _ = venv_dir_path
            return []

        def get_venv_python_interpreter(
            self, venv_dir_path: pathlib.Path
        ) -> pathlib.Path:
            _ = venv_dir_path
            return pathlib.Path(_sys.executable)

    result = await run_handler(
        BuildArtifactPyHandler,
        BuildPythonArtifactRunPayload(
            src_artifact_def_path=path_to_resource_uri(fixture / "pyproject.toml"),
            output_dir=path_to_resource_uri(outdir),
        ),
        action_cls=BuildPythonArtifactAction,
        project_dir=fixture,
        service_overrides={
            iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider: _StubRunnerInfo(),
        },
    )
    assert result is not None
    built = typing.cast(typing.Any, result).build_output_paths
    assert len(built) == 2
    names = sorted(resource_uri_to_path(uri).name for uri in built)
    assert all((outdir / name).is_file() for name in names)
    for name in names:
        try:
            parsed_name, parsed_version, *_ = parse_wheel_filename(name)
        except InvalidWheelFilename:
            parsed_name, parsed_version = parse_sdist_filename(name)
        except InvalidSdistFilename:
            raise AssertionError(f"unparseable built filename: {name}") from None
        assert canonicalize_name(str(parsed_name)) == _PACKAGE_NAME
        assert parsed_version == Version("0.3.0")

    with LocalRegistry(tmp_path / "registry") as registry:
        published = await _publish_session(tmp_path, registry, built, version="0.3.0")
        assert typing.cast(typing.Any, published).published_registries == [
            _REGISTRY_NAME
        ]
        listed = await _list_session(registry, "0.3.0")
        assert set(listed.filenames) == set(names)
