import pathlib
import typing

import pytest
from fine_format import format_file_action
from finecode_extension_api import code_action
from finecode_extension_api.code_action import CoverageStatus, ItemCoverage
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_api.resource_uri import ResourceUri, path_to_resource_uri
from finecode_extension_runner import coverage_sink

from fine_envs import sync_toolchains_action
from fine_envs.sync_toolchains_format_handler import SyncToolchainsFormatHandler

META = code_action.RunActionMeta(
    trigger=code_action.RunActionTrigger.USER,
    dev_env=code_action.DevEnv.CLI,
)


class _FakeLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def debug(self, message: str) -> None: ...

    def warning(self, message: str) -> None:
        self.warnings.append(message)


class _FakeFormatRunner:
    def __init__(
        self,
        *,
        result: format_file_action.FormatFileRunResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self._result = result or format_file_action.FormatFileRunResult(
            changed=True, code="formatted"
        )
        self._error = error
        self.payloads: list[typing.Any] = []

    async def run_action(
        self,
        action_type: iprojectactionrunner.ActionRef,
        payload: typing.Any,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> typing.Any:
        self.payloads.append(payload)
        if self._error is not None:
            raise self._error
        # Mirrors the real runner's deposit at every run_action return point.
        coverage_sink.deposit_from(self._result)
        return self._result


def _def_uri(tmp_path: pathlib.Path) -> ResourceUri:
    return path_to_resource_uri(tmp_path / "pyproject.toml")


def _run_context(
    project_def_file: sync_toolchains_action.ProjectDefFileOutcome | None = None,
) -> sync_toolchains_action.SyncToolchainsRunContext:
    """A context whose accumulated result is what the dispatch handler returned."""
    attributes = {
        "meta": META,
        "current_result": sync_toolchains_action.SyncToolchainsRunResult(
            project_def_file=project_def_file
        ),
    }
    return typing.cast(
        sync_toolchains_action.SyncToolchainsRunContext,
        type("Ctx", (), attributes)(),
    )


def _updated(tmp_path: pathlib.Path) -> sync_toolchains_action.ProjectDefFileOutcome:
    return sync_toolchains_action.ProjectDefFileOutcome(
        path=_def_uri(tmp_path),
        status=sync_toolchains_action.ProjectDefFileStatus.UPDATED,
    )


def _format_handler(
    runner: _FakeFormatRunner, logger: _FakeLogger | None = None
) -> SyncToolchainsFormatHandler:
    return SyncToolchainsFormatHandler(
        action_runner=typing.cast(iprojectactionrunner.IProjectActionRunner, runner),
        logger=typing.cast(typing.Any, logger or _FakeLogger()),
    )


def test_format_step_contribution_keeps_the_file_outcome(
    tmp_path: pathlib.Path,
) -> None:
    """The format handler returns a result without a file outcome; merging it must
    not erase the outcome the language subaction reported."""
    accumulated = sync_toolchains_action.SyncToolchainsRunResult(
        project_def_file=_updated(tmp_path)
    )

    accumulated.update(sync_toolchains_action.SyncToolchainsRunResult())

    assert accumulated.project_def_file == _updated(tmp_path)


async def test_synced_file_is_formatted_and_saved(tmp_path: pathlib.Path) -> None:
    """A file the sync wrote is formatted in place, so `check_formatting` passes
    right after `sync_toolchains` (issue 968)."""
    runner = _FakeFormatRunner()

    result = await _format_handler(runner).run(
        sync_toolchains_action.SyncToolchainsRunPayload(),
        _run_context(_updated(tmp_path)),
    )

    assert len(runner.payloads) == 1
    sent = runner.payloads[0]
    assert isinstance(sent, format_file_action.FormatFileRunPayload)
    assert sent.file_path == _def_uri(tmp_path)
    assert sent.save is True
    assert result.unhandled == []
    assert result.axes == []
    assert result.project_def_file is None


@pytest.mark.parametrize(
    "status",
    [
        None,
        sync_toolchains_action.ProjectDefFileStatus.UP_TO_DATE,
        sync_toolchains_action.ProjectDefFileStatus.OUT_OF_DATE,
    ],
)
async def test_file_not_updated_is_not_formatted(
    tmp_path: pathlib.Path,
    status: sync_toolchains_action.ProjectDefFileStatus | None,
) -> None:
    """No file targeted, an up-to-date axis, or a dry run wrote nothing, so nothing
    is formatted: files the sync did not change are never rewritten."""
    runner = _FakeFormatRunner()
    project_def_file = (
        None
        if status is None
        else sync_toolchains_action.ProjectDefFileOutcome(
            path=_def_uri(tmp_path), status=status
        )
    )

    result = await _format_handler(runner).run(
        sync_toolchains_action.SyncToolchainsRunPayload(),
        _run_context(project_def_file),
    )

    assert runner.payloads == []
    assert result.coverage == []


async def test_missing_format_action_records_a_miss(tmp_path: pathlib.Path) -> None:
    runner = _FakeFormatRunner(
        error=iprojectactionrunner.ActionNotFound("no format_file")
    )
    logger = _FakeLogger()

    result = await _format_handler(runner, logger).run(
        sync_toolchains_action.SyncToolchainsRunPayload(),
        _run_context(_updated(tmp_path)),
    )

    assert result.unhandled == [
        ItemCoverage(
            status=CoverageStatus.NO_SUBACTIONS,
            item=_def_uri(tmp_path),
            detail="format_file",
        )
    ]
    assert logger.warnings


async def test_uncovered_file_is_warned_not_failed(tmp_path: pathlib.Path) -> None:
    runner = _FakeFormatRunner(
        result=format_file_action.FormatFileRunResult(
            changed=False,
            code="",
            coverage=[
                ItemCoverage(
                    status=CoverageStatus.NO_SUBACTION_FOR_LANGUAGE,
                    item=_def_uri(tmp_path),
                    detail="toml",
                )
            ],
        )
    )
    logger = _FakeLogger()

    result = await _format_handler(runner, logger).run(
        sync_toolchains_action.SyncToolchainsRunPayload(),
        _run_context(_updated(tmp_path)),
    )

    assert result.unhandled == []
    assert logger.warnings


async def test_failing_formatter_fails_the_run(tmp_path: pathlib.Path) -> None:
    runner = _FakeFormatRunner(
        error=iprojectactionrunner.ActionRunFailed("tombi crashed")
    )

    with pytest.raises(code_action.ActionFailedException) as exc_info:
        await _format_handler(runner).run(
            sync_toolchains_action.SyncToolchainsRunPayload(),
            _run_context(_updated(tmp_path)),
        )

    assert "tombi crashed" in exc_info.value.message
    assert "sync_toolchains_format" in exc_info.value.message


async def test_refused_save_fails_the_run(tmp_path: pathlib.Path) -> None:
    """The file changed after the formatter read it, so the formatted content was
    not saved. The sync asked for a save, so the run fails rather than leaving the
    file unformatted without saying so."""
    runner = _FakeFormatRunner(
        result=format_file_action.FormatFileRunResult(
            changed=True,
            code="formatted",
            status=format_file_action.FormatFileStatus.SAVE_REFUSED,
        )
    )

    with pytest.raises(code_action.ActionFailedException) as exc_info:
        await _format_handler(runner).run(
            sync_toolchains_action.SyncToolchainsRunPayload(),
            _run_context(_updated(tmp_path)),
        )

    assert "changed" in exc_info.value.message
