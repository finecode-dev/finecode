"""get_lint_fixes dispatch coverage: an uncovered file answers "no fixes",
not "fixes were computed and there are none".

``fixes == []`` is also the legitimate "ran and found nothing" answer; the
coverage entry is what tells a caller which one it is.
"""

from __future__ import annotations

import pathlib
import typing

from fine_src_artifacts import group_src_artifact_files_by_lang_action
from finecode_extension_api import code_action
from finecode_extension_api.code_action import CoverageStatus, ItemCoverage
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_api.resource_uri import path_to_resource_uri

from fine_lint.get_lint_fixes_action import (
    GetLintFixesRunPayload,
    GetLintFixesRunResult,
)
from fine_lint.get_lint_fixes_files_dispatch_handler import (
    GetLintFixesFilesDispatchHandler,
)

_FILE_URI = path_to_resource_uri(pathlib.Path("/tmp/subject.toml"))


class _FakeLogger:
    def debug(self, message: str) -> None: ...


class _CollectingSender:
    def __init__(self) -> None:
        self.results: list[code_action.RunActionResult] = []

    async def send(self, result: code_action.RunActionResult) -> None:
        self.results.append(result)


class _RunContextStub:
    def __init__(self, sender: _CollectingSender) -> None:
        self.meta = code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.USER, dev_env=code_action.DevEnv.CLI
        )
        self.partial_result_sender = sender  # type: ignore[assignment]


class _FakeActionRunner:
    def __init__(
        self,
        subactions_by_lang: dict[str, iprojectactionrunner.ActionRef],
        files_by_lang: dict[str, list],
    ) -> None:
        self._subactions = subactions_by_lang
        self._files_by_lang = files_by_lang

    async def get_actions_for_parent(
        self, parent_action_type: type
    ) -> dict[str, iprojectactionrunner.ActionRef]:
        return self._subactions

    async def run_action(
        self,
        action_type: iprojectactionrunner.ActionRef,
        payload: typing.Any,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> typing.Any:
        return group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangRunResult(
            files_by_lang=self._files_by_lang
        )


async def _run(
    subactions_by_lang: dict[str, iprojectactionrunner.ActionRef],
    files_by_lang: dict[str, list],
) -> list[GetLintFixesRunResult]:
    handler = GetLintFixesFilesDispatchHandler(
        action_runner=typing.cast(
            iprojectactionrunner.IProjectActionRunner,
            _FakeActionRunner(subactions_by_lang, files_by_lang),
        ),
        logger=typing.cast(typing.Any, _FakeLogger()),
    )
    sender = _CollectingSender()
    await handler.run(
        payload=GetLintFixesRunPayload(file_path=_FILE_URI),
        run_context=typing.cast(typing.Any, _RunContextStub(sender)),
    )
    return typing.cast(list[GetLintFixesRunResult], sender.results)


async def test_no_subaction_reports_empty_fixes_and_names_the_file() -> None:
    """No subaction for the file: ``fixes == []`` *and* ``unhandled`` names the
    file — checking only one of the two would miss the real defect."""
    results = await _run(
        subactions_by_lang={"python": typing.cast(typing.Any, object())},
        files_by_lang={"toml": [_FILE_URI]},
    )
    assert len(results) == 1
    result = results[0]
    assert result.fixes == []
    assert result.unhandled == [
        ItemCoverage(
            status=CoverageStatus.NO_SUBACTION_FOR_LANGUAGE,
            item=_FILE_URI,
            detail="toml",
        )
    ]


async def test_no_subactions_at_all_reports_a_no_subactions_miss() -> None:
    results = await _run(subactions_by_lang={}, files_by_lang={})
    assert len(results) == 1
    assert results[0].fixes == []
    assert results[0].unhandled == [
        ItemCoverage(status=CoverageStatus.NO_SUBACTIONS, item=_FILE_URI)
    ]