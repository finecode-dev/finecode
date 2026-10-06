import pathlib
import types
import typing

import pytest
from fine_format import format_file_action
from finecode_extension_api import code_action
from finecode_extension_api.code_action import CoverageStatus, ItemCoverage
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_api.resource_uri import (
    path_to_resource_uri,
    resource_uri_to_path,
)
from finecode_extension_runner import coverage_sink

from fine_envs import dump_config_action
from fine_envs.dump_config_format_handler import DumpConfigFormatHandler
from fine_envs.dump_config_handler import DumpConfigHandler
from fine_envs.dump_config_render import render_config_dump
from fine_envs.dump_config_save_handler import DumpConfigSaveHandler

RAW_CONFIG = {
    "dependency-groups": {"runtime": ["finecode_dev_common_preset[lint_fix]~=0.3.0a0"]}
}
SELECTION = {"finecode-dev-common-preset": ["lint_fix"]}
RENDERED = "rendered dump"


class _FakeLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.debugs: list[str] = []

    def debug(self, message: str) -> None:
        self.debugs.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)


class _FakeProjectInfoProvider:
    def __init__(self, selection: dict[str, list[str]]) -> None:
        self._selection = selection

    async def get_workspace_extra_selection(self) -> dict[str, list[str]]:
        return self._selection


class _FakeActionRunner:
    def __init__(
        self,
        *,
        result: format_file_action.FormatFileRunResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self.run_action_calls = 0
        self.captured_payload: format_file_action.FormatFileRunPayload | None = None
        self.captured_caller_kwargs: code_action.CallerRunContextKwargs | None = None

    async def run_action(
        self,
        action_type: iprojectactionrunner.ActionRef,
        payload: typing.Any,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> typing.Any:
        self.run_action_calls += 1
        self.captured_payload = payload
        self.captured_caller_kwargs = caller_kwargs
        if self._error is not None:
            raise self._error
        if self._result is not None:
            # Mirrors the real runner's deposit at every run_action return point.
            coverage_sink.deposit_from(self._result)
        return self._result


class _FakeFileManager:
    async def create_dir(self, dir_path: pathlib.Path) -> None: ...


class _FakeSession:
    def __init__(self) -> None:
        self.saved: list[tuple[pathlib.Path, str]] = []

    async def save_file(self, file_path: pathlib.Path, file_content: str) -> None:
        self.saved.append((file_path, file_content))


class _FakeFileEditor:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def session(self, author):
        return _FakeSessionCM(self._session)


class _FakeSessionCM:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


def _target_uri(tmp_path: pathlib.Path):
    return path_to_resource_uri(tmp_path / "finecode_config_dump" / "pyproject.toml")


def _payload(tmp_path: pathlib.Path) -> dump_config_action.DumpConfigRunPayload:
    return dump_config_action.DumpConfigRunPayload(
        source_file_path=path_to_resource_uri(tmp_path / "pyproject.toml"),
        project_raw_config=RAW_CONFIG,
        target_file_path=_target_uri(tmp_path),
    )


def _new_run_context(content: str | None = RENDERED) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        raw_config_dump=RAW_CONFIG,
        config_dump_content=content,
        meta=code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.USER,
            dev_env=code_action.DevEnv.CLI,
        ),
    )


def _make_handler(
    action_runner: _FakeActionRunner,
    logger: _FakeLogger | None = None,
) -> DumpConfigFormatHandler:
    return DumpConfigFormatHandler(
        action_runner=typing.cast(
            iprojectactionrunner.IProjectActionRunner, action_runner
        ),
        logger=typing.cast(typing.Any, logger or _FakeLogger()),
    )


def _miss(tmp_path: pathlib.Path) -> format_file_action.FormatFileRunResult:
    return format_file_action.FormatFileRunResult(
        changed=False,
        code="",
        coverage=[
            ItemCoverage(
                status=CoverageStatus.NO_SUBACTION_FOR_LANGUAGE,
                item=_target_uri(tmp_path),
                detail="toml",
            )
        ],
    )


async def test_formatted_content_replaces_rendered_content(
    tmp_path: pathlib.Path,
) -> None:
    """A formatter that changed the content: the formatted code replaces the
    rendered content on the run context, and nothing is reported unhandled.
    The formatter receives the rendered content in memory for the target file."""
    runner = _FakeActionRunner(
        result=format_file_action.FormatFileRunResult(
            changed=True, code="formatted dump"
        )
    )
    context = _new_run_context()

    result = await _make_handler(runner).run(_payload(tmp_path), context)

    assert context.config_dump_content == "formatted dump"
    assert result.unhandled == []
    sent_payload = runner.captured_payload
    assert sent_payload is not None
    assert sent_payload.file_path == _target_uri(tmp_path)
    assert sent_payload.save is False
    sent_kwargs = runner.captured_caller_kwargs
    assert isinstance(sent_kwargs, format_file_action.FormatFileCallerRunContextKwargs)
    assert sent_kwargs.file_editor_session is None
    assert sent_kwargs.file_info is not None
    assert sent_kwargs.file_info.file_content == RENDERED


async def test_unchanged_formatter_keeps_rendered_content(
    tmp_path: pathlib.Path,
) -> None:
    """``changed=False`` with no coverage means a formatter ran and the content
    was already fine: the rendered bytes stay, the empty result code is never
    read, and the handler result carries no miss."""
    runner = _FakeActionRunner(
        result=format_file_action.FormatFileRunResult(changed=False, code="")
    )
    context = _new_run_context()

    result = await _make_handler(runner).run(_payload(tmp_path), context)

    assert context.config_dump_content == RENDERED
    assert result.unhandled == []


async def test_formatter_miss_keeps_rendered_content_and_logs(
    tmp_path: pathlib.Path,
) -> None:
    """A miss on the format result means no formatter covered the dump: the
    rendered (unformatted) content is kept — never ``result.code`` — the
    outcome is logged at WARNING, and the miss is not copied onto this result:
    it travels through the run's sink instead."""
    runner = _FakeActionRunner(result=_miss(tmp_path))
    logger = _FakeLogger()
    context = _new_run_context()

    result = await _make_handler(runner, logger=logger).run(_payload(tmp_path), context)

    assert context.config_dump_content == RENDERED
    assert result.unhandled == []
    assert logger.warnings


async def test_action_not_found_records_a_miss_itself(
    tmp_path: pathlib.Path,
) -> None:
    """With no format_file action registered, the dump has no formatter — the
    same answer as "no subactions registered". The handler records the miss
    itself because no dispatcher ran that could record it."""
    runner = _FakeActionRunner(
        error=iprojectactionrunner.ActionNotFound("no format_file")
    )
    logger = _FakeLogger()
    context = _new_run_context()

    result = await _make_handler(runner, logger=logger).run(_payload(tmp_path), context)

    assert context.config_dump_content == RENDERED
    assert result.unhandled == [
        ItemCoverage(
            status=CoverageStatus.NO_SUBACTIONS,
            item=_target_uri(tmp_path),
            detail="format_file",
        )
    ]
    assert logger.warnings


async def test_failing_formatter_fails_the_dump(tmp_path: pathlib.Path) -> None:
    """A formatter that exists but fails is a broken setup, not a missing one:
    the dump fails, and the message names the two ways to still write an
    unformatted dump: ``format_output=false`` on the payload and disabling the
    handler."""
    runner = _FakeActionRunner(
        error=iprojectactionrunner.ActionRunFailed("tombi crashed")
    )

    with pytest.raises(code_action.ActionFailedException) as exc_info:
        await _make_handler(runner).run(_payload(tmp_path), _new_run_context())

    assert "tombi crashed" in exc_info.value.message
    assert "format_output=false" in exc_info.value.message
    assert "dump_config_format" in exc_info.value.message


async def test_format_output_false_skips_formatting(
    tmp_path: pathlib.Path,
) -> None:
    """``format_output=False`` means the dump is machine input, not something
    a person reads: no ``format_file`` dispatch happens, the rendered content
    is saved unchanged, the result carries no coverage, and nothing is logged
    above DEBUG. A caller that asked for no formatting has no unhandled input
    to be told about."""
    runner = _FakeActionRunner(
        result=format_file_action.FormatFileRunResult(
            changed=True, code="formatted dump"
        )
    )
    logger = _FakeLogger()
    context = _new_run_context()
    payload = _payload(tmp_path)
    payload.format_output = False

    result = await _make_handler(runner, logger=logger).run(payload, context)

    assert runner.run_action_calls == 0
    assert context.config_dump_content == RENDERED
    assert result.coverage == []
    assert result.config_dump == RAW_CONFIG
    assert logger.warnings == []
    assert logger.debugs


async def test_cancelled_format_run_is_reraised(tmp_path: pathlib.Path) -> None:
    """``ActionRunCancelled`` is not a fallback: a cancelled outer run must not
    silently produce a "successful" unformatted dump."""
    runner = _FakeActionRunner(
        error=iprojectactionrunner.ActionRunCancelled("cancelled")
    )

    with pytest.raises(iprojectactionrunner.ActionRunCancelled):
        await _make_handler(runner).run(_payload(tmp_path), _new_run_context())


async def test_without_rendered_content_fails(tmp_path: pathlib.Path) -> None:
    runner = _FakeActionRunner(
        result=format_file_action.FormatFileRunResult(changed=False, code="")
    )

    with pytest.raises(code_action.ActionFailedException):
        await _make_handler(runner).run(
            _payload(tmp_path), _new_run_context(content=None)
        )

    assert runner.captured_payload is None


async def test_pipeline_renders_formats_then_saves_once(
    tmp_path: pathlib.Path,
) -> None:
    """The three handlers in preset order: the rendered dump (attribution
    included) is what the formatter receives, and the formatted content is
    written exactly once."""
    runner = _FakeActionRunner(
        result=format_file_action.FormatFileRunResult(
            changed=True, code="formatted dump"
        )
    )
    context = _new_run_context(content=None)
    payload = _payload(tmp_path)
    session = _FakeSession()

    await DumpConfigHandler(
        project_info_provider=typing.cast(
            typing.Any, _FakeProjectInfoProvider(SELECTION)
        )
    ).run(payload, context)
    await _make_handler(runner).run(payload, context)
    await DumpConfigSaveHandler(
        file_manager=_FakeFileManager(), file_editor=_FakeFileEditor(session)
    ).run(payload, context)

    sent_kwargs = runner.captured_caller_kwargs
    assert isinstance(sent_kwargs, format_file_action.FormatFileCallerRunContextKwargs)
    assert sent_kwargs.file_info is not None
    assert sent_kwargs.file_info.file_content == render_config_dump(
        RAW_CONFIG, SELECTION
    )
    assert session.saved == [
        (resource_uri_to_path(_target_uri(tmp_path)), "formatted dump")
    ]


async def test_handler_never_absorbs_the_miss(tmp_path: pathlib.Path) -> None:
    """Writing an unformatted dump does not handle the degradation, so the
    handler must never call ``absorb_coverage``: the miss must ride the run's
    sink to the dump_config result untouched, where the caller can see it."""
    with coverage_sink.run():
        await _make_handler(_FakeActionRunner(result=_miss(tmp_path))).run(
            _payload(tmp_path), _new_run_context()
        )
        sink = coverage_sink.current_sink()
        assert sink is not None
        entries = sink.entries

    assert entries
    assert not any(e.status is CoverageStatus.ABSORBED for e in entries)
