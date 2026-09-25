"""format_file dispatch coverage: "no handler covered this input" is an answer.

The dispatch handler is where a caller learns whether any formatter covered the
requested file. Without coverage, both unhandled paths return a
``changed=False, code=...`` value identical to a file that a formatter ran on
and left untouched — an invisible misconfiguration.
"""

from __future__ import annotations

import pathlib
import typing

from fine_src_artifacts import group_src_artifact_files_by_lang_action
from finecode_extension_api import code_action
from finecode_extension_api.code_action import CoverageStatus, ItemCoverage
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_api.resource_uri import path_to_resource_uri

from fine_format import format_file_action
from fine_format.format_file_dispatch_handler import FormatFileDispatchHandler

_FILE_URI = path_to_resource_uri(pathlib.Path("/tmp/subject.py"))


class _FakeLogger:
    def debug(self, message: str) -> None: ...


class _RunContextStub:
    def __init__(self, content: str) -> None:
        self.meta = code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.USER, dev_env=code_action.DevEnv.CLI
        )
        self.file_info = format_file_action.FileInfo(
            file_content=content, file_version="v"
        )
        self.file_editor_session = None


class _FakeActionRunner:
    def __init__(
        self,
        subactions_by_lang: dict[str, iprojectactionrunner.ActionRef],
        files_by_lang: dict[str, list],
        subaction_result: format_file_action.FormatFileRunResult | None = None,
    ) -> None:
        self._subactions = subactions_by_lang
        self._files_by_lang = files_by_lang
        self._subaction_result = subaction_result

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
        if (
            getattr(action_type, "action_type", None)
            is group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangAction
        ):
            return group_src_artifact_files_by_lang_action.GroupSrcArtifactFilesByLangRunResult(
                files_by_lang=self._files_by_lang
            )
        return self._subaction_result


def _run(
    subactions_by_lang: dict[str, iprojectactionrunner.ActionRef],
    files_by_lang: dict[str, list],
    subaction_result: format_file_action.FormatFileRunResult | None = None,
) -> format_file_action.FormatFileRunResult:
    handler = FormatFileDispatchHandler(
        action_runner=typing.cast(
            iprojectactionrunner.IProjectActionRunner,
            _FakeActionRunner(subactions_by_lang, files_by_lang, subaction_result),
        ),
        logger=typing.cast(typing.Any, _FakeLogger()),
    )
    return handler.run(
        payload=format_file_action.FormatFileRunPayload(
            file_path=_FILE_URI, save=False
        ),
        run_context=typing.cast(typing.Any, _RunContextStub(content="unchanged")),
    )


async def test_no_subactions_reports_a_miss_per_file() -> None:
    """No formatter registered at all must be an explicit NO_SUBACTIONS miss —
    not a silent "unchanged"."""
    result = await _run(subactions_by_lang={}, files_by_lang={})
    assert result.changed is False
    assert result.unhandled == [
        ItemCoverage(status=CoverageStatus.NO_SUBACTIONS, item=_FILE_URI)
    ]


async def test_no_language_match_reports_a_miss() -> None:
    """A file no registered formatter's language covers is a
    NO_LANGUAGE_DETECTED miss."""
    result = await _run(
        subactions_by_lang={"python": typing.cast(typing.Any, object())},
        files_by_lang={"python": []},
    )
    assert result.changed is False
    assert result.unhandled == [
        ItemCoverage(status=CoverageStatus.NO_LANGUAGE_DETECTED, item=_FILE_URI)
    ]


async def test_unregistered_language_bucket_reports_no_subaction_for_language() -> None:
    """A file the grouping put in a bucket whose language has no registered
    subaction is NO_SUBACTION_FOR_LANGUAGE — the bucket name is the diagnosis,
    not a dispatcher crash or a silent "unchanged"."""
    result = await _run(
        subactions_by_lang={"python": typing.cast(typing.Any, object())},
        files_by_lang={"python": [], "toml": [_FILE_URI]},
    )
    assert result.changed is False
    assert result.unhandled == [
        ItemCoverage(
            status=CoverageStatus.NO_SUBACTION_FOR_LANGUAGE,
            item=_FILE_URI,
            detail="toml",
        )
    ]


async def test_successful_dispatch_is_clean() -> None:
    """A dispatched-and-formatted file is the legitimate empty answer: no
    coverage entries at all — nothing emits HANDLED."""
    result = await _run(
        subactions_by_lang={"python": typing.cast(typing.Any, object())},
        files_by_lang={"python": [_FILE_URI]},
        subaction_result=format_file_action.FormatFileRunResult(
            changed=True, code="formatted"
        ),
    )
    assert result.changed is True
    assert result.unhandled == []
    assert result.coverage == []
