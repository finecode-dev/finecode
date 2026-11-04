from __future__ import annotations

import pathlib

import pytest
from fine_dist_artifacts.publish_artifact_to_registry_action import (
    PublishArtifactToRegistryAction,
    PublishArtifactToRegistryRunPayload,
    PublishArtifactToRegistryRunResult,
)
from finecode_extension_api.interfaces.irepositorycredentialsprovider import (
    IRepositoryCredentialsProvider,
    Repository,
)
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.impls.repository_credentials_provider import (
    ConfigRepositoryCredentialsProvider,
    RepositoryCredentialsProviderConfig,
)
from finecode_extension_runner.testing import run_handler
from twine import settings as twine_settings

from fine_python_package_info.publish_artifact_to_registry_py_handler import (
    PublishArtifactToRegistryPyHandler,
)


@pytest.mark.parametrize(
    "upload_url",
    ["https://upload.pypi.org/legacy/", "https://test.pypi.org/legacy/"],
)
@pytest.mark.parametrize("force", [False, True])
async def test_warehouse_upload_preserves_skip_existing_policy(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    upload_url: str,
    force: bool,
) -> None:
    from twine.commands import upload as twine_upload

    artifact = tmp_path / "dist café%20" / "pkg-1.0.0-py3-none-any.whl"
    artifact_uri = path_to_resource_uri(artifact)
    uploaded: list[list[str]] = []

    def capture_upload(settings: twine_settings.Settings, filenames: list[str]) -> None:
        assert settings.skip_existing is (not force)
        uploaded.append(filenames)

    monkeypatch.setattr(twine_upload, "upload", capture_upload)
    result = await run_handler(
        PublishArtifactToRegistryPyHandler,
        PublishArtifactToRegistryRunPayload(
            src_artifact_def_path=path_to_resource_uri(tmp_path / "pyproject.toml"),
            dist_artifact_paths=[artifact_uri],
            registry_name="registry",
            force=force,
        ),
        action_cls=PublishArtifactToRegistryAction,
        service_overrides={
            IRepositoryCredentialsProvider: _repository_provider(
                "registry", upload_url=upload_url
            ),
        },
    )

    assert isinstance(result, PublishArtifactToRegistryRunResult)
    assert result.error is None
    assert result.published_paths == [artifact_uri]
    assert uploaded == [[artifact.as_posix()]]


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


@pytest.mark.asyncio
async def test_upload_url_pointing_at_the_index_host_is_reported_without_uploading(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The index-only host rejects uploads, so the handler reports the misconfiguration as this registry's error — leaving sibling registries free to publish — and must not reach the upload library at all."""
    from twine.commands import upload as twine_upload

    def fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("twine.upload must not be reached on a bad upload_url")

    monkeypatch.setattr(twine_upload, "upload", fail_if_called)

    payload = PublishArtifactToRegistryRunPayload(
        src_artifact_def_path=path_to_resource_uri(tmp_path / "pyproject.toml"),
        dist_artifact_paths=[
            path_to_resource_uri(tmp_path / "dist/pkg-1.0.0-py3-none-any.whl")
        ],
        registry_name="pypi",
    )

    result = await run_handler(
        PublishArtifactToRegistryPyHandler,
        payload,
        action_cls=PublishArtifactToRegistryAction,
        service_overrides={
            IRepositoryCredentialsProvider: _repository_provider(
                "pypi", upload_url="https://pypi.org/legacy/"
            ),
        },
    )

    assert isinstance(result, PublishArtifactToRegistryRunResult)
    assert result.published_paths == []
    assert result.error is not None
    assert "upload.pypi.org" in result.error
