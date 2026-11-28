"""Publish-and-verify wrapper tests.

Every test runs on a virtual-time event loop (`run_on_virtual_time`): delays and
the verification handler's real 60 s deadline take milliseconds.
"""

from __future__ import annotations

import asyncio
import collections.abc
import gc
import pathlib
import types
import typing

import pytest
from fine_dist_artifacts import (
    verify_artifact_published_to_registry_handler as verify_module,
)
from fine_dist_artifacts.list_published_artifacts_action import (
    ListPublishedArtifactsRunResult,
)
from fine_dist_artifacts.publish_artifact_action import PublishArtifactRunResult
from fine_dist_artifacts.verify_artifact_published_to_registry_action import (
    VerifyArtifactPublishedToRegistryRunResult,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner, iprojectinfoprovider
from finecode_extension_api.interfaces.irepositorycredentialsprovider import Repository
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.impls.repository_credentials_provider import (
    ConfigRepositoryCredentialsProvider,
    RepositoryCredentialsProviderConfig,
)
from finecode_extension_runner.testing import run_handler, run_on_virtual_time

from finecode_dev_extensions.publish_and_verify_artifact_action import (
    PublishAndVerifyArtifactAction,
    PublishAndVerifyArtifactRunPayload,
    PublishAndVerifyArtifactRunResult,
)
from finecode_dev_extensions.publish_and_verify_artifact_handler import (
    PublishAndVerifyArtifactHandler,
)

_Verify = collections.abc.Callable[
    [typing.Any, code_action.RunActionMeta],
    collections.abc.Awaitable[VerifyArtifactPublishedToRegistryRunResult],
]


class _ScriptedRunner:
    """Fake IProjectActionRunner: canned publish result, per-registry verify behavior.

    Verify behaviors: ("ok", errors), ("raise", exception),
    ("delay", seconds, errors), or ("hang", ignores_cancellation, late_exception).
    A hang records its cancellation in `cancelled`, ends when `release` is set
    (raising `late_exception` if given), and then sets `settled`.
    `verify` replaces the scripted behaviors with a real verification.
    """

    def __init__(
        self,
        version: str = "1.0.0",
        published: list[str] | None = None,
        failed: dict[str, str] | None = None,
        verify_script: dict[str, tuple[typing.Any, ...]] | None = None,
        verify: _Verify | None = None,
    ) -> None:
        self._version = version
        self._published = published if published is not None else ["local"]
        self._failed = dict(failed or {})
        self._verify_script = dict(verify_script or {})
        self._verify = verify
        self.publish_payloads: list[typing.Any] = []
        self.verify_payloads: list[typing.Any] = []
        self.cancelled: set[str] = set()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.settled = asyncio.Event()

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
    ) -> typing.Any:
        del caller_kwargs
        source = action_type.source
        if "PublishArtifactAction" in source:
            self.publish_payloads.append(payload)
            return PublishArtifactRunResult(
                version=self._version,
                published_registries=list(self._published),
                failed_registries=dict(self._failed),
            )
        if "VerifyArtifactPublishedToRegistryAction" in source:
            self.verify_payloads.append(payload)
            if self._verify is not None:
                return await self._verify(payload, meta)
            return await self._scripted_verify(payload.registry_name)
        raise AssertionError(f"unexpected dispatch to {source}")

    async def _scripted_verify(
        self, registry_name: str
    ) -> VerifyArtifactPublishedToRegistryRunResult:
        kind, *args = self._verify_script.get(registry_name, ("ok", []))
        if kind == "ok":
            return VerifyArtifactPublishedToRegistryRunResult(errors=list(args[0]))
        if kind == "raise":
            raise args[0]
        if kind == "delay":
            await asyncio.sleep(args[0])
            return VerifyArtifactPublishedToRegistryRunResult(errors=list(args[1]))
        assert kind == "hang"
        ignores_cancellation, late_exception = args
        self.started.set()
        try:
            while True:
                try:
                    _ = await self.release.wait()
                    break
                except asyncio.CancelledError:
                    self.cancelled.add(registry_name)
                    if not ignores_cancellation:
                        raise
            if late_exception is not None:
                raise late_exception
            return VerifyArtifactPublishedToRegistryRunResult(errors=[])
        finally:
            self.settled.set()


class _StubProjectInfoProvider:
    def get_current_project_dir_path(self) -> pathlib.Path:
        raise NotImplementedError

    def get_current_project_def_path(self) -> pathlib.Path:
        raise NotImplementedError

    async def get_current_project_package_name(self) -> str:
        raise NotImplementedError

    async def get_project_raw_config(
        self, project_def_path: pathlib.Path
    ) -> dict[str, typing.Any]:
        raise NotImplementedError

    async def get_current_project_raw_config(self) -> dict[str, typing.Any]:
        raise NotImplementedError

    def get_current_project_raw_config_version(self) -> int:
        raise NotImplementedError

    async def get_workspace_packages(self) -> dict[str, typing.Any]:
        raise NotImplementedError


async def _run_wrapper(
    tmp_path: pathlib.Path, runner: _ScriptedRunner
) -> PublishAndVerifyArtifactRunResult:
    payload = PublishAndVerifyArtifactRunPayload(
        src_artifact_def_path=path_to_resource_uri(tmp_path / "pyproject.toml"),
        dist_artifact_paths=[path_to_resource_uri(tmp_path / "pkg-1.0.0.tar.gz")],
        force=False,
    )
    result = await run_handler(
        PublishAndVerifyArtifactHandler,
        payload,
        action_cls=PublishAndVerifyArtifactAction,
        project_dir=tmp_path,
        service_overrides={
            iprojectactionrunner.IProjectActionRunner: runner,
            iprojectinfoprovider.IProjectInfoProvider: _StubProjectInfoProvider(),
        },
    )
    assert isinstance(result, PublishAndVerifyArtifactRunResult)
    return result


def test_wrapper_forwards_source_uri_and_publish_version(
    tmp_path: pathlib.Path,
) -> None:
    """Verification must reuse the explicit source definition and the publish result version so identity never drifts."""
    runner = _ScriptedRunner(version="2.0.0")
    result = run_on_virtual_time(_run_wrapper(tmp_path, runner))
    assert result.version == "2.0.0"
    assert len(runner.verify_payloads) == 1
    verify_payload = runner.verify_payloads[0]
    assert verify_payload.src_artifact_def_path == path_to_resource_uri(
        tmp_path / "pyproject.toml"
    )
    assert verify_payload.version == "2.0.0"
    assert verify_payload.registry_name == "local"
    assert verify_payload.dist_artifact_paths == [
        path_to_resource_uri(tmp_path / "pkg-1.0.0.tar.gz")
    ]


def test_returned_verify_errors_are_prefixed(tmp_path: pathlib.Path) -> None:
    """Returned verification errors keep their registry prefix so operators know an accepted upload was never confirmed."""
    runner = _ScriptedRunner(
        published=["a", "b"],
        verify_script={"a": ("ok", ["missing a.whl"]), "b": ("ok", [])},
    )
    result = run_on_virtual_time(_run_wrapper(tmp_path, runner))
    assert result.published_registries == ["a", "b"]
    assert result.verification_errors == {
        "a": ["a: Upload accepted but not confirmed: missing a.whl"]
    }
    assert result.publish_errors == {}


def test_raised_verify_error_becomes_prefixed_error(tmp_path: pathlib.Path) -> None:
    """A verification raise becomes a per-registry error so one registry failure never hides its sibling."""
    runner = _ScriptedRunner(
        published=["a", "b"],
        verify_script={"a": ("raise", RuntimeError("listing down")), "b": ("ok", [])},
    )
    result = run_on_virtual_time(_run_wrapper(tmp_path, runner))
    assert result.verification_errors == {
        "a": ["a: Upload accepted but not confirmed: listing down"]
    }


def test_pending_sibling_survives_failure(tmp_path: pathlib.Path) -> None:
    """A slow sibling still reports its real outcome after a fast sibling fails, proving concurrent checks."""
    runner = _ScriptedRunner(
        published=["fast", "slow"],
        verify_script={
            "fast": ("ok", ["fast missing"]),
            "slow": ("delay", 10.0, ["slow missing"]),
        },
    )
    result = run_on_virtual_time(_run_wrapper(tmp_path, runner))
    assert set(result.verification_errors) == {"fast", "slow"}


def test_failed_uploads_are_not_verified(tmp_path: pathlib.Path) -> None:
    """Registries whose upload failed are never verified and keep their publish errors unchanged."""
    runner = _ScriptedRunner(
        published=["good"],
        failed={"bad": "upload rejected"},
        verify_script={"good": ("ok", [])},
    )
    result = run_on_virtual_time(_run_wrapper(tmp_path, runner))
    assert result.published_registries == ["good"]
    assert result.publish_errors == {"bad": ["upload rejected"]}
    assert result.verification_errors == {}
    assert [p.registry_name for p in runner.verify_payloads] == ["good"]


def test_all_skipped_performs_no_verification(tmp_path: pathlib.Path) -> None:
    """No accepted upload means no verification dispatch at all."""
    runner = _ScriptedRunner(published=[], failed={})
    result = run_on_virtual_time(_run_wrapper(tmp_path, runner))
    assert result.published_registries == []
    assert result.verification_errors == {}
    assert runner.verify_payloads == []


@pytest.mark.parametrize(
    "exc",
    [asyncio.CancelledError(), iprojectactionrunner.ActionRunCancelled("stop")],
)
def test_verify_cancellation_propagates_and_cancels_siblings(
    tmp_path: pathlib.Path, exc: BaseException
) -> None:
    """Verification cancellation aborts the wrapper run and its sibling checks instead of becoming a registry error."""
    runner = _ScriptedRunner(
        published=["a", "b"],
        verify_script={"a": ("raise", exc), "b": ("hang", False, None)},
    )

    async def scenario() -> None:
        with pytest.raises(type(exc)):
            _ = await _run_wrapper(tmp_path, runner)
        _ = await runner.settled.wait()

    run_on_virtual_time(scenario())
    assert runner.cancelled == {"b"}


def test_caller_cancellation_does_not_wait_for_a_stuck_check(
    tmp_path: pathlib.Path,
) -> None:
    """A cancelled release re-raises at once even when a check ignores cancellation, and the check's late failure is never reported as unhandled."""
    runner = _ScriptedRunner(
        verify_script={
            "local": ("hang", True, iprojectactionrunner.ActionRunCancelled("late"))
        },
    )
    reports: list[dict[str, typing.Any]] = []

    async def scenario() -> tuple[bool, float]:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: reports.append(context))
        run = asyncio.create_task(_run_wrapper(tmp_path, runner))
        _ = await runner.started.wait()
        _ = run.cancel()
        done, _ = await asyncio.wait({run}, timeout=1.0)
        stopped_at = loop.time()
        cancelled = run in done and run.cancelled()
        # The run's traceback keeps the wrapper's futures alive; drop it so an
        # unretrieved late failure would be reported at collection.
        del run, done
        runner.release.set()
        _ = await runner.settled.wait()
        await asyncio.sleep(0)
        _ = gc.collect()
        return cancelled, stopped_at

    cancelled, stopped_at = run_on_virtual_time(scenario())
    assert cancelled
    assert stopped_at == 0.0
    assert runner.cancelled == {"local"}
    assert reports == []


class _Gate:
    def __init__(self, permits: int = 1) -> None:
        self.semaphore = asyncio.Semaphore(permits)
        self.current = 0
        self.max_seen = 0


class _GatedListingRunner:
    """Listing runner whose dispatches contend on a shared semaphore.

    Behaviors: ("ok", filenames) or ("hang", ignores_cancellation); a hang ends
    when `release` is set.
    """

    def __init__(
        self, gate: _Gate, script: list[tuple[str, typing.Any]], release: asyncio.Event
    ) -> None:
        self._gate = gate
        self._script = list(script)
        self._release = release

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
        del action_type, payload, meta, caller_kwargs
        kind, value = self._script.pop(0)
        async with self._gate.semaphore:
            self._gate.current += 1
            self._gate.max_seen = max(self._gate.max_seen, self._gate.current)
            try:
                if kind == "ok":
                    return ListPublishedArtifactsRunResult(filenames=list(value))
                assert kind == "hang"
                while True:
                    try:
                        _ = await self._release.wait()
                        return ListPublishedArtifactsRunResult(filenames=[])
                    except asyncio.CancelledError:
                        if not value:
                            raise
            finally:
                self._gate.current -= 1


def _with_real_verification(
    listing: _GatedListingRunner, registries: list[str]
) -> _ScriptedRunner:
    """A wrapper runner whose verify dispatch runs the real verification handler over `listing`."""
    provider = ConfigRepositoryCredentialsProvider(
        RepositoryCredentialsProviderConfig(
            repositories=[
                Repository(
                    name=name,
                    index_url=f"https://example.test/{name}/",
                    upload_url=f"https://example.test/{name}/",
                )
                for name in registries
            ]
        )
    )
    verify_handler = verify_module.VerifyArtifactPublishedToRegistryHandler(
        action_runner=listing,  # type: ignore[arg-type]
        repository_credentials_provider=provider,
    )

    async def verify(
        payload: typing.Any, meta: code_action.RunActionMeta
    ) -> VerifyArtifactPublishedToRegistryRunResult:
        context = typing.cast(typing.Any, types.SimpleNamespace(meta=meta))
        return await verify_handler.run(payload, context)

    return _ScriptedRunner(published=registries, verify=verify)


def test_two_active_checks_share_listing_resource(tmp_path: pathlib.Path) -> None:
    """Two concurrent verifications through one listing semaphore both keep their budgets and confirm."""
    gate = _Gate(permits=1)
    release = asyncio.Event()
    names = ["pkg-1.0.0.tar.gz"]
    listing = _GatedListingRunner(gate, [("ok", names), ("ok", names)], release)
    runner = _with_real_verification(listing, ["a", "b"])
    result = run_on_virtual_time(_run_wrapper(tmp_path, runner))
    assert result.verification_errors == {}
    assert gate.max_seen == 1
    assert not gate.semaphore.locked()


def test_abandoned_query_retains_resource_until_released(
    tmp_path: pathlib.Path,
) -> None:
    """An expired verification detaches its hung listing without acknowledgement, keeping the semaphore until the remote side finishes."""
    gate = _Gate(permits=1)
    release = asyncio.Event()
    listing = _GatedListingRunner(gate, [("hang", True)], release)
    runner = _with_real_verification(listing, ["a"])

    async def scenario() -> tuple[PublishAndVerifyArtifactRunResult, bool]:
        result = await _run_wrapper(tmp_path, runner)
        held_after_return = gate.semaphore.locked()
        release.set()
        async with gate.semaphore:
            pass
        return result, held_after_return

    result, held_after_return = run_on_virtual_time(scenario())
    assert list(result.verification_errors) == ["a"]
    assert "60 s deadline exhausted" in result.verification_errors["a"][0]
    assert held_after_return
