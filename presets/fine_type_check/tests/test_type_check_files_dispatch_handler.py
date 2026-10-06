"""type_check_files dispatch coverage: mirror of lint_files.

The type-check dispatch shares the lint dispatch's shape exactly; this pins
that it reports the same per-file misses for unmatched inputs, with the same
NO_SUBACTION_FOR_LANGUAGE / NO_LANGUAGE_DETECTED attribution.
"""

from __future__ import annotations

import pathlib
import typing

from fine_inspect_code.diagnostic_types import (
    DiagnosticFilesRunPayload,
    DiagnosticFilesRunResult,
)
from fine_src_artifacts import group_src_artifact_files_by_lang_action
from finecode_extension_api import code_action
from finecode_extension_api.code_action import CoverageStatus, ItemCoverage
from finecode_extension_api.interfaces import iprojectactionrunner
from finecode_extension_api.resource_uri import path_to_resource_uri

from fine_type_check.type_check_files_dispatch_handler import (
    TypeCheckFilesDispatchHandler,
)

_PY_URI = path_to_resource_uri(pathlib.Path("/tmp/subject.py"))
_TOML_URI = path_to_resource_uri(pathlib.Path("/tmp/config.toml"))


class _FakeLogger:
    def debug(self, message: str) -> None: ...


class _AccumulatingSender:
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
        subaction_partials: list[DiagnosticFilesRunResult] | None = None,
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
    subaction_partials: list[DiagnosticFilesRunResult] | None = None,
) -> DiagnosticFilesRunResult | None:
    handler = TypeCheckFilesDispatchHandler(
        action_runner=typing.cast(
            iprojectactionrunner.IProjectActionRunner,
            _FakeActionRunner(subactions_by_lang, files_by_lang, subaction_partials),
        ),
        logger=typing.cast(typing.Any, _FakeLogger()),
    )
    sender = _AccumulatingSender()
    await handler.run(
        payload=DiagnosticFilesRunPayload(file_paths=[_PY_URI, _TOML_URI]),
        run_context=typing.cast(typing.Any, _RunContextStub(sender)),
    )
    return typing.cast(DiagnosticFilesRunResult | None, sender.accumulated)


async def test_mixed_batch_reports_the_uncovered_file() -> None:
    """Same mixed-batch contract as lint_files: both URIs in ``messages``, and
    ``unhandled`` names exactly the ``.toml`` file with the language as
    detail."""
    subactions = {"python": typing.cast(typing.Any, object())}
    result = await _run(
        subactions_by_lang=subactions,
        files_by_lang={"python": [_PY_URI], "toml": [_TOML_URI]},
        subaction_partials=[DiagnosticFilesRunResult(messages={_PY_URI: []})],
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


async def test_no_subactions_reports_every_file() -> None:
    result = await _run(subactions_by_lang={}, files_by_lang={})
    assert result is not None
    assert set(result.messages) == {_PY_URI, _TOML_URI}
    assert result.unhandled == [
        ItemCoverage(status=CoverageStatus.NO_SUBACTIONS, item=uri)
        for uri in (_PY_URI, _TOML_URI)
    ]
