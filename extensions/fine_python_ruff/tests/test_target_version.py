"""Tests for how ruff's ``target-version`` is resolved."""

from __future__ import annotations

import collections.abc
import typing

from fine_python_lang import support_range
from fine_src_artifacts import get_src_artifact_toolchain_range_action
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import iprojectactionrunner

from fine_python_ruff.target_version import (
    resolve_target_version,
    to_ruff_target_version,
)

_META = code_action.RunActionMeta(
    trigger=code_action.RunActionTrigger.USER, dev_env=code_action.DevEnv.CLI
)


class _StubActionRunner:
    """Answers the range action with a fixed result, or raises what is given."""

    def __init__(
        self,
        result: get_src_artifact_toolchain_range_action.GetSrcArtifactToolchainRangeRunResult
        | None = None,
        error: Exception | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self.calls = 0

    async def get_actions_for_parent(
        self, parent_action_type: type
    ) -> dict[str, iprojectactionrunner.ActionRef]:
        raise NotImplementedError

    async def run_action(
        self,
        action_type: iprojectactionrunner.ActionRef,
        payload: typing.Any,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> typing.Any:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._result

    def run_action_iter(
        self,
        action_type: iprojectactionrunner.ActionRef,
        payload: typing.Any,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> collections.abc.AsyncIterator[typing.Any]:
        raise NotImplementedError


class _CollectingLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def exception(self, exception: Exception) -> None: ...
    def trace(self, message: str) -> None: ...
    def info(self, message: str) -> None: ...
    def debug(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...
    def disable(self, package: str) -> None: ...
    def enable(self, package: str) -> None: ...

    def warning(self, message: str) -> None:
        self.warnings.append(message)


def _resolver(
    *, min_version: str | None = None, error: Exception | None = None
) -> tuple[
    support_range.PythonSupportRangeResolver, _StubActionRunner, _CollectingLogger
]:
    result = (
        get_src_artifact_toolchain_range_action.GetSrcArtifactToolchainRangeRunResult(
            min_version=min_version
        )
    )
    action_runner = _StubActionRunner(result=result, error=error)
    logger = _CollectingLogger()
    return (
        support_range.PythonSupportRangeResolver(
            action_runner=typing.cast(
                iprojectactionrunner.IProjectActionRunner, action_runner
            ),
            logger=typing.cast(typing.Any, logger),
        ),
        action_runner,
        logger,
    )


def test_minor_series_becomes_ruffs_spelling() -> None:
    assert to_ruff_target_version("3.11") == "py311"


def test_series_ruff_cannot_name_has_no_spelling() -> None:
    # ruff rejects an unknown target-version outright rather than clamping, and the
    # value is passed on every invocation, so returning one here would turn every lint
    # and format run into a failure
    assert to_ruff_target_version("3.6") is None
    assert to_ruff_target_version("2.7") is None
    assert to_ruff_target_version("3.99") is None


async def test_declared_floor_is_what_ruff_targets() -> None:
    # the oldest supported version, not the newest: it is the level all the code has to
    # stay valid at, so it decides which upgrades ruff suggests
    resolver, _, logger = _resolver(min_version="3.11")

    assert await resolve_target_version(None, resolver, _META, logger) == "py311"


async def test_configured_value_wins_without_consulting_the_range() -> None:
    resolver, action_runner, logger = _resolver(min_version="3.11")

    assert await resolve_target_version("py312", resolver, _META, logger) == "py312"
    assert action_runner.calls == 0


async def test_no_declared_range_sends_nothing() -> None:
    # None is not a default: it means say nothing to ruff, which then infers the level
    # from requires-python itself rather than being overridden with a wrong one
    resolver, _, logger = _resolver(min_version=None)

    assert await resolve_target_version(None, resolver, _META, logger) is None


async def test_floor_ruff_cannot_express_is_not_sent_and_is_reported() -> None:
    # a project supporting 3.6 declares something ruff has no spelling for. Sending
    # py36 makes ruff exit with "invalid value", taking every lint run down with it
    resolver, _, logger = _resolver(min_version="3.6")

    assert await resolve_target_version(None, resolver, _META, logger) is None
    assert any("3.6" in warning for warning in logger.warnings)


async def test_failure_to_read_the_range_does_not_break_the_tool() -> None:
    resolver, _, logger = _resolver(
        error=iprojectactionrunner.ActionRunFailed("env unreachable")
    )

    assert await resolve_target_version(None, resolver, _META, logger) is None
    assert any("support range" in warning for warning in logger.warnings)


async def test_range_is_read_once_per_handler() -> None:
    resolver, action_runner, logger = _resolver(min_version="3.11")

    await resolve_target_version(None, resolver, _META, logger)
    await resolve_target_version(None, resolver, _META, logger)

    assert action_runner.calls == 1
