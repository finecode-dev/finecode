from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ilogger,
    iprojectactionrunner,
    iprojectinfoprovider,
)
from finecode_extension_runner.testing import handler_test_session

from fine_release.build_release_artifact_handler import BuildReleaseArtifactHandler
from fine_release.publish_release_artifact_handler import PublishReleaseArtifactHandler
from fine_release.record_release_tag_handler import RecordReleaseTagHandler
from fine_release.release_package_action import (
    PackageReleaseOutcome,
    RegistryPublishOutcome,
    ReleasePackageAction,
    ReleasePackageRunPayload,
    ReleasePackageRunResult,
)

# Sub-actions are matched on class name: the handlers pass real classes to
# ``ActionRef.from_type``.
GET_SRC_ARTIFACT_REGISTRIES_ACTION = "GetSrcArtifactRegistriesAction"
LIST_PUBLISHED_ARTIFACTS_ACTION = "ListPublishedArtifactsAction"
BUILD_ARTIFACT_ACTION = "BuildArtifactAction"
PUBLISH_AND_VERIFY_ARTIFACT_ACTION = "PublishAndVerifyArtifactAction"
CREATE_GIT_TAG_ACTION = "CreateGitTagAction"

SRC_ARTIFACT_DEF_PATH = "file:///pkg_a/pyproject.toml"


@dataclasses.dataclass
class FakeRegistry:
    name: str


@dataclasses.dataclass
class FakeRegistriesResult:
    registries: list[FakeRegistry]


@dataclasses.dataclass
class FakeListPublishedArtifactsResult:
    filenames: list[str]


@dataclasses.dataclass
class FakeBuildResult:
    build_output_paths: list[str]
    src_artifact_def_path: str


@dataclasses.dataclass
class FakePublishAndVerifyResult:
    published_registries: list[str]
    publish_errors: dict[str, list[str]]
    verification_errors: dict[str, list[str]]
    version: str


@dataclasses.dataclass
class FakeCreateGitTagResult:
    tag: str
    created: bool
    error: str | None = None


@dataclasses.dataclass
class _Raises:
    exception: Exception


class RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.debug_messages: list[str] = []
        self.info_messages: list[str] = []
        self.trace_messages: list[str] = []

    def warning(self, message: str, *args: object, **kwargs: object) -> None:
        self.warnings.append(message)

    def error(self, message: str, *args: object, **kwargs: object) -> None:
        self.errors.append(message)

    def debug(self, message: str, *args: object, **kwargs: object) -> None:
        self.debug_messages.append(message)

    def info(self, message: str, *args: object, **kwargs: object) -> None:
        self.info_messages.append(message)

    def trace(self, message: str, *args: object, **kwargs: object) -> None:
        self.trace_messages.append(message)

    def exception(self, exception: Exception) -> None:
        self.errors.append(str(exception))

    def disable(self, package: str) -> None:
        return None

    def enable(self, package: str) -> None:
        return None


class FakeProjectActionRunner:
    """Fake ``IProjectActionRunner``.

    Records every ``run_action`` call and resolves a canned result per
    sub-action class name. A stored ``_Raises`` raises instead, and a stored
    callable is invoked with the payload so per-registry behaviour can vary.
    """

    def __init__(self) -> None:
        self.recorded_calls: list[tuple[str, object]] = []
        self._results: dict[str, object] = {}

    def set_result(self, action_name: str, result: object) -> None:
        self._results[action_name] = result

    async def run_action(
        self,
        action_type: iprojectactionrunner.ActionRef,
        payload: object,
        meta: object,
        caller_kwargs: object | None = None,
    ) -> object:
        action_name = (
            action_type.action_type.__name__
            if action_type.action_type is not None
            else action_type.source.rsplit(".", 1)[-1]
        )
        self.recorded_calls.append((action_name, payload))
        stored = self._results[action_name]
        if isinstance(stored, _Raises):
            raise stored.exception
        return stored(payload) if callable(stored) else stored

    async def get_actions_for_parent(self, parent_action_type: type) -> dict:
        return {}

    def was_invoked(self, action_name: str) -> bool:
        return any(call[0] == action_name for call in self.recorded_calls)

    def calls_for(self, action_name: str) -> list[object]:
        return [call[1] for call in self.recorded_calls if call[0] == action_name]


class FakeProjectInfoProvider:
    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir

    def get_current_project_dir_path(self) -> Path:
        return self._project_dir

    def get_current_project_def_path(self) -> Path:
        return self._project_dir / "pyproject.toml"


@dataclasses.dataclass
class PackageSpec:
    package_name: str = "pkg-a"
    version: str = "1.0.0"
    registries: list[str] = dataclasses.field(default_factory=lambda: ["pypi"])
    dry_run_published_registries: set[str] = dataclasses.field(default_factory=set)
    dry_run_unreachable_registries: dict[str, str] = dataclasses.field(
        default_factory=dict
    )
    build_output_paths: list[str] = dataclasses.field(
        default_factory=lambda: ["dist/pkg-1.0.0.tar.gz"]
    )
    build_raises: bool = False
    published_registries: list[str] = dataclasses.field(default_factory=list)
    publish_errors: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    verification_errors: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    publish_raises: bool = False
    tag_result: FakeCreateGitTagResult | None = None
    tag_raises: bool = False


def _make_list_published_fn(spec: PackageSpec):
    def _fn(payload: object) -> FakeListPublishedArtifactsResult:
        registry_name = payload.registry_name  # type: ignore[attr-defined]
        if registry_name in spec.dry_run_unreachable_registries:
            raise RuntimeError(spec.dry_run_unreachable_registries[registry_name])
        filenames = (
            ["dist-file.tar.gz"]
            if registry_name in spec.dry_run_published_registries
            else []
        )
        return FakeListPublishedArtifactsResult(filenames=filenames)

    return _fn


def build_runner(spec: PackageSpec) -> FakeProjectActionRunner:
    runner = FakeProjectActionRunner()
    runner.set_result(
        GET_SRC_ARTIFACT_REGISTRIES_ACTION,
        FakeRegistriesResult(
            registries=[FakeRegistry(name=r) for r in spec.registries]
        ),
    )
    runner.set_result(LIST_PUBLISHED_ARTIFACTS_ACTION, _make_list_published_fn(spec))
    if spec.build_raises:
        runner.set_result(BUILD_ARTIFACT_ACTION, _Raises(RuntimeError("build failed")))
    else:
        runner.set_result(
            BUILD_ARTIFACT_ACTION,
            FakeBuildResult(
                build_output_paths=spec.build_output_paths,
                src_artifact_def_path=SRC_ARTIFACT_DEF_PATH,
            ),
        )
    if spec.publish_raises:
        runner.set_result(
            PUBLISH_AND_VERIFY_ARTIFACT_ACTION,
            _Raises(RuntimeError("publish_and_verify_artifact failed before dispatch")),
        )
    else:
        runner.set_result(
            PUBLISH_AND_VERIFY_ARTIFACT_ACTION,
            FakePublishAndVerifyResult(
                published_registries=spec.published_registries,
                publish_errors=spec.publish_errors,
                verification_errors=spec.verification_errors,
                version=spec.version,
            ),
        )
    if spec.tag_raises:
        runner.set_result(
            CREATE_GIT_TAG_ACTION, _Raises(RuntimeError("git unavailable"))
        )
    else:
        runner.set_result(
            CREATE_GIT_TAG_ACTION,
            spec.tag_result
            or FakeCreateGitTagResult(
                tag=f"{spec.package_name}@{spec.version}", created=True, error=None
            ),
        )
    return runner


def make_payload(spec: PackageSpec, dry_run: bool = False) -> ReleasePackageRunPayload:
    return ReleasePackageRunPayload(
        package_name=spec.package_name,
        version=spec.version,
        src_artifact_def_path=SRC_ARTIFACT_DEF_PATH,
        dry_run=dry_run,
    )


_ACTION_NAME = ReleasePackageAction.__name__
_ACTION_SOURCE = (
    f"{ReleasePackageAction.__module__}.{ReleasePackageAction.__qualname__}"
)
_ACTIONS = {
    _ACTION_NAME: {
        "source": _ACTION_SOURCE,
        "handlers": [
            {
                "name": "build",
                "source": (
                    f"{BuildReleaseArtifactHandler.__module__}."
                    f"{BuildReleaseArtifactHandler.__qualname__}"
                ),
            },
            {
                "name": "publish",
                "source": (
                    f"{PublishReleaseArtifactHandler.__module__}."
                    f"{PublishReleaseArtifactHandler.__qualname__}"
                ),
            },
            {
                "name": "record_tag",
                "source": (
                    f"{RecordReleaseTagHandler.__module__}."
                    f"{RecordReleaseTagHandler.__qualname__}"
                ),
            },
        ],
    }
}


async def run_release(
    payload: ReleasePackageRunPayload,
    runner: FakeProjectActionRunner,
    project_dir: Path,
    logger: RecordingLogger | None = None,
) -> ReleasePackageRunResult:
    async with handler_test_session(
        project_dir=project_dir,
        actions=_ACTIONS,
        service_overrides={
            iprojectactionrunner.IProjectActionRunner: runner,
            iprojectinfoprovider.IProjectInfoProvider: FakeProjectInfoProvider(
                project_dir
            ),
            ilogger.ILogger: logger or RecordingLogger(),
        },
    ) as session:
        result = await session.run_action(_ACTION_NAME, payload)
    return result


def _find_registry(result: ReleasePackageRunResult, registry_name: str) -> object:
    return next(r for r in result.registries if r.registry == registry_name)


@pytest.mark.asyncio
async def test_unpublished_package_is_published_and_recorded(tmp_path: Path) -> None:
    """A package absent from its registry is built, published, and reported PUBLISHED under its declared version, so the artifact a consumer installs matches the source tree exactly."""
    spec = PackageSpec(version="1.2.3", published_registries=["pypi"])
    result = await run_release(make_payload(spec), build_runner(spec), tmp_path)

    assert result.version == "1.2.3"
    assert result.outcome == PackageReleaseOutcome.PUBLISHED
    assert _find_registry(result, "pypi").outcome == RegistryPublishOutcome.PUBLISHED
    assert result.error is None


@pytest.mark.asyncio
async def test_already_published_package_is_skipped_without_error(
    tmp_path: Path,
) -> None:
    """Re-releasing an unchanged package reports SKIPPED with a successful return code, so a routine re-run never masquerades as a failure."""
    spec = PackageSpec(published_registries=[])
    result = await run_release(make_payload(spec), build_runner(spec), tmp_path)

    assert result.outcome == PackageReleaseOutcome.SKIPPED
    assert all(r.outcome == RegistryPublishOutcome.SKIPPED for r in result.registries)
    assert result.return_code == code_action.RunReturnCode.SUCCESS


@pytest.mark.asyncio
async def test_registry_absent_from_every_map_is_reported_skipped(
    tmp_path: Path,
) -> None:
    """A configured registry the publish step didn't need to touch is reported SKIPPED rather than omitted, so the per-registry record accounts for every configured registry."""
    spec = PackageSpec(registries=["A", "B"], published_registries=["A"])
    result = await run_release(make_payload(spec), build_runner(spec), tmp_path)

    assert _find_registry(result, "A").outcome == RegistryPublishOutcome.PUBLISHED
    assert _find_registry(result, "B").outcome == RegistryPublishOutcome.SKIPPED
    assert result.outcome == PackageReleaseOutcome.PUBLISHED


@pytest.mark.asyncio
async def test_partial_verification_failure_is_failed_but_still_recorded(
    tmp_path: Path,
) -> None:
    """A package whose upload succeeds on one registry but fails verification on another is FAILED yet still tagged, so a partial publish is never silently treated as success and the registry that accepted it stays traceable."""
    spec = PackageSpec(
        registries=["A", "B"],
        published_registries=["A"],
        verification_errors={"B": ["checksum mismatch"]},
    )
    runner = build_runner(spec)
    result = await run_release(make_payload(spec), runner, tmp_path)

    assert result.outcome == PackageReleaseOutcome.FAILED
    assert _find_registry(result, "A").outcome == RegistryPublishOutcome.PUBLISHED
    b_entry = _find_registry(result, "B")
    assert b_entry.outcome == RegistryPublishOutcome.FAILED
    assert b_entry.errors
    assert runner.was_invoked(CREATE_GIT_TAG_ACTION)
    assert result.created_refs == ["pkg-a@1.0.0"]


@pytest.mark.asyncio
async def test_partial_upload_failure_is_failed_but_still_recorded(
    tmp_path: Path,
) -> None:
    """A package whose upload fails on one registry but succeeds on another is FAILED and still tagged for the registry that accepted it."""
    spec = PackageSpec(
        registries=["A", "B"],
        published_registries=["A"],
        publish_errors={"B": ["connection reset"]},
    )
    runner = build_runner(spec)
    result = await run_release(make_payload(spec), runner, tmp_path)

    assert result.outcome == PackageReleaseOutcome.FAILED
    assert _find_registry(result, "A").outcome == RegistryPublishOutcome.PUBLISHED
    assert _find_registry(result, "B").outcome == RegistryPublishOutcome.FAILED
    assert runner.was_invoked(CREATE_GIT_TAG_ACTION)


@pytest.mark.asyncio
async def test_no_configured_registries_fails_with_an_explanation(
    tmp_path: Path,
) -> None:
    """A package with no resolvable registry is FAILED with an explanatory error rather than silently reported as nothing-to-do, since there is no destination to publish to."""
    spec = PackageSpec(registries=[])
    runner = build_runner(spec)
    result = await run_release(make_payload(spec), runner, tmp_path)

    assert result.outcome == PackageReleaseOutcome.FAILED
    assert result.error is not None
    assert result.registries == []
    assert not runner.was_invoked(PUBLISH_AND_VERIFY_ARTIFACT_ACTION)


@pytest.mark.asyncio
async def test_successful_publish_creates_a_release_tag_but_does_not_push(
    tmp_path: Path,
) -> None:
    """A successful publish is recorded as a tag named `<package>@<version>` and returned for the caller to publish, so pushing stays a single repository-wide operation the orchestrator owns."""
    spec = PackageSpec(published_registries=["pypi"])
    runner = build_runner(spec)
    result = await run_release(make_payload(spec), runner, tmp_path)

    tag_calls = runner.calls_for(CREATE_GIT_TAG_ACTION)
    assert len(tag_calls) == 1
    assert tag_calls[0].tag == "pkg-a@1.0.0"
    assert result.created_refs == ["pkg-a@1.0.0"]
    assert not runner.was_invoked("PushGitRefsAction")


@pytest.mark.asyncio
async def test_already_published_version_reconciles_its_tag(tmp_path: Path) -> None:
    """A version already present in every registry (nothing published this run) still gets its tag reconciled and offered to the push, so a tag that failed to write on an earlier run is retried once the publish itself is a no-op (ADR-0060)."""
    spec = PackageSpec(published_registries=[])
    runner = build_runner(spec)
    result = await run_release(make_payload(spec), runner, tmp_path)

    assert result.outcome == PackageReleaseOutcome.SKIPPED
    assert runner.was_invoked(CREATE_GIT_TAG_ACTION)
    assert result.created_refs == ["pkg-a@1.0.0"]


@pytest.mark.asyncio
async def test_tag_write_failure_fails_the_release(tmp_path: Path) -> None:
    """A tag that fails to write fails the release with a non-zero return code, so a lost tag surfaces in CI instead of hiding behind a green publish — while the registry entry stays PUBLISHED for traceability and the unwritten ref is never handed to the push (ADR-0060)."""
    spec = PackageSpec(
        published_registries=["pypi"],
        tag_result=FakeCreateGitTagResult(
            tag="pkg-a@1.0.0", created=False, error="git tag failed"
        ),
    )
    logger = RecordingLogger()
    result = await run_release(make_payload(spec), build_runner(spec), tmp_path, logger)

    assert result.outcome == PackageReleaseOutcome.FAILED
    assert result.error is not None
    assert result.return_code == code_action.RunReturnCode.ERROR
    assert _find_registry(result, "pypi").outcome == RegistryPublishOutcome.PUBLISHED
    assert result.created_refs == []
    assert len(logger.warnings) >= 1


@pytest.mark.asyncio
async def test_tag_action_raising_fails_the_release(tmp_path: Path) -> None:
    """A tag sub-action that raises outright is treated the same as one reporting an error: the release is FAILED with the error recorded and no ref handed to the push."""
    spec = PackageSpec(published_registries=["pypi"], tag_raises=True)
    logger = RecordingLogger()
    result = await run_release(make_payload(spec), build_runner(spec), tmp_path, logger)

    assert result.outcome == PackageReleaseOutcome.FAILED
    assert result.error is not None
    assert result.return_code == code_action.RunReturnCode.ERROR
    assert result.created_refs == []
    assert len(logger.warnings) >= 1


@pytest.mark.asyncio
async def test_build_failure_is_reported_without_publishing_or_tagging(
    tmp_path: Path,
) -> None:
    """A build failure is returned as a FAILED result rather than raised, and stops the chain — nothing is published or tagged on top of an artifact that was never built."""
    spec = PackageSpec(build_raises=True)
    runner = build_runner(spec)
    result = await run_release(make_payload(spec), runner, tmp_path)

    assert isinstance(result, ReleasePackageRunResult)
    assert result.outcome == PackageReleaseOutcome.FAILED
    assert result.error is not None
    assert result.registries == []
    assert not runner.was_invoked(PUBLISH_AND_VERIFY_ARTIFACT_ACTION)
    assert not runner.was_invoked(CREATE_GIT_TAG_ACTION)


@pytest.mark.asyncio
async def test_pre_dispatch_publish_failure_names_registries_without_fabricating_outcomes(
    tmp_path: Path,
) -> None:
    """When publish-and-verify raises before reaching any registry, the package is FAILED with an error naming the registries that were configured — but carries no per-registry results, since nothing was attempted against them."""
    spec = PackageSpec(registries=["A", "B"], publish_raises=True)
    runner = build_runner(spec)
    result = await run_release(make_payload(spec), runner, tmp_path)

    assert isinstance(result, ReleasePackageRunResult)
    assert result.outcome == PackageReleaseOutcome.FAILED
    assert result.registries == []
    assert result.error is not None
    assert "A" in result.error and "B" in result.error
    assert not runner.was_invoked(CREATE_GIT_TAG_ACTION)


@pytest.mark.asyncio
async def test_dry_run_reports_would_publish_without_touching_publish_actions(
    tmp_path: Path,
) -> None:
    """A dry-run preview never builds, publishes or tags, so previewing a release on a pull request can never accidentally ship anything."""
    spec = PackageSpec()
    runner = build_runner(spec)
    result = await run_release(make_payload(spec, dry_run=True), runner, tmp_path)

    assert result.outcome == PackageReleaseOutcome.WOULD_PUBLISH
    assert not runner.was_invoked(BUILD_ARTIFACT_ACTION)
    assert not runner.was_invoked(PUBLISH_AND_VERIFY_ARTIFACT_ACTION)
    assert not runner.was_invoked(CREATE_GIT_TAG_ACTION)


@pytest.mark.asyncio
async def test_dry_run_of_published_version_reports_skipped(tmp_path: Path) -> None:
    """Previewing a version that is already published reports SKIPPED, so a preview of a workspace with nothing to release shows no pending work."""
    spec = PackageSpec(dry_run_published_registries={"pypi"})
    result = await run_release(
        make_payload(spec, dry_run=True), build_runner(spec), tmp_path
    )

    assert result.outcome == PackageReleaseOutcome.SKIPPED
    assert all(r.outcome == RegistryPublishOutcome.SKIPPED for r in result.registries)


@pytest.mark.asyncio
async def test_dry_run_unreachable_registry_fails_alone_without_aborting_preview(
    tmp_path: Path,
) -> None:
    """One unreachable registry during a preview is reported FAILED for that registry only — the preview still completes and reports the reachable registry's verdict."""
    spec = PackageSpec(
        registries=["reachable", "unreachable"],
        dry_run_unreachable_registries={"unreachable": "connection refused"},
    )
    result = await run_release(
        make_payload(spec, dry_run=True), build_runner(spec), tmp_path
    )

    unreachable_entry = _find_registry(result, "unreachable")
    assert unreachable_entry.outcome == RegistryPublishOutcome.FAILED
    assert unreachable_entry.errors
    assert (
        _find_registry(result, "reachable").outcome
        == RegistryPublishOutcome.WOULD_PUBLISH
    )
