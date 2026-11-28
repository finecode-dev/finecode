from __future__ import annotations

import dataclasses
import pathlib
import subprocess
import typing

import pytest
from fine_dist_artifacts.list_published_artifacts_action import (
    ListPublishedArtifactsAction,
    ListPublishedArtifactsRunResult,
)
from fine_dist_artifacts.publish_artifact_action import (
    PublishArtifactAction,
    PublishArtifactRunResult,
)
from fine_dist_artifacts.verify_artifact_published_to_registry_action import (
    VerifyArtifactPublishedToRegistryAction,
)
from fine_git.create_git_tag_action import CreateGitTagAction
from fine_git.git_create_git_tag_handler import GitCreateGitTagHandler
from fine_src_artifacts.get_src_artifact_registries_action import (
    GetSrcArtifactRegistriesAction,
    GetSrcArtifactRegistriesRunResult,
    Registry,
)
from finecode_dev_extensions.publish_and_verify_artifact_action import (
    PublishAndVerifyArtifactAction,
)
from finecode_dev_extensions.publish_and_verify_artifact_handler import (
    PublishAndVerifyArtifactHandler,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ilogger
from finecode_extension_api.interfaces.irepositorycredentialsprovider import (
    IRepositoryCredentialsProvider,
    Repository,
)
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.impls.repository_credentials_provider import (
    ConfigRepositoryCredentialsProvider,
    RepositoryCredentialsProviderConfig,
)
from finecode_extension_runner.testing import NoOpLogger, handler_test_session

from fine_release.publish_release_artifact_handler import PublishReleaseArtifactHandler
from fine_release.record_release_tag_handler import RecordReleaseTagHandler
from fine_release.release_package_action import (
    PackageReleaseOutcome,
    RegistryPublishOutcome,
    ReleasePackageAction,
    ReleasePackageRunPayload,
    ReleasePackageRunResult,
    result_from_state,
)

_VERSION = "1.0.0"
_DIST_NAME = f"pkg-{_VERSION}.tar.gz"


@dataclasses.dataclass
class _ChainScript:
    registries: list[str] = dataclasses.field(default_factory=lambda: ["pypi"])
    published: list[str] = dataclasses.field(default_factory=lambda: ["pypi"])
    failed: dict[str, str] = dataclasses.field(default_factory=dict)
    listings: dict[str, object] = dataclasses.field(default_factory=dict)
    dist_dir: str = "/tmp"


_SCRIPT = _ChainScript()


def _set_script(script: _ChainScript) -> None:
    global _SCRIPT
    _SCRIPT = script


@dataclasses.dataclass
class _FakeBuildConfig(code_action.ActionHandlerConfig): ...


class _FakeBuildHandler(
    code_action.ActionHandler[ReleasePackageAction, _FakeBuildConfig]
):
    async def run(
        self,
        payload: ReleasePackageRunPayload,
        run_context: typing.Any,
    ) -> ReleasePackageRunResult:
        run_context.state.build_output_paths = [path_to_resource_uri(_dist_path())]
        return result_from_state(payload, run_context.state)


def _dist_path() -> pathlib.Path:
    base = pathlib.Path(_SCRIPT.dist_dir) if _SCRIPT.dist_dir else pathlib.Path("/tmp")
    return base / _DIST_NAME


@dataclasses.dataclass
class _FakeRegistriesConfig(code_action.ActionHandlerConfig): ...


class _FakeRegistriesHandler(
    code_action.ActionHandler[GetSrcArtifactRegistriesAction, _FakeRegistriesConfig]
):
    async def run(
        self,
        payload: typing.Any,
        run_context: typing.Any,
    ) -> GetSrcArtifactRegistriesRunResult:
        _ = payload
        _ = run_context
        return GetSrcArtifactRegistriesRunResult(
            registries=[
                Registry(
                    name=name,
                    index_url=f"https://example.test/{name}/",
                    upload_url=f"https://example.test/{name}/upload/",
                )
                for name in _SCRIPT.registries
            ]
        )


@dataclasses.dataclass
class _FakePublishConfig(code_action.ActionHandlerConfig): ...


class _FakePublishHandler(
    code_action.ActionHandler[PublishArtifactAction, _FakePublishConfig]
):
    async def run(
        self,
        payload: typing.Any,
        run_context: typing.Any,
    ) -> PublishArtifactRunResult:
        _ = payload
        _ = run_context
        return PublishArtifactRunResult(
            version=_VERSION,
            published_registries=list(_SCRIPT.published),
            failed_registries=dict(_SCRIPT.failed),
        )


@dataclasses.dataclass
class _FakeListingConfig(code_action.ActionHandlerConfig): ...


class _FakeListingHandler(
    code_action.ActionHandler[ListPublishedArtifactsAction, _FakeListingConfig]
):
    async def run(
        self,
        payload: typing.Any,
        run_context: typing.Any,
    ) -> ListPublishedArtifactsRunResult:
        _ = run_context
        behavior = _SCRIPT.listings.get(payload.registry_name, [])
        if isinstance(behavior, BaseException):
            raise behavior
        return ListPublishedArtifactsRunResult(
            filenames=list(typing.cast(list[str], behavior))
        )


def _action_entry(
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


def _actions() -> dict[str, typing.Any]:
    from fine_dist_artifacts.verify_artifact_published_to_registry_handler import (
        VerifyArtifactPublishedToRegistryHandler,
    )

    return {
        ReleasePackageAction.__name__: _action_entry(
            ReleasePackageAction,
            [
                ("build", _FakeBuildHandler),
                ("publish", PublishReleaseArtifactHandler),
                ("record_tag", RecordReleaseTagHandler),
            ],
        ),
        GetSrcArtifactRegistriesAction.__name__: _action_entry(
            GetSrcArtifactRegistriesAction, [("regs", _FakeRegistriesHandler)]
        ),
        PublishArtifactAction.__name__: _action_entry(
            PublishArtifactAction, [("pub", _FakePublishHandler)]
        ),
        PublishAndVerifyArtifactAction.__name__: _action_entry(
            PublishAndVerifyArtifactAction, [("wrap", PublishAndVerifyArtifactHandler)]
        ),
        VerifyArtifactPublishedToRegistryAction.__name__: _action_entry(
            VerifyArtifactPublishedToRegistryAction,
            [("verify", VerifyArtifactPublishedToRegistryHandler)],
        ),
        ListPublishedArtifactsAction.__name__: _action_entry(
            ListPublishedArtifactsAction, [("listing", _FakeListingHandler)]
        ),
        CreateGitTagAction.__name__: _action_entry(
            CreateGitTagAction, [("tag", GitCreateGitTagHandler)]
        ),
    }


def _init_repo(repo: pathlib.Path) -> str:
    _ = subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    _ = subprocess.run(
        ["git", "config", "user.email", "test@example.test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    _ = subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    _ = (repo / "README.md").write_text("pkg\n", encoding="utf-8")
    _ = subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    _ = subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True
    )
    done = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def _git(repo: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )


def _provider(names: list[str]) -> ConfigRepositoryCredentialsProvider:
    return ConfigRepositoryCredentialsProvider(
        RepositoryCredentialsProviderConfig(
            repositories=[
                Repository(
                    name=name,
                    index_url=f"https://example.test/{name}/",
                    upload_url=f"https://example.test/{name}/upload/",
                )
                for name in names
            ]
        )
    )


def _registry(result: ReleasePackageRunResult, name: str) -> typing.Any:
    return next(r for r in result.registries if r.registry == name)


@pytest.mark.asyncio
async def test_confirmed_single_registry_published_and_tagged(
    tmp_path: pathlib.Path,
) -> None:
    """A confirmed upload is PUBLISHED and recorded as an annotated tag pointing at the release commit."""
    head = _init_repo(tmp_path)
    _set_script(
        _ChainScript(
            registries=["pypi"],
            published=["pypi"],
            listings={"pypi": [_DIST_NAME]},
            dist_dir=str(tmp_path),
        )
    )
    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(),
        service_overrides={
            IRepositoryCredentialsProvider: _provider(["pypi"]),
            ilogger.ILogger: NoOpLogger(),
        },
    ) as session:
        result = typing.cast(
            ReleasePackageRunResult,
            await session.run_action(
                ReleasePackageAction.__name__,
                ReleasePackageRunPayload(
                    package_name="pkg",
                    version=_VERSION,
                    src_artifact_def_path=None,
                    dry_run=False,
                ),
            ),
        )
    assert result.outcome == PackageReleaseOutcome.PUBLISHED
    assert _registry(result, "pypi").outcome == RegistryPublishOutcome.PUBLISHED
    tag = "pkg@1.0.0"
    assert _git(tmp_path, "cat-file", "-t", tag).stdout.strip() == "tag"
    assert _git(tmp_path, "rev-parse", f"{tag}^{{}}").stdout.strip() == head
    assert "pypi" in _git(tmp_path, "cat-file", "tag", tag).stdout


@pytest.mark.asyncio
async def test_unconfirmed_single_registry_failed_without_tag(
    tmp_path: pathlib.Path,
) -> None:
    """An accepted upload that never appears in the index is FAILED and records no tag."""
    _ = _init_repo(tmp_path)
    _set_script(
        _ChainScript(
            registries=["pypi"],
            published=["pypi"],
            listings={"pypi": []},
            dist_dir=str(tmp_path),
        )
    )
    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(),
        service_overrides={
            IRepositoryCredentialsProvider: _provider(["pypi"]),
            ilogger.ILogger: NoOpLogger(),
        },
    ) as session:
        result = typing.cast(
            ReleasePackageRunResult,
            await session.run_action(
                ReleasePackageAction.__name__,
                ReleasePackageRunPayload(
                    package_name="pkg",
                    version=_VERSION,
                    src_artifact_def_path=None,
                    dry_run=False,
                ),
            ),
        )
    assert result.outcome == PackageReleaseOutcome.FAILED
    failed = _registry(result, "pypi")
    assert failed.outcome == RegistryPublishOutcome.FAILED
    assert any("not confirmed" in e for e in failed.errors)
    assert (
        _git(
            tmp_path, "rev-parse", "--verify", "--quiet", "refs/tags/pkg@1.0.0"
        ).returncode
        != 0
    )


@pytest.mark.asyncio
async def test_mixed_confirmation_failed_with_tag_naming_confirmed(
    tmp_path: pathlib.Path,
) -> None:
    """One confirmed and one unconfirmed registry is FAILED overall yet still tags the confirmed registry."""
    _ = _init_repo(tmp_path)
    _set_script(
        _ChainScript(
            registries=["a", "b"],
            published=["a", "b"],
            listings={"a": [_DIST_NAME], "b": []},
            dist_dir=str(tmp_path),
        )
    )
    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(),
        service_overrides={
            IRepositoryCredentialsProvider: _provider(["a", "b"]),
            ilogger.ILogger: NoOpLogger(),
        },
    ) as session:
        result = typing.cast(
            ReleasePackageRunResult,
            await session.run_action(
                ReleasePackageAction.__name__,
                ReleasePackageRunPayload(
                    package_name="pkg",
                    version=_VERSION,
                    src_artifact_def_path=None,
                    dry_run=False,
                ),
            ),
        )
    assert result.outcome == PackageReleaseOutcome.FAILED
    assert _registry(result, "a").outcome == RegistryPublishOutcome.PUBLISHED
    assert _registry(result, "b").outcome == RegistryPublishOutcome.FAILED
    annotation = _git(tmp_path, "cat-file", "tag", "pkg@1.0.0").stdout
    assert "Release pkg 1.0.0 (a)" in annotation


@pytest.mark.asyncio
async def test_reconciliation_skipped_and_tagged(tmp_path: pathlib.Path) -> None:
    """A version already present in the index is SKIPPED and still reconciles the tag."""
    _ = _init_repo(tmp_path)
    _set_script(
        _ChainScript(
            registries=["pypi"],
            published=[],
            listings={"pypi": [_DIST_NAME]},
            dist_dir=str(tmp_path),
        )
    )
    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(),
        service_overrides={
            IRepositoryCredentialsProvider: _provider(["pypi"]),
            ilogger.ILogger: NoOpLogger(),
        },
    ) as session:
        payload = ReleasePackageRunPayload(
            package_name="pkg",
            version=_VERSION,
            src_artifact_def_path=None,
            dry_run=False,
        )
        first = typing.cast(
            ReleasePackageRunResult,
            await session.run_action(ReleasePackageAction.__name__, payload),
        )
        annotation_before = _git(tmp_path, "cat-file", "tag", "pkg@1.0.0").stdout
        second = typing.cast(
            ReleasePackageRunResult,
            await session.run_action(ReleasePackageAction.__name__, payload),
        )
        annotation_after = _git(tmp_path, "cat-file", "tag", "pkg@1.0.0").stdout
    assert first.outcome == PackageReleaseOutcome.SKIPPED
    assert second.outcome == PackageReleaseOutcome.SKIPPED
    assert annotation_before == annotation_after


@pytest.mark.asyncio
async def test_cancellation_propagates_through_both_layers(
    tmp_path: pathlib.Path,
) -> None:
    """A listing cancellation reaches the release caller instead of becoming a registry error."""
    from finecode_extension_api.interfaces import iprojectactionrunner as runner_api

    _ = _init_repo(tmp_path)
    _set_script(
        _ChainScript(
            registries=["pypi"],
            published=["pypi"],
            listings={"pypi": runner_api.ActionRunCancelled("stop")},
            dist_dir=str(tmp_path),
        )
    )
    async with handler_test_session(
        project_dir=tmp_path,
        actions=_actions(),
        service_overrides={
            IRepositoryCredentialsProvider: _provider(["pypi"]),
            ilogger.ILogger: NoOpLogger(),
        },
    ) as session:
        with pytest.raises(BaseException):  # noqa: B017 -- cancellation is BaseException
            await session.run_action(
                ReleasePackageAction.__name__,
                ReleasePackageRunPayload(
                    package_name="pkg",
                    version=_VERSION,
                    src_artifact_def_path=None,
                    dry_run=False,
                ),
            )
    assert (
        _git(
            tmp_path, "rev-parse", "--verify", "--quiet", "refs/tags/pkg@1.0.0"
        ).returncode
        != 0
    )
