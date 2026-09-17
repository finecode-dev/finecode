"""lint_files dispatch coverage: an unmatched file is a miss, not an "OK".

The dispatch sends explicit empty ``messages`` for unmatched files so the IDE
can clear stale diagnostics (R-307); this module pins that the same send also
carries the coverage answer, so "this file was never handed to a linter"
stops looking identical to "this file linted clean".
"""

from __future__ import annotations

import pathlib
import typing

from fine_inspect_code.diagnostic_types import (
    DiagnosticFilesRunResult as LintFilesRunResult,
)
from fine_src_artifacts import group_src_artifact_files_by_lang_action
from finecode_extension_api import code_action
from finecode_extension_api.code_action import CoverageStatus, ItemCoverage
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_api.resource_uri import path_to_resource_uri

from fine_lint.lint_files_action import LintFilesRunPayload
from fine_lint.lint_files_dispatch_handler import LintFilesDispatchHandler

_PY_URI = path_to_resource_uri(pathlib.Path("/tmp/subject.py"))
_TOML_URI = path_to_resource_uri(pathlib.Path("/tmp/config.toml"))


class _FakeLogger:
    def debug(self, message: str) -> None: ...


class _AccumulatingSender:
    """Mirrors the runner's accumulator: first result kept by reference, later
    sends merged into it via ``update()``."""

    def __init__(self) -> None:
        self.accumulated: code_action.RunActionResult | None = None

    async def send(self, result: code_action.RunActionResult) -> None:
        if self.accumulated is None:
            self.accumulated = result
        else:
            self.accumulated.update(result)


class _RunContextStub:
    def __init__(self, sender: _AccumulatingSender) -> None:
        self.meta = code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.USER, dev_env=code_action.DevEnv.CLI
        )
        self.partial_result_sender = sender  # type: ignore[assignment]


class _FakeActionRunner:
    def __init__(
        self,
        subactions_by_lang: dict[str, iprojectactionrunner.ActionRef],
        files_by_lang: dict[str, list],
        subaction_partials: list[LintFilesRunResult] | None = None,
    ) -> None:
        self._subactions = subactions_by_lang
        self._files_by_lang = files_by_lang
        self._subaction_partials = subaction_partials or []

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

    async def run_action_iter(
        self,
        action_type: iprojectactionrunner.ActionRef,
        payload: typing.Any,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> typing.Any:
        for partial in self._subaction_partials:
            yield partial


async def _run(
    subactions_by_lang: dict[str, iprojectactionrunner.ActionRef],
    files_by_lang: dict[str, list],
    subaction_partials: list[LintFilesRunResult] | None = None,
) -> LintFilesRunResult | None:
    handler = LintFilesDispatchHandler(
        action_runner=typing.cast(
            iprojectactionrunner.IProjectActionRunner,
            _FakeActionRunner(subactions_by_lang, files_by_lang, subaction_partials),
        ),
        logger=typing.cast(typing.Any, _FakeLogger()),
    )
    sender = _AccumulatingSender()
    await handler.run(
        payload=LintFilesRunPayload(file_paths=[_PY_URI, _TOML_URI]),
        run_context=typing.cast(typing.Any, _RunContextStub(sender)),
    )
    return typing.cast(LintFilesRunResult | None, sender.accumulated)


async def test_mixed_batch_keeps_both_uris_and_reports_the_uncovered_one() -> None:
    """A mixed batch must still carry both URIs in ``messages`` (R-307
    unchanged) while ``unhandled`` names exactly the ``.toml`` file with
    NO_SUBACTION_FOR_LANGUAGE and ``detail == \"toml\"``."""
    subactions = {"python": typing.cast(typing.Any, object())}
    result = await _run(
        subactions_by_lang=subactions,
        files_by_lang={"python": [_PY_URI], "toml": [_TOML_URI]},
        subaction_partials=[LintFilesRunResult(messages={_PY_URI: []})],
    )
    assert result is not None
    assert set(result.messages) == {_PY_URI, _TOML_URI}
    assert result.unhandled == [
        ItemCoverage(
            status=CoverageStatus.NO_SUBACTION_FOR_LANGUAGE,
            item=_TOML_URI,
            detail="toml",
        )
    ]


async def test_file_in_no_bucket_is_a_language_detection_miss() -> None:
    """A file that no grouping handler buckets at all is NO_LANGUAGE_DETECTED,
    not NO_SUBACTION_FOR_LANGUAGE — the two diagnoses tell a caller different
    things (language not recognised vs recognised but unserved)."""
    result = await _run(
        subactions_by_lang={"python": typing.cast(typing.Any, object())},
        files_by_lang={"python": []},
    )
    assert result is not None
    assert set(result.messages) == {_PY_URI, _TOML_URI}
    assert result.unhandled == [
        ItemCoverage(status=CoverageStatus.NO_LANGUAGE_DETECTED, item=uri)
        for uri in (_PY_URI, _TOML_URI)
    ]


async def test_no_subactions_reports_every_file() -> None:
    """With no lint subactions registered at all, every file is a
    NO_SUBACTIONS miss — the empty-messages send is preserved, coverage added
    to it, never replacing it."""
    result = await _run(subactions_by_lang={}, files_by_lang={})
    assert result is not None
    assert set(result.messages) == {_PY_URI, _TOML_URI}
    assert result.unhandled == [
        ItemCoverage(status=CoverageStatus.NO_SUBACTIONS, item=uri)
        for uri in (_PY_URI, _TOML_URI)
    ]