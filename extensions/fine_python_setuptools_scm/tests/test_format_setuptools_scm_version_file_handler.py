import asyncio
import pathlib
import tomllib
import types
import typing

import pytest
from fine_format import format_file_action
from fine_src_artifacts import get_src_artifact_version_action
from finecode_extension_api import code_action
from finecode_extension_api.code_action import CoverageStatus, ItemCoverage
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_api.resource_uri import (
    path_to_resource_uri,
    resource_uri_to_path,
)
from finecode_extension_runner import coverage_sink

from fine_python_setuptools_scm.format_setuptools_scm_version_file_handler import (
    FormatSetuptoolsScmVersionFileHandler,
)
from fine_python_setuptools_scm.get_src_artifact_version_setuptools_scm_handler import (
    GetSrcArtifactVersionSetuptoolsScmHandler,
    GetSrcArtifactVersionSetuptoolsScmHandlerConfig,
)

PYPROJECT_TEMPLATE = """\
[project]
name = "pkg"
dynamic = ["version"]

[tool.setuptools_scm]
version_file = "src/pkg/_version.py"
"""

PYPROJECT_NO_VERSION_FILE = """\
[project]
name = "pkg"
dynamic = ["version"]

[tool.setuptools_scm]
"""


class _FakeLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.debugs: list[str] = []

    def debug(self, message: str) -> None:
        self.debugs.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)


class _FakeProjectInfoProvider:
    def __init__(self, def_path: pathlib.Path) -> None:
        self._def_path = def_path

    def get_current_project_def_path(self) -> pathlib.Path:
        return self._def_path

    async def get_project_raw_config(
        self, project_def_path: pathlib.Path
    ) -> dict[str, typing.Any]:
        text = await asyncio.to_thread(project_def_path.read_text, encoding="utf-8")
        return tomllib.loads(text)


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
    async def get_content(self, file_path: pathlib.Path) -> str:
        return file_path.read_text(encoding="utf-8")


class _FakeSession:
    def __init__(self) -> None:
        self.saved: list[tuple[pathlib.Path, str]] = []

    async def save_file(
        self,
        file_path: pathlib.Path,
        file_content: str,
        if_version: str | None = None,
    ) -> None:
        file_path.write_text(file_content, encoding="utf-8")
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


def _write_fixture(
    tmp_path: pathlib.Path, template: str = PYPROJECT_TEMPLATE
) -> pathlib.Path:
    def_path = tmp_path / "pyproject.toml"
    def_path.write_text(template, encoding="utf-8")
    (tmp_path / "src" / "pkg").mkdir(parents=True, exist_ok=True)
    return def_path


def _target(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "src" / "pkg" / "_version.py"


async def _run_scm(tmp_path: pathlib.Path, monkeypatch: typing.Any) -> typing.Any:
    monkeypatch.setenv("SETUPTOOLS_SCM_PRETEND_VERSION", "1.2.3.dev4")
    def_path = _write_fixture(tmp_path)
    handler = GetSrcArtifactVersionSetuptoolsScmHandler(
        config=GetSrcArtifactVersionSetuptoolsScmHandlerConfig(),
        project_info_provider=typing.cast(
            typing.Any, _FakeProjectInfoProvider(def_path)
        ),
        logger=typing.cast(typing.Any, _FakeLogger()),
    )
    payload = get_src_artifact_version_action.GetSrcArtifactVersionRunPayload(
        src_artifact_def_path=path_to_resource_uri(def_path)
    )
    return await handler.run(payload, typing.cast(typing.Any, None))


def _new_run_context(current_result: typing.Any) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        meta=code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.USER,
            dev_env=code_action.DevEnv.CLI,
        ),
        current_result=current_result,
    )


def _payload_for(def_path: pathlib.Path | None, tmp_path: pathlib.Path):
    if def_path is None:
        return get_src_artifact_version_action.GetSrcArtifactVersionRunPayload(
            src_artifact_def_path=None
        )
    return get_src_artifact_version_action.GetSrcArtifactVersionRunPayload(
        src_artifact_def_path=path_to_resource_uri(def_path)
    )


def _make_format_handler(
    runner: _FakeActionRunner,
    session: _FakeSession,
    def_path: pathlib.Path,
    logger: _FakeLogger | None = None,
) -> FormatSetuptoolsScmVersionFileHandler:
    return FormatSetuptoolsScmVersionFileHandler(
        action_runner=typing.cast(iprojectactionrunner.IProjectActionRunner, runner),
        file_editor=typing.cast(typing.Any, _FakeFileEditor(session)),
        file_manager=typing.cast(typing.Any, _FakeFileManager()),
        project_info_provider=typing.cast(
            typing.Any, _FakeProjectInfoProvider(def_path)
        ),
        logger=typing.cast(typing.Any, logger or _FakeLogger()),
    )


async def test_formatted_content_is_written(
    tmp_path: pathlib.Path, monkeypatch: typing.Any
) -> None:
    scm_result = await _run_scm(tmp_path, monkeypatch)
    def_path = tmp_path / "pyproject.toml"
    disk_before = _target(tmp_path).read_text(encoding="utf-8")
    runner = _FakeActionRunner(
        result=format_file_action.FormatFileRunResult(
            changed=True, code='__version__ = version = "1.2.3.dev4"\n'
        )
    )
    session = _FakeSession()

    result = await _make_format_handler(runner, session, def_path).run(
        _payload_for(def_path, tmp_path), _new_run_context(scm_result)
    )

    assert result.version == scm_result.version
    assert session.saved == [
        (_target(tmp_path), '__version__ = version = "1.2.3.dev4"\n')
    ]
    assert _target(tmp_path).read_text(encoding="utf-8") == (
        '__version__ = version = "1.2.3.dev4"\n'
    )
    sent_payload = runner.captured_payload
    assert sent_payload is not None
    assert sent_payload.file_path == path_to_resource_uri(_target(tmp_path))
    assert sent_payload.save is False
    sent_kwargs = runner.captured_caller_kwargs
    assert isinstance(sent_kwargs, format_file_action.FormatFileCallerRunContextKwargs)
    assert sent_kwargs.file_editor_session is None
    assert sent_kwargs.file_info is not None
    assert sent_kwargs.file_info.file_content == disk_before


async def test_unchanged_content_triggers_no_write(
    tmp_path: pathlib.Path, monkeypatch: typing.Any
) -> None:
    scm_result = await _run_scm(tmp_path, monkeypatch)
    def_path = tmp_path / "pyproject.toml"
    runner = _FakeActionRunner(
        result=format_file_action.FormatFileRunResult(changed=False, code="")
    )
    session = _FakeSession()

    result = await _make_format_handler(runner, session, def_path).run(
        _payload_for(def_path, tmp_path), _new_run_context(scm_result)
    )

    assert result.version == scm_result.version
    assert session.saved == []


async def test_action_not_found_records_a_miss_itself(
    tmp_path: pathlib.Path, monkeypatch: typing.Any
) -> None:
    scm_result = await _run_scm(tmp_path, monkeypatch)
    def_path = tmp_path / "pyproject.toml"
    runner = _FakeActionRunner(
        error=iprojectactionrunner.ActionNotFound("no format_file")
    )
    logger = _FakeLogger()
    session = _FakeSession()
    disk_before = _target(tmp_path).read_text(encoding="utf-8")

    result = await _make_format_handler(runner, session, def_path, logger).run(
        _payload_for(def_path, tmp_path), _new_run_context(scm_result)
    )

    assert result.version == scm_result.version
    assert result.coverage == [
        ItemCoverage(
            status=CoverageStatus.NO_SUBACTIONS,
            item=path_to_resource_uri(_target(tmp_path)),
            detail="format_file",
        )
    ]
    assert _target(tmp_path).read_text(encoding="utf-8") == disk_before
    assert session.saved == []
    assert logger.warnings


async def test_dispatch_miss_is_never_absorbed(
    tmp_path: pathlib.Path, monkeypatch: typing.Any
) -> None:
    scm_result = await _run_scm(tmp_path, monkeypatch)
    def_path = tmp_path / "pyproject.toml"
    miss = format_file_action.FormatFileRunResult(
        changed=False,
        code="",
        coverage=[
            ItemCoverage(
                status=CoverageStatus.NO_SUBACTION_FOR_LANGUAGE,
                item=path_to_resource_uri(_target(tmp_path)),
                detail="python",
            )
        ],
    )
    disk_before = _target(tmp_path).read_text(encoding="utf-8")

    with coverage_sink.run():
        result = await _make_format_handler(
            _FakeActionRunner(result=miss), _FakeSession(), def_path
        ).run(_payload_for(def_path, tmp_path), _new_run_context(scm_result))
        sink = coverage_sink.current_sink()
        assert sink is not None
        entries = sink.entries

    assert result.version == scm_result.version
    assert _target(tmp_path).read_text(encoding="utf-8") == disk_before
    assert entries
    assert not any(e.status is CoverageStatus.ABSORBED for e in entries)


async def test_failing_formatter_fails_the_run(
    tmp_path: pathlib.Path, monkeypatch: typing.Any
) -> None:
    scm_result = await _run_scm(tmp_path, monkeypatch)
    def_path = tmp_path / "pyproject.toml"
    runner = _FakeActionRunner(error=iprojectactionrunner.ActionRunFailed("boom"))

    with pytest.raises(code_action.ActionFailedException) as exc_info:
        await _make_format_handler(runner, _FakeSession(), def_path).run(
            _payload_for(def_path, tmp_path), _new_run_context(scm_result)
        )

    assert "boom" in exc_info.value.message
    assert "get_src_artifact_version_setuptools_scm_format" in exc_info.value.message


async def test_cancelled_format_run_is_reraised(
    tmp_path: pathlib.Path, monkeypatch: typing.Any
) -> None:
    scm_result = await _run_scm(tmp_path, monkeypatch)
    def_path = tmp_path / "pyproject.toml"
    runner = _FakeActionRunner(
        error=iprojectactionrunner.ActionRunCancelled("cancelled")
    )

    with pytest.raises(iprojectactionrunner.ActionRunCancelled):
        await _make_format_handler(runner, _FakeSession(), def_path).run(
            _payload_for(def_path, tmp_path), _new_run_context(scm_result)
        )


async def test_missing_current_result_fails_without_dispatch(
    tmp_path: pathlib.Path,
) -> None:
    def_path = _write_fixture(tmp_path)
    runner = _FakeActionRunner(
        result=format_file_action.FormatFileRunResult(changed=False, code="")
    )

    with pytest.raises(code_action.ActionFailedException) as exc_info:
        await _make_format_handler(runner, _FakeSession(), def_path).run(
            _payload_for(def_path, tmp_path), _new_run_context(None)
        )

    assert "register it after a handler that determines the version" in (
        exc_info.value.message
    )
    assert runner.run_action_calls == 0


async def test_resource_uri_and_none_resolve_the_same_target(
    tmp_path: pathlib.Path, monkeypatch: typing.Any
) -> None:
    scm_result = await _run_scm(tmp_path, monkeypatch)
    def_path = tmp_path / "pyproject.toml"
    disk_content = _target(tmp_path).read_text(encoding="utf-8")

    seen: list[str] = []

    class _RecordingFileManager(_FakeFileManager):
        async def get_content(self, file_path: pathlib.Path) -> str:
            seen.append(file_path.read_text(encoding="utf-8"))
            return await super().get_content(file_path)

    for payload in (_payload_for(def_path, tmp_path), _payload_for(None, tmp_path)):
        runner = _FakeActionRunner(
            result=format_file_action.FormatFileRunResult(changed=False, code="")
        )
        handler = FormatSetuptoolsScmVersionFileHandler(
            action_runner=typing.cast(
                iprojectactionrunner.IProjectActionRunner, runner
            ),
            file_editor=typing.cast(typing.Any, _FakeFileEditor(_FakeSession())),
            file_manager=typing.cast(typing.Any, _RecordingFileManager()),
            project_info_provider=typing.cast(
                typing.Any, _FakeProjectInfoProvider(def_path)
            ),
            logger=typing.cast(typing.Any, _FakeLogger()),
        )
        result = await handler.run(payload, _new_run_context(scm_result))
        assert result.version == scm_result.version
        sent_payload = runner.captured_payload
        assert sent_payload is not None
        assert resource_uri_to_path(sent_payload.file_path) == _target(tmp_path)

    assert seen == [disk_content, disk_content]


async def test_config_without_version_file_makes_no_dispatch(
    tmp_path: pathlib.Path,
) -> None:
    def_path = _write_fixture(tmp_path, PYPROJECT_NO_VERSION_FILE)
    current = get_src_artifact_version_action.GetSrcArtifactVersionRunResult(
        version="1.2.3.dev4"
    )
    runner = _FakeActionRunner(
        result=format_file_action.FormatFileRunResult(changed=False, code="")
    )

    result = await _make_format_handler(runner, _FakeSession(), def_path).run(
        _payload_for(def_path, tmp_path), _new_run_context(current)
    )

    assert result.version == "1.2.3.dev4"
    assert runner.run_action_calls == 0
