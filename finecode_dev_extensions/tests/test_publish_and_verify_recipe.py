"""Recipe test: the development preset wires a real verification handler.

A wrapper run using the real dev-preset verification declaration confirms the
wheel and sdist after an accepted publication. With a deliberately empty
verification declaration the same run records the upload side effect but
returns per-registry errors instead of success.
"""

from __future__ import annotations

import dataclasses
import pathlib
import tomllib
import typing

import pytest
from fine_dist_artifacts.list_published_artifacts_action import (
    ListPublishedArtifactsAction,
    ListPublishedArtifactsRunResult,
)
from fine_dist_artifacts.publish_artifact_to_registry_action import (
    PublishArtifactToRegistryAction,
    PublishArtifactToRegistryRunResult,
)
from fine_src_artifacts.get_src_artifact_registries_action import (
    GetSrcArtifactRegistriesAction,
    GetSrcArtifactRegistriesRunResult,
    Registry,
)
from fine_src_artifacts.get_src_artifact_version_action import (
    GetSrcArtifactVersionAction,
    GetSrcArtifactVersionRunResult,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ilogger
from finecode_extension_api.interfaces.irepositorycredentialsprovider import (
    IRepositoryCredentialsProvider,
    Repository,
)
from finecode_extension_api.resource_uri import ResourceUri, path_to_resource_uri
from finecode_extension_runner.impls.repository_credentials_provider import (
    ConfigRepositoryCredentialsProvider,
    RepositoryCredentialsProviderConfig,
)
from finecode_extension_runner.testing import NoOpLogger, handler_test_session

from finecode_dev_extensions.publish_and_verify_artifact_action import (
    PublishAndVerifyArtifactRunPayload,
)

_PRESET_PATH = (
    pathlib.Path(__file__).parent.parent.parent
    / "finecode_dev_common_preset"
    / "src"
    / "finecode_dev_common_preset"
    / "preset.toml"
)

_VERSION = "1.0.0"
_WHEEL = f"pkg-{_VERSION}-py3-none-any.whl"
_SDIST = f"pkg-{_VERSION}.tar.gz"


def _preset() -> dict[str, typing.Any]:
    with open(_PRESET_PATH, "rb") as handle:
        return tomllib.load(handle)


def _decl(
    preset: dict[str, typing.Any], action_key: str, *, env: str = "test"
) -> dict[str, typing.Any]:
    entry = preset["tool"]["finecode"]["action"][action_key]
    return {
        "source": entry["source"],
        "handlers": [{**handler, "env": env} for handler in entry.get("handlers", [])],
    }


@dataclasses.dataclass
class _RecipeState:
    uploaded: list[list[ResourceUri]] = dataclasses.field(default_factory=list)


_STATE = _RecipeState()


@dataclasses.dataclass
class _FakeVersionConfig(code_action.ActionHandlerConfig): ...


class _FakeVersionHandler(
    code_action.ActionHandler[GetSrcArtifactVersionAction, _FakeVersionConfig]
):
    async def run(self, payload: typing.Any, run_context: typing.Any) -> typing.Any:
        _ = payload
        _ = run_context
        return GetSrcArtifactVersionRunResult(version=_VERSION)


@dataclasses.dataclass
class _FakeRegistriesConfig(code_action.ActionHandlerConfig): ...


class _FakeRegistriesHandler(
    code_action.ActionHandler[GetSrcArtifactRegistriesAction, _FakeRegistriesConfig]
):
    async def run(self, payload: typing.Any, run_context: typing.Any) -> typing.Any:
        _ = payload
        _ = run_context
        return GetSrcArtifactRegistriesRunResult(
            registries=[
                Registry(
                    name="local",
                    index_url="https://example.test/local/",
                    upload_url="https://example.test/local/upload/",
                )
            ]
        )


@dataclasses.dataclass
class _FakeUploadConfig(code_action.ActionHandlerConfig): ...


class _FakeUploadHandler(
    code_action.ActionHandler[PublishArtifactToRegistryAction, _FakeUploadConfig]
):
    async def run(self, payload: typing.Any, run_context: typing.Any) -> typing.Any:
        _ = run_context
        _STATE.uploaded.append(list(payload.dist_artifact_paths))
        return PublishArtifactToRegistryRunResult(
            registry_name=payload.registry_name,
            published_paths=list(payload.dist_artifact_paths),
            error=None,
        )


@dataclasses.dataclass
class _FakeListingConfig(code_action.ActionHandlerConfig): ...


class _FakeListingHandler(
    code_action.ActionHandler[ListPublishedArtifactsAction, _FakeListingConfig]
):
    async def run(self, payload: typing.Any, run_context: typing.Any) -> typing.Any:
        _ = payload
        _ = run_context
        if _STATE.uploaded:
            return ListPublishedArtifactsRunResult(filenames=[_WHEEL, _SDIST])
        return ListPublishedArtifactsRunResult(filenames=[])


def _actions(
    preset: dict[str, typing.Any], *, empty_verification: bool
) -> dict[str, typing.Any]:
    verify_decl = _decl(preset, "verify_artifact_published_to_registry")
    if empty_verification:
        _empty: list[dict[str, typing.Any]] = []
        verify_decl = {"source": verify_decl["source"], "handlers": _empty}

    def _entry(
        action_cls: type, handlers: list[tuple[str, type]]
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

    return {
        "publish_and_verify_artifact": _decl(preset, "publish_and_verify_artifact"),
        "verify_artifact_published_to_registry": verify_decl,
        "publish_artifact": _decl(preset, "publish_artifact"),
        ListPublishedArtifactsAction.__name__: _entry(
            ListPublishedArtifactsAction, [("listing", _FakeListingHandler)]
        ),
        GetSrcArtifactVersionAction.__name__: _entry(
            GetSrcArtifactVersionAction, [("version", _FakeVersionHandler)]
        ),
        GetSrcArtifactRegistriesAction.__name__: _entry(
            GetSrcArtifactRegistriesAction, [("regs", _FakeRegistriesHandler)]
        ),
        PublishArtifactToRegistryAction.__name__: _entry(
            PublishArtifactToRegistryAction, [("upload", _FakeUploadHandler)]
        ),
    }


def _overrides() -> dict[type, typing.Any]:
    return {
        IRepositoryCredentialsProvider: ConfigRepositoryCredentialsProvider(
            RepositoryCredentialsProviderConfig(
                repositories=[
                    Repository(
                        name="local",
                        index_url="https://example.test/local/",
                        upload_url="https://example.test/local/upload/",
                    )
                ]
            )
        ),
        ilogger.ILogger: NoOpLogger(),
    }


def _payload(tmp_path: pathlib.Path) -> PublishAndVerifyArtifactRunPayload:
    return PublishAndVerifyArtifactRunPayload(
        src_artifact_def_path=path_to_resource_uri(tmp_path / "pyproject.toml"),
        dist_artifact_paths=[
            path_to_resource_uri(tmp_path / _WHEEL),
            path_to_resource_uri(tmp_path / _SDIST),
        ],
        force=False,
    )


@pytest.mark.asyncio
async def test_preset_wires_real_verification_handler() -> None:
    """The development preset must declare a real verification handler so releases actually confirm uploads."""
    preset = _preset()
    verify_handlers = preset["tool"]["finecode"]["action"][
        "verify_artifact_published_to_registry"
    ]["handlers"]
    assert len(verify_handlers) == 1
    assert (
        verify_handlers[0]["source"]
        == "fine_dist_artifacts.VerifyArtifactPublishedToRegistryHandler"
    )
    assert verify_handlers[0]["env"] == "dev_no_runtime"
    assert "fine_dist_artifacts~=0.1.0" in verify_handlers[0]["dependencies"]


@pytest.mark.asyncio
async def test_wrapper_confirms_upload_through_preset_recipe(
    tmp_path: pathlib.Path,
) -> None:
    """A wrapper run through the preset recipe confirms the wheel and sdist once the upload lands."""
    _STATE.uploaded.clear()
    preset = _preset()
    actions = _actions(preset, empty_verification=False)
    async with handler_test_session(
        project_dir=tmp_path,
        actions=actions,
        service_overrides=_overrides(),
    ) as session:
        result = await session.run_action(
            "publish_and_verify_artifact", _payload(tmp_path)
        )
    assert _STATE.uploaded != []
    assert result is not None
    assert typing.cast(typing.Any, result).verification_errors == {}
    assert typing.cast(typing.Any, result).published_registries == ["local"]


@pytest.mark.asyncio
async def test_empty_verification_declaration_returns_errors(
    tmp_path: pathlib.Path,
) -> None:
    """Without a verification handler the upload still lands but every registry reports unconfirmed."""
    _STATE.uploaded.clear()
    preset = _preset()
    actions = _actions(preset, empty_verification=True)
    async with handler_test_session(
        project_dir=tmp_path,
        actions=actions,
        service_overrides=_overrides(),
    ) as session:
        result = await session.run_action(
            "publish_and_verify_artifact", _payload(tmp_path)
        )
    assert _STATE.uploaded != []
    assert result is not None
    assert typing.cast(typing.Any, result).published_registries == ["local"]
    assert set(typing.cast(typing.Any, result).verification_errors) == {"local"}
