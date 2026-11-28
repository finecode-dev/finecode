"""Verification handler tests.

Every test runs on a virtual-time event loop: the clock jumps to the next timer
instead of waiting for it, so the handler's real 60 s deadline and retry delays
run in milliseconds and the tests measure them from dispatch times.
"""

from __future__ import annotations

import asyncio
import itertools
import pathlib
import typing

import pytest
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_api.interfaces.irepositorycredentialsprovider import (
    IRepositoryCredentialsProvider,
    Repository,
)
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.impls.repository_credentials_provider import (
    ConfigRepositoryCredentialsProvider,
    RepositoryCredentialsProviderConfig,
)
from finecode_extension_runner.testing import run_handler, run_on_virtual_time

from fine_dist_artifacts import (
    verify_artifact_published_to_registry_handler as handler_module,
)
from fine_dist_artifacts.list_published_artifacts_action import (
    ListPublishedArtifactsRunResult,
)
from fine_dist_artifacts.verify_artifact_published_to_registry_action import (
    VerifyArtifactPublishedToRegistryAction,
    VerifyArtifactPublishedToRegistryRunPayload,
    VerifyArtifactPublishedToRegistryRunResult,
)

_REGISTRY = "local"
_INDEX = "https://example.test/simple/"
_WHEEL = "pkg-1.0.0-py3-none-any.whl"
_SDIST = "pkg-1.0.0.tar.gz"


def _payload(
    tmp_path: pathlib.Path, filenames: tuple[str, ...] = (_WHEEL, _SDIST)
) -> VerifyArtifactPublishedToRegistryRunPayload:
    return VerifyArtifactPublishedToRegistryRunPayload(
        src_artifact_def_path=path_to_resource_uri(tmp_path / "pyproject.toml"),
        dist_artifact_paths=[
            path_to_resource_uri(tmp_path / name) for name in filenames
        ],
        registry_name=_REGISTRY,
        version="1.0.0",
    )


class _FakeRunner:
    """Scripted stand-in for IProjectActionRunner recording listing dispatches.

    Each behavior is ("result", filenames), ("error", exception),
    ("slow", (seconds, filenames)) or ("hang", ignores_cancellation). A hang ends
    when `release` is set.
    """

    def __init__(self, behaviors: list[tuple[str, typing.Any]]) -> None:
        self._behaviors = list(behaviors)
        self.calls: list[typing.Any] = []
        self.dispatch_times: list[float] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    async def get_actions_for_parent(
        self, parent_action_type: type[typing.Any]
    ) -> dict[str, typing.Any]:
        raise NotImplementedError

    def run_action_iter(self, *args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        raise NotImplementedError

    async def run_action(
        self,
        action_type: iprojectactionrunner.ActionRef,
        payload: typing.Any,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> ListPublishedArtifactsRunResult:
        del action_type, meta, caller_kwargs
        self.calls.append(payload)
        self.dispatch_times.append(asyncio.get_running_loop().time())
        self.started.set()
        try:
            kind, value = self._behaviors.pop(0)
            if kind == "result":
                return ListPublishedArtifactsRunResult(filenames=list(value))
            if kind == "error":
                raise value
            if kind == "slow":
                seconds, filenames = value
                await asyncio.sleep(seconds)
                return ListPublishedArtifactsRunResult(filenames=list(filenames))
            assert kind == "hang"
            while True:
                try:
                    _ = await self.release.wait()
                    return ListPublishedArtifactsRunResult(filenames=[])
                except asyncio.CancelledError:
                    if not value:
                        raise
        finally:
            self.finished.set()

    def gaps(self) -> list[float]:
        times = self.dispatch_times
        return [later - earlier for earlier, later in itertools.pairwise(times)]


async def _verify(
    tmp_path: pathlib.Path,
    runner: _FakeRunner,
    payload: VerifyArtifactPublishedToRegistryRunPayload | None = None,
) -> list[str]:
    result = await run_handler(
        handler_module.VerifyArtifactPublishedToRegistryHandler,
        payload if payload is not None else _payload(tmp_path),
        action_cls=VerifyArtifactPublishedToRegistryAction,
        project_dir=tmp_path,
        service_overrides={
            iprojectactionrunner.IProjectActionRunner: runner,
            IRepositoryCredentialsProvider: ConfigRepositoryCredentialsProvider(
                RepositoryCredentialsProviderConfig(
                    repositories=[
                        Repository(name=_REGISTRY, index_url=_INDEX, upload_url=_INDEX)
                    ]
                )
            ),
        },
    )
    assert isinstance(result, VerifyArtifactPublishedToRegistryRunResult)
    return result.errors


def test_empty_payload_performs_no_listing(tmp_path: pathlib.Path) -> None:
    """An empty distribution list is vacuously complete so callers never wait on nothing."""
    runner = _FakeRunner([])
    payload = _payload(tmp_path, filenames=())
    assert run_on_virtual_time(_verify(tmp_path, runner, payload)) == []
    assert runner.calls == []


def test_success_first_attempt(tmp_path: pathlib.Path) -> None:
    """Complete listings on the first check confirm the upload without further waiting."""
    runner = _FakeRunner([("result", [_WHEEL, _SDIST])])
    assert run_on_virtual_time(_verify(tmp_path, runner)) == []
    assert len(runner.calls) == 1
    assert runner.calls[0].version == "1.0.0"
    assert runner.calls[0].registry_name == _REGISTRY


@pytest.mark.parametrize(
    "first",
    [("result", [_WHEEL]), ("error", RuntimeError("boom"))],
    ids=["missing-file", "listing-error"],
)
def test_transient_failure_then_complete_retries_once(
    tmp_path: pathlib.Path, first: tuple[str, typing.Any]
) -> None:
    """A missing file or a listing error followed by a complete listing recovers so transient registry lag does not fail releases."""
    runner = _FakeRunner([first, ("result", [_WHEEL, _SDIST])])
    assert run_on_virtual_time(_verify(tmp_path, runner)) == []
    assert runner.gaps() == pytest.approx([1.0])


def test_exhaustion_returns_one_error_per_missing_file(tmp_path: pathlib.Path) -> None:
    """Persistent absence reports each missing file after five attempts with increasing delays, so operators know exactly what never appeared."""
    runner = _FakeRunner([("result", [_WHEEL])] * 5)
    errors = run_on_virtual_time(_verify(tmp_path, runner))
    assert len(errors) == 1
    assert _SDIST in errors[0]
    assert _INDEX in errors[0]
    assert "5 of 5 attempts (attempt limit exhausted)" in errors[0]
    assert runner.gaps() == pytest.approx([1.0, 2.0, 4.0, 8.0])


def test_exhaustion_after_errors_raises(tmp_path: pathlib.Path) -> None:
    """Persistent listing errors raise so an unreachable index is not mistaken for missing files."""
    runner = _FakeRunner([("error", RuntimeError("down"))] * 5)
    with pytest.raises(
        iprojectactionrunner.ActionRunFailed, match=r"attempt limit exhausted.*down"
    ):
        _ = run_on_virtual_time(_verify(tmp_path, runner))
    assert runner.gaps() == pytest.approx([1.0, 2.0, 4.0, 8.0])


@pytest.mark.parametrize(
    "exc", [asyncio.CancelledError(), iprojectactionrunner.ActionRunCancelled("stop")]
)
def test_cancellation_propagates_without_retry(
    tmp_path: pathlib.Path, exc: BaseException
) -> None:
    """Run cancellation must reach the caller immediately so aborted releases do not keep polling."""
    runner = _FakeRunner([("error", exc)])
    with pytest.raises(type(exc)):
        _ = run_on_virtual_time(_verify(tmp_path, runner))
    assert len(runner.calls) == 1


def test_caller_cancellation_cancels_the_listing(tmp_path: pathlib.Path) -> None:
    """Cancelling verification cancels the in-flight listing so an aborted release leaves no registry query running."""
    runner = _FakeRunner([("hang", False)])

    async def scenario() -> None:
        verification = asyncio.create_task(_verify(tmp_path, runner))
        _ = await runner.started.wait()
        _ = verification.cancel()
        with pytest.raises(asyncio.CancelledError):
            _ = await verification
        _ = await runner.finished.wait()

    run_on_virtual_time(scenario())
    assert not runner.release.is_set()


@pytest.mark.parametrize(
    "ignores_cancellation", [False, True], ids=["cooperative", "ignores-cancellation"]
)
def test_hanging_first_attempt_reports_all_files_missing_at_deadline(
    tmp_path: pathlib.Path, ignores_cancellation: bool
) -> None:
    """A hung first listing returns at the 60 s deadline with every file unconfirmed, even when the listing never acknowledges cancellation."""
    runner = _FakeRunner([("hang", ignores_cancellation)])
    reports: list[dict[str, typing.Any]] = []

    async def scenario() -> tuple[list[str], float]:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: reports.append(context))
        errors = await _verify(tmp_path, runner)
        elapsed = loop.time()
        if ignores_cancellation:
            assert not runner.finished.is_set()
        runner.release.set()
        _ = await runner.finished.wait()
        await asyncio.sleep(0)
        return errors, elapsed

    errors, elapsed = run_on_virtual_time(scenario())
    assert elapsed == pytest.approx(60.0)
    assert len(errors) == 2
    assert all("60 s deadline exhausted" in error for error in errors)
    assert reports == []


@pytest.mark.parametrize(
    "first",
    [("result", [_WHEEL]), ("error", RuntimeError("down"))],
    ids=["missing-file", "listing-error"],
)
def test_later_hang_keeps_last_completed_observation(
    tmp_path: pathlib.Path, first: tuple[str, typing.Any]
) -> None:
    """A listing that hangs after a completed one does not erase what was observed, so the report reflects the registry's last answer."""
    runner = _FakeRunner([first, ("hang", False)])
    if first[0] == "error":
        with pytest.raises(
            iprojectactionrunner.ActionRunFailed, match=r"deadline exhausted.*down"
        ):
            _ = run_on_virtual_time(_verify(tmp_path, runner))
    else:
        errors = run_on_virtual_time(_verify(tmp_path, runner))
        assert len(errors) == 1
        assert _SDIST in errors[0]
    assert len(runner.calls) == 2


def test_retry_delay_is_clamped_to_the_deadline(tmp_path: pathlib.Path) -> None:
    """A retry delay never runs past the deadline and no attempt starts at it, so verification ends on time."""
    runner = _FakeRunner([("slow", (59.5, [_WHEEL]))])

    async def scenario() -> tuple[list[str], float]:
        errors = await _verify(tmp_path, runner)
        return errors, asyncio.get_running_loop().time()

    errors, elapsed = run_on_virtual_time(scenario())
    assert elapsed == pytest.approx(60.0)
    assert len(runner.calls) == 1
    assert len(errors) == 1
    assert "1 of 5 attempts (60 s deadline exhausted)" in errors[0]
