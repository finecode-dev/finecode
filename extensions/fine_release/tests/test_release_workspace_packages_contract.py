from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ilogger,
    iprojectinfoprovider,
    iworkspaceactionrunner,
)
from finecode_extension_runner.testing import handler_test_session

from fine_release.compute_release_order_handler import ComputeReleaseOrderHandler
from fine_release.discover_release_candidates_handler import (
    DiscoverReleaseCandidatesHandler,
)
from fine_release.release_package_action import (
    PackageReleaseOutcome,
    RegistryPublishOutcome,
)
from fine_release.release_workspace_packages_action import (
    ReleaseWorkspacePackagesAction,
    ReleaseWorkspacePackagesRunPayload,
    ReleaseWorkspacePackagesRunResult,
)
from fine_release.sweep_release_packages_handler import SweepReleasePackagesHandler

# Action identity is matched on the sub-action *class name* only: the real handler
# passes an actual class object to ``run_action_in_projects``.
GET_SRC_ARTIFACT_VERSION_ACTION = "GetSrcArtifactVersionAction"
COLLECT_PROJECT_DEPENDENCY_INFO_ACTION = "CollectProjectDependencyInfoAction"
GET_PACKAGE_TRANSITIVE_DEPS_ACTION = "GetPackageTransitiveDepsAction"
SEED_DEPENDENCY_GRAPH_ACTION = "SeedWorkspaceDependencyGraphAction"
DETECT_DEPENDENCY_CYCLES_ACTION = "DetectWorkspaceDependencyCyclesAction"
RELEASE_PACKAGE_ACTION = "ReleasePackageAction"
PUSH_GIT_REFS_ACTION = "PushGitRefsAction"

# Placeholder target for the two graph-wide sub-actions (seed / detect-cycles),
# which aren't a per-candidate fan-out like the others.
WORKSPACE_ROOT_PATH = "workspace_root"


@dataclasses.dataclass
class FakeVersionResult:
    version: str


@dataclasses.dataclass
class FakeDependencyInfoResult:
    package_name: str


@dataclasses.dataclass
class FakeTransitiveDepsResult:
    dependency_names: list[str]


@dataclasses.dataclass
class FakeCyclesResult:
    cycles: list[list[str]]


@dataclasses.dataclass
class FakeRegistryPublishResult:
    registry: str
    outcome: RegistryPublishOutcome
    errors: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class FakeReleasePackageResult:
    """Shaped like ``ReleasePackageRunResult`` in the fields the sweep reads."""

    outcome: PackageReleaseOutcome
    registries: list[FakeRegistryPublishResult] = dataclasses.field(
        default_factory=list
    )
    created_refs: list[str] = dataclasses.field(default_factory=list)
    error: str | None = None


@dataclasses.dataclass
class FakePushGitRefsResult:
    pushed_refs: list[str]
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


class FakeActionRunner:
    """Fake ``IWorkspaceActionRunner``.

    Records every ``run_action_in_projects`` call (sub-action name, targeted
    project paths, payload) and resolves a canned result per
    ``(sub-action name, project path)`` pair set up via ``set_result``. A
    ``project_paths=None`` call broadcasts to every project path registered
    for that sub-action name, mirroring ``run_action_in_projects``'s own
    "None = all projects" contract.
    """

    def __init__(self) -> None:
        self.recorded_calls: list[tuple[str, list[Path] | None, object]] = []
        self._results: dict[tuple[str, Path | None], object] = {}

    def set_result(
        self, action_name: str, result: object, project_path: str | None = None
    ) -> None:
        key_path = Path(project_path) if project_path is not None else None
        self._results[(action_name, key_path)] = result

    async def run_action_in_projects(
        self,
        action_type: type,
        payload: object,
        meta: object,
        project_paths: list[Path] | None = None,
        concurrently: bool = True,
    ) -> dict[Path, object]:
        action_name = action_type.__name__
        self.recorded_calls.append((action_name, project_paths, payload))
        targets = (
            project_paths
            if project_paths is not None
            else [
                path
                for (name, path) in self._results
                if name == action_name and path is not None
            ]
        )
        results: dict[Path, object] = {}
        for path in targets:
            stored = self._results[(action_name, path)]
            if isinstance(stored, _Raises):
                raise stored.exception
            results[path] = stored(payload) if callable(stored) else stored
        return results

    async def run_action_per_project(
        self,
        action_type: type,
        payload_by_project: dict[Path, object],
        meta: object,
        concurrently: bool = True,
    ) -> dict[Path, object]:
        action_name = action_type.__name__
        self.recorded_calls.append(
            (action_name, list(payload_by_project), payload_by_project)
        )
        results: dict[Path, object] = {}
        for path, payload in payload_by_project.items():
            stored = self._results[(action_name, path)]
            if isinstance(stored, _Raises):
                raise stored.exception
            results[path] = stored(payload) if callable(stored) else stored
        return results

    def was_invoked(self, action_name: str, project_path: str | None = None) -> bool:
        target = Path(project_path) if project_path is not None else None
        return any(
            call[0] == action_name
            and (target is None or (call[1] is not None and target in call[1]))
            for call in self.recorded_calls
        )

    def calls_for(self, action_name: str) -> list[tuple[list[Path] | None, object]]:
        return [
            (call[1], call[2]) for call in self.recorded_calls if call[0] == action_name
        ]


class FakeProjectInfoProvider:
    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir

    def get_current_project_dir_path(self) -> Path:
        return self._project_dir

    def get_current_project_def_path(self) -> Path:
        return self._project_dir / "pyproject.toml"


@dataclasses.dataclass
class PackageSpec:
    project_path: str
    package_name: str
    version: str
    depends_on: list[str] = dataclasses.field(default_factory=list)
    outcome: PackageReleaseOutcome = PackageReleaseOutcome.PUBLISHED
    registries: list[FakeRegistryPublishResult] | None = None
    created_refs: list[str] | None = None
    """None -> the package's own tag when it published, otherwise nothing."""
    error: str | None = None
    release_raises: bool = False


def _release_result_for(spec: PackageSpec) -> FakeReleasePackageResult:
    if spec.registries is not None:
        registries = spec.registries
    elif spec.outcome == PackageReleaseOutcome.PUBLISHED:
        registries = [
            FakeRegistryPublishResult(
                registry="pypi", outcome=RegistryPublishOutcome.PUBLISHED
            )
        ]
    elif spec.outcome == PackageReleaseOutcome.SKIPPED:
        registries = [
            FakeRegistryPublishResult(
                registry="pypi", outcome=RegistryPublishOutcome.SKIPPED
            )
        ]
    elif spec.outcome == PackageReleaseOutcome.WOULD_PUBLISH:
        registries = [
            FakeRegistryPublishResult(
                registry="pypi", outcome=RegistryPublishOutcome.WOULD_PUBLISH
            )
        ]
    else:
        registries = [
            FakeRegistryPublishResult(
                registry="pypi",
                outcome=RegistryPublishOutcome.FAILED,
                errors=["registry unreachable"],
            )
        ]

    if spec.created_refs is not None:
        created_refs = spec.created_refs
    elif spec.outcome == PackageReleaseOutcome.PUBLISHED:
        created_refs = [f"{spec.package_name}@{spec.version}"]
    else:
        created_refs = []

    return FakeReleasePackageResult(
        outcome=spec.outcome,
        registries=registries,
        created_refs=created_refs,
        error=spec.error,
    )


def build_runner(
    packages: list[PackageSpec],
    cycles: list[list[str]] | None = None,
    workspace_root: Path | None = None,
    push_result: FakePushGitRefsResult | None = None,
) -> FakeActionRunner:
    runner = FakeActionRunner()
    for pkg in packages:
        runner.set_result(
            GET_SRC_ARTIFACT_VERSION_ACTION,
            FakeVersionResult(version=pkg.version),
            project_path=pkg.project_path,
        )
        runner.set_result(
            COLLECT_PROJECT_DEPENDENCY_INFO_ACTION,
            FakeDependencyInfoResult(package_name=pkg.package_name),
            project_path=pkg.project_path,
        )
        runner.set_result(
            GET_PACKAGE_TRANSITIVE_DEPS_ACTION,
            FakeTransitiveDepsResult(dependency_names=pkg.depends_on),
            project_path=pkg.project_path,
        )
        if pkg.release_raises:
            runner.set_result(
                RELEASE_PACKAGE_ACTION,
                _Raises(RuntimeError("release_package could not be run")),
                project_path=pkg.project_path,
            )
        else:
            runner.set_result(
                RELEASE_PACKAGE_ACTION,
                _release_result_for(pkg),
                project_path=pkg.project_path,
            )

    runner.set_result(
        SEED_DEPENDENCY_GRAPH_ACTION, object(), project_path=WORKSPACE_ROOT_PATH
    )
    runner.set_result(
        DETECT_DEPENDENCY_CYCLES_ACTION,
        FakeCyclesResult(cycles=cycles or []),
        project_path=WORKSPACE_ROOT_PATH,
    )
    if workspace_root is not None:
        runner.set_result(
            PUSH_GIT_REFS_ACTION,
            push_result or FakePushGitRefsResult(pushed_refs=[], error=None),
            project_path=str(workspace_root),
        )
    return runner


def make_payload(
    dry_run: bool = False, project_paths: list[str] | None = None
) -> ReleaseWorkspacePackagesRunPayload:
    return ReleaseWorkspacePackagesRunPayload(
        dry_run=dry_run, project_paths=project_paths
    )


_ACTION_NAME = ReleaseWorkspacePackagesAction.__name__
_ACTION_SOURCE = f"{ReleaseWorkspacePackagesAction.__module__}.{ReleaseWorkspacePackagesAction.__qualname__}"
_ACTIONS = {
    _ACTION_NAME: {
        "source": _ACTION_SOURCE,
        "handlers": [
            {
                "name": "discover",
                "source": (
                    f"{DiscoverReleaseCandidatesHandler.__module__}."
                    f"{DiscoverReleaseCandidatesHandler.__qualname__}"
                ),
            },
            {
                "name": "order",
                "source": (
                    f"{ComputeReleaseOrderHandler.__module__}."
                    f"{ComputeReleaseOrderHandler.__qualname__}"
                ),
            },
            {
                "name": "sweep",
                "source": (
                    f"{SweepReleasePackagesHandler.__module__}."
                    f"{SweepReleasePackagesHandler.__qualname__}"
                ),
            },
        ],
    }
}


async def run_sweep(
    payload: ReleaseWorkspacePackagesRunPayload,
    runner: FakeActionRunner,
    project_dir: Path,
    logger: RecordingLogger | None = None,
) -> ReleaseWorkspacePackagesRunResult:
    async with handler_test_session(
        project_dir=project_dir,
        actions=_ACTIONS,
        service_overrides={
            iworkspaceactionrunner.IWorkspaceActionRunner: runner,
            iprojectinfoprovider.IProjectInfoProvider: FakeProjectInfoProvider(
                project_dir
            ),
            ilogger.ILogger: logger or RecordingLogger(),
        },
    ) as session:
        result = await session.run_action(_ACTION_NAME, payload)
    return result


def _find_package(
    result: ReleaseWorkspacePackagesRunResult, package_name: str
) -> object:
    return next(p for p in result.packages if p.package_name == package_name)


def _find_registry(package: object, registry_name: str) -> object:
    return next(r for r in package.registries if r.registry == registry_name)


def _diamond_packages(b_succeeds: bool) -> list[PackageSpec]:
    return [
        PackageSpec(
            project_path="pkg_a",
            package_name="pkg-a",
            version="1.0.0",
            outcome=PackageReleaseOutcome.SKIPPED,
        ),
        PackageSpec(
            project_path="pkg_b",
            package_name="pkg-b",
            version="1.0.0",
            outcome=(
                PackageReleaseOutcome.PUBLISHED
                if b_succeeds
                else PackageReleaseOutcome.FAILED
            ),
        ),
        PackageSpec(
            project_path="pkg_c",
            package_name="pkg-c",
            version="1.0.0",
            depends_on=["pkg-b"],
        ),
        PackageSpec(project_path="pkg_d", package_name="pkg-d", version="1.0.0"),
    ]


@pytest.mark.asyncio
async def test_unpublished_package_is_released_under_its_declared_version(
    tmp_path: Path,
) -> None:
    """A released package is reported under the exact declared version, so operators never see a mismatch between the source tree and what the release recorded."""
    pkg = PackageSpec(project_path="pkg_a", package_name="pkg-a", version="1.2.3")
    runner = build_runner([pkg], workspace_root=tmp_path)
    result = await run_sweep(make_payload(), runner, tmp_path)

    package = _find_package(result, "pkg-a")
    assert package.version == "1.2.3"
    assert package.outcome == PackageReleaseOutcome.PUBLISHED
    assert _find_registry(package, "pypi").outcome == RegistryPublishOutcome.PUBLISHED


@pytest.mark.asyncio
async def test_already_published_package_is_skipped_without_error(
    tmp_path: Path,
) -> None:
    """Re-running the sweep on an unchanged repo reports SKIPPED with a successful return code, so a no-op run never masquerades as a failure in CI."""
    pkg = PackageSpec(
        project_path="pkg_a",
        package_name="pkg-a",
        version="1.0.0",
        outcome=PackageReleaseOutcome.SKIPPED,
    )
    runner = build_runner([pkg], workspace_root=tmp_path)
    result = await run_sweep(make_payload(), runner, tmp_path)

    package = _find_package(result, "pkg-a")
    assert package.outcome == PackageReleaseOutcome.SKIPPED
    assert result.return_code == code_action.RunReturnCode.SUCCESS


@pytest.mark.asyncio
async def test_fully_published_workspace_reports_no_failures(tmp_path: Path) -> None:
    """A workspace where every candidate is already published completes cleanly, so a routine re-run of the release job never produces spurious CI failures."""
    packages = [
        PackageSpec(
            project_path=f"pkg_{name}",
            package_name=f"pkg-{name}",
            version="1.0.0",
            outcome=PackageReleaseOutcome.SKIPPED,
        )
        for name in ("a", "b", "c")
    ]
    runner = build_runner(packages, workspace_root=tmp_path)
    result = await run_sweep(make_payload(), runner, tmp_path)

    assert len(result.packages) == 3
    assert all(p.outcome == PackageReleaseOutcome.SKIPPED for p in result.packages)
    assert result.return_code == code_action.RunReturnCode.SUCCESS


@pytest.mark.asyncio
async def test_every_candidate_gets_a_recognized_outcome(tmp_path: Path) -> None:
    """Every candidate package always carries one of the defined outcome values, so downstream tooling summarizing a release never has to special-case a missing or null outcome."""
    packages = [
        PackageSpec(project_path=f"pkg_{i}", package_name=f"pkg-{i}", version="1.0.0")
        for i in range(4)
    ]
    runner = build_runner(packages, workspace_root=tmp_path)
    result = await run_sweep(make_payload(), runner, tmp_path)

    assert len(result.packages) == 4
    for package in result.packages:
        assert package.outcome in set(PackageReleaseOutcome)
        assert package.outcome is not None


@pytest.mark.asyncio
async def test_failure_blocks_only_its_transitive_dependent_on_rerun(
    tmp_path: Path,
) -> None:
    """When a dependency's release keeps failing, only packages that actually depend on it are withheld — an unrelated package still ships in the same run."""
    runner = build_runner(_diamond_packages(b_succeeds=False), workspace_root=tmp_path)
    result = await run_sweep(make_payload(), runner, tmp_path)

    assert _find_package(result, "pkg-a").outcome == PackageReleaseOutcome.SKIPPED
    assert _find_package(result, "pkg-b").outcome == PackageReleaseOutcome.FAILED
    assert _find_package(result, "pkg-c").outcome == PackageReleaseOutcome.BLOCKED
    assert _find_package(result, "pkg-d").outcome == PackageReleaseOutcome.PUBLISHED


@pytest.mark.asyncio
async def test_previously_failed_dependency_unblocks_dependents_once_it_succeeds(
    tmp_path: Path,
) -> None:
    """Once a previously-failing dependency is fixed and republishes successfully, its blocked dependent is released in the very same run — resumability requires no manual unblocking step."""
    runner = build_runner(_diamond_packages(b_succeeds=True), workspace_root=tmp_path)
    result = await run_sweep(make_payload(), runner, tmp_path)

    assert _find_package(result, "pkg-a").outcome == PackageReleaseOutcome.SKIPPED
    assert _find_package(result, "pkg-b").outcome == PackageReleaseOutcome.PUBLISHED
    assert _find_package(result, "pkg-c").outcome == PackageReleaseOutcome.PUBLISHED
    assert _find_package(result, "pkg-d").outcome == PackageReleaseOutcome.PUBLISHED
    assert result.return_code == code_action.RunReturnCode.SUCCESS


@pytest.mark.asyncio
async def test_dependency_is_ordered_before_its_dependent(tmp_path: Path) -> None:
    """An unpublished dependency is always released before the package that depends on it, so a consumer resolving the dependent's declared requirement never hits a not-yet-published version."""
    dep = PackageSpec(project_path="pkg_dep", package_name="pkg-dep", version="1.0.0")
    dependent = PackageSpec(
        project_path="pkg_dependent",
        package_name="pkg-dependent",
        version="1.0.0",
        depends_on=["pkg-dep"],
    )
    runner = build_runner([dependent, dep], workspace_root=tmp_path)
    result = await run_sweep(make_payload(), runner, tmp_path)

    names_in_order = [p.package_name for p in result.packages]
    assert names_in_order.index("pkg-dep") < names_in_order.index("pkg-dependent")
    assert _find_package(result, "pkg-dep").outcome == PackageReleaseOutcome.PUBLISHED
    assert (
        _find_package(result, "pkg-dependent").outcome
        == PackageReleaseOutcome.PUBLISHED
    )


@pytest.mark.asyncio
async def test_dependent_of_a_failed_release_is_never_attempted(tmp_path: Path) -> None:
    """A package whose dependency failed is never released at all — its result carries no registry attempts, and its own release action is never invoked, since it would depend on a version the registry never accepted."""
    dep = PackageSpec(
        project_path="pkg_dep",
        package_name="pkg-dep",
        version="1.0.0",
        outcome=PackageReleaseOutcome.FAILED,
    )
    dependent = PackageSpec(
        project_path="pkg_dependent",
        package_name="pkg-dependent",
        version="1.0.0",
        depends_on=["pkg-dep"],
    )
    runner = build_runner([dep, dependent], workspace_root=tmp_path)
    result = await run_sweep(make_payload(), runner, tmp_path)

    assert _find_package(result, "pkg-dep").outcome == PackageReleaseOutcome.FAILED
    blocked = _find_package(result, "pkg-dependent")
    assert blocked.outcome == PackageReleaseOutcome.BLOCKED
    assert blocked.registries == []
    assert not runner.was_invoked(RELEASE_PACKAGE_ACTION, project_path="pkg_dependent")
    assert result.return_code == code_action.RunReturnCode.ERROR


@pytest.mark.asyncio
async def test_failed_release_result_is_recorded_with_its_own_diagnosis(
    tmp_path: Path,
) -> None:
    """A package release that reports failure (rather than raising) keeps the error and per-registry detail its own chain produced, so the operator sees why it failed without the orchestrator re-deriving anything."""
    failed = PackageSpec(
        project_path="pkg_x",
        package_name="pkg-x",
        version="1.0.0",
        outcome=PackageReleaseOutcome.FAILED,
        error="Build failed for pkg-x 1.0.0: compiler exploded",
        registries=[],
    )
    runner = build_runner([failed], workspace_root=tmp_path)
    result = await run_sweep(make_payload(), runner, tmp_path)

    package = _find_package(result, "pkg-x")
    assert package.outcome == PackageReleaseOutcome.FAILED
    assert package.error == "Build failed for pkg-x 1.0.0: compiler exploded"
    assert package.registries == []
    assert result.return_code == code_action.RunReturnCode.ERROR


@pytest.mark.asyncio
async def test_package_release_that_raises_fails_only_that_package(
    tmp_path: Path,
) -> None:
    """When the package release cannot be run at all — a runner crash rather than a release failure — that package is FAILED with the error recorded and the sweep still attempts every independent candidate."""
    broken = PackageSpec(
        project_path="pkg_p", package_name="pkg-p", version="1.0.0", release_raises=True
    )
    healthy = PackageSpec(project_path="pkg_r", package_name="pkg-r", version="1.0.0")
    runner = build_runner([broken, healthy], workspace_root=tmp_path)
    result = await run_sweep(make_payload(), runner, tmp_path)

    package_p = _find_package(result, "pkg-p")
    assert package_p.outcome == PackageReleaseOutcome.FAILED
    assert package_p.error is not None
    assert package_p.registries == []
    assert _find_package(result, "pkg-r").outcome == PackageReleaseOutcome.PUBLISHED


@pytest.mark.asyncio
async def test_failure_of_an_independent_package_does_not_halt_the_sweep(
    tmp_path: Path,
) -> None:
    """A failure in one package never stops the sweep for packages that don't depend on it — the release run always attempts every independent candidate."""
    x = PackageSpec(
        project_path="pkg_x",
        package_name="pkg-x",
        version="1.0.0",
        outcome=PackageReleaseOutcome.FAILED,
    )
    y = PackageSpec(project_path="pkg_y", package_name="pkg-y", version="1.0.0")
    runner = build_runner([x, y], workspace_root=tmp_path)
    result = await run_sweep(make_payload(), runner, tmp_path)

    assert _find_package(result, "pkg-x").outcome == PackageReleaseOutcome.FAILED
    assert _find_package(result, "pkg-y").outcome == PackageReleaseOutcome.PUBLISHED
    assert result.return_code == code_action.RunReturnCode.ERROR


@pytest.mark.asyncio
async def test_dry_run_delegates_in_preview_mode_and_pushes_nothing(
    tmp_path: Path,
) -> None:
    """A dry run delegates to each package in preview mode and pushes nothing, so previewing a release set on a pull request can never accidentally publish or move a ref."""
    pkg = PackageSpec(
        project_path="pkg_a",
        package_name="pkg-a",
        version="1.0.0",
        outcome=PackageReleaseOutcome.WOULD_PUBLISH,
    )
    runner = build_runner([pkg], workspace_root=tmp_path)
    result = await run_sweep(make_payload(dry_run=True), runner, tmp_path)

    assert _find_package(result, "pkg-a").outcome == PackageReleaseOutcome.WOULD_PUBLISH
    release_calls = runner.calls_for(RELEASE_PACKAGE_ACTION)
    assert len(release_calls) == 1
    assert release_calls[0][1].dry_run is True
    assert not runner.was_invoked(PUSH_GIT_REFS_ACTION)


@pytest.mark.asyncio
async def test_dry_run_of_fully_published_workspace_reports_no_failures(
    tmp_path: Path,
) -> None:
    """Previewing a release set where every version is already published reports a clean SKIPPED-only preview, so the PR preview never shows failures for a workspace with nothing to release."""
    pkg = PackageSpec(
        project_path="pkg_a",
        package_name="pkg-a",
        version="1.0.0",
        outcome=PackageReleaseOutcome.SKIPPED,
    )
    runner = build_runner([pkg], workspace_root=tmp_path)
    result = await run_sweep(make_payload(dry_run=True), runner, tmp_path)

    assert all(p.outcome == PackageReleaseOutcome.SKIPPED for p in result.packages)
    assert result.return_code == code_action.RunReturnCode.SUCCESS


@pytest.mark.asyncio
async def test_refs_from_every_released_package_are_published_in_one_push(
    tmp_path: Path,
) -> None:
    """Every ref a run created reaches the remote in a single push, so a release of many packages costs one repository-wide operation rather than one per package."""
    packages = [
        PackageSpec(
            project_path=f"pkg_{name}", package_name=f"pkg-{name}", version="1.0.0"
        )
        for name in ("a", "b", "c")
    ]
    runner = build_runner(packages, workspace_root=tmp_path)
    await run_sweep(make_payload(), runner, tmp_path)

    push_calls = runner.calls_for(PUSH_GIT_REFS_ACTION)
    assert len(push_calls) == 1
    project_paths, push_payload = push_calls[0]
    assert project_paths == [tmp_path]
    assert set(push_payload.refs) == {"pkg-a@1.0.0", "pkg-b@1.0.0", "pkg-c@1.0.0"}


@pytest.mark.asyncio
async def test_refs_are_published_even_when_some_packages_failed(
    tmp_path: Path,
) -> None:
    """A failed package never discards the records of packages that succeeded — the push still happens, carrying every ref the run did create."""
    failed = PackageSpec(
        project_path="pkg_x",
        package_name="pkg-x",
        version="1.0.0",
        outcome=PackageReleaseOutcome.FAILED,
    )
    succeeded = PackageSpec(project_path="pkg_y", package_name="pkg-y", version="1.0.0")
    runner = build_runner([failed, succeeded], workspace_root=tmp_path)
    result = await run_sweep(make_payload(), runner, tmp_path)

    push_calls = runner.calls_for(PUSH_GIT_REFS_ACTION)
    assert len(push_calls) == 1
    assert push_calls[0][1].refs == ["pkg-y@1.0.0"]
    assert result.return_code == code_action.RunReturnCode.ERROR


@pytest.mark.asyncio
async def test_no_push_when_the_run_created_no_refs(tmp_path: Path) -> None:
    """A run that published nothing performs no push at all, so a no-op release never touches the remote."""
    pkg = PackageSpec(
        project_path="pkg_a",
        package_name="pkg-a",
        version="1.0.0",
        outcome=PackageReleaseOutcome.SKIPPED,
    )
    runner = build_runner([pkg], workspace_root=tmp_path)
    await run_sweep(make_payload(), runner, tmp_path)

    assert not runner.was_invoked(PUSH_GIT_REFS_ACTION)


@pytest.mark.asyncio
async def test_push_failure_fails_the_run_without_unpublishing(
    tmp_path: Path,
) -> None:
    """A push that never reaches the remote fails the run with a non-zero return code so the lost refs surface in CI, while the packages themselves stay PUBLISHED — the publish already succeeded and is never undone (ADR-0060). Reconciling per-package tagging re-offers the refs on the next run, so the push is retried."""
    pkg = PackageSpec(project_path="pkg_a", package_name="pkg-a", version="1.0.0")
    runner = build_runner(
        [pkg],
        workspace_root=tmp_path,
        push_result=FakePushGitRefsResult(pushed_refs=[], error="push rejected"),
    )
    logger = RecordingLogger()
    result = await run_sweep(make_payload(), runner, tmp_path, logger=logger)

    assert _find_package(result, "pkg-a").outcome == PackageReleaseOutcome.PUBLISHED
    assert result.error is not None
    assert result.return_code == code_action.RunReturnCode.ERROR
    assert len(logger.warnings) >= 1


@pytest.mark.asyncio
async def test_project_paths_narrows_the_candidate_set(tmp_path: Path) -> None:
    """Restricting the release run to an explicit set of project paths releases only those projects — an operator re-releasing a single package never touches the rest of the workspace."""
    packages = [
        PackageSpec(
            project_path=f"pkg_{name}", package_name=f"pkg-{name}", version="1.0.0"
        )
        for name in ("a", "b", "c")
    ]
    runner = build_runner(packages, workspace_root=tmp_path)
    result = await run_sweep(
        make_payload(project_paths=["pkg_a", "pkg_b"]), runner, tmp_path
    )

    assert {p.package_name for p in result.packages} == {"pkg-a", "pkg-b"}


@pytest.mark.asyncio
async def test_dependency_cycle_fails_before_any_package_is_touched(
    tmp_path: Path,
) -> None:
    """A dependency cycle among candidates is detected and fails the whole action before any package is released, since the computed order can no longer be trusted for any candidate."""
    a = PackageSpec(
        project_path="pkg_a",
        package_name="pkg-a",
        version="1.0.0",
        depends_on=["pkg-b"],
    )
    b = PackageSpec(
        project_path="pkg_b",
        package_name="pkg-b",
        version="1.0.0",
        depends_on=["pkg-a"],
    )
    runner = build_runner([a, b], cycles=[["pkg-a", "pkg-b"]], workspace_root=tmp_path)

    with pytest.raises(Exception):
        await run_sweep(make_payload(), runner, tmp_path)

    assert not runner.was_invoked(RELEASE_PACKAGE_ACTION)
    assert not runner.was_invoked(PUSH_GIT_REFS_ACTION)


@pytest.mark.asyncio
async def test_candidate_order_is_deterministic_regardless_of_discovery_order(
    tmp_path: Path,
) -> None:
    """The release order among unrelated candidates is always by package name, never by the concurrent fan-out's arrival order, so two runs against the same repo state always produce the same order."""

    def _specs(names: list[str]) -> list[PackageSpec]:
        return [
            PackageSpec(
                project_path=f"pkg_{name}", package_name=f"pkg-{name}", version="1.0.0"
            )
            for name in names
        ]

    runner_one = build_runner(_specs(["c", "a", "b"]), workspace_root=tmp_path / "one")
    runner_two = build_runner(_specs(["b", "c", "a"]), workspace_root=tmp_path / "two")

    result_one = await run_sweep(make_payload(), runner_one, tmp_path / "one")
    result_two = await run_sweep(make_payload(), runner_two, tmp_path / "two")

    names_one = [p.package_name for p in result_one.packages]
    names_two = [p.package_name for p in result_two.packages]
    assert names_one == names_two == ["pkg-a", "pkg-b", "pkg-c"]
