"""Tests for the file-version pin ``get_code_actions`` establishes for a run."""

from __future__ import annotations

import dataclasses
import pathlib

from finecode_extension_api import code_action
from finecode_extension_api.interfaces.ifileeditor import IFileEditor
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.testing import InMemoryFileEditor, handler_test_session

from fine_lint.get_code_actions_action import (
    GetCodeActionsAction,
    GetCodeActionsRunContext,
    GetCodeActionsRunPayload,
    GetCodeActionsRunResult,
)
from fine_lint.lint_fix import Position, Range

_WHOLE_FILE_RANGE = Range(
    start=Position(line=0, character=0), end=Position(line=0, character=0)
)


@dataclasses.dataclass
class _RecordSeenVersionHandlerConfig(code_action.ActionHandlerConfig): ...


class _RecordSeenVersionHandler(
    code_action.ActionHandler[GetCodeActionsAction, _RecordSeenVersionHandlerConfig]
):
    """Reports whatever file_version the run context pinned, and never looks at the
    payload's own (optional) staleness guard."""

    def __init__(self, config: _RecordSeenVersionHandlerConfig) -> None:
        self.config = config

    async def run(
        self,
        payload: GetCodeActionsRunPayload,
        run_context: GetCodeActionsRunContext,
    ) -> GetCodeActionsRunResult:
        return GetCodeActionsRunResult(
            file_version=run_context.file_version, actions=[]
        )


_ACTION_NAME = GetCodeActionsAction.__name__
_ACTION_SOURCE = (
    f"{GetCodeActionsAction.__module__}.{GetCodeActionsAction.__qualname__}"
)
_HANDLER_SOURCE = (
    f"{_RecordSeenVersionHandler.__module__}.{_RecordSeenVersionHandler.__qualname__}"
)
_ACTIONS = {
    _ACTION_NAME: {
        "source": _ACTION_SOURCE,
        "handlers": [{"name": "record_seen_version", "source": _HANDLER_SOURCE}],
    }
}


async def test_handler_sees_the_pinned_version_even_when_the_caller_sends_none(
    tmp_path: pathlib.Path,
) -> None:
    """A handler must be able to trust one version for the file for its whole run.

    ``get_code_actions`` runs its handlers concurrently, so without a version pinned
    up front each handler could read the file at a slightly different moment. A
    request whose caller supplies no version guard -- as the real LSP endpoint does
    today -- must still give every handler the same answer.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "import os\n")

    async with handler_test_session(
        project_dir=tmp_path,
        actions=_ACTIONS,
        service_overrides={IFileEditor: file_editor},
    ) as session:
        result = await session.run_action(
            _ACTION_NAME,
            GetCodeActionsRunPayload(
                file_path=path_to_resource_uri(file_path),
                range=_WHOLE_FILE_RANGE,
                diagnostics=[],
            ),
        )

    assert result is not None
    assert isinstance(result, GetCodeActionsRunResult)
    assert result.file_version != ""


async def test_merging_a_diverged_contribution_marks_the_result_unusable_as_a_batch() -> (
    None
):
    """A result assembled from actions computed against two different file contents
    must say so, rather than reporting one version label over an inconsistent set of
    edits.

    Applying such a result as one edit batch would silently mix edits that are not
    simultaneously valid against any single version of the file, corrupting
    whichever file that batch touches.
    """
    accumulated = GetCodeActionsRunResult(file_version="v1", actions=[])
    other = GetCodeActionsRunResult(file_version="v2", actions=[])

    accumulated.update(other)

    assert accumulated.version_diverged is True
    assert accumulated.file_version == "v1"


async def test_merging_a_matching_contribution_does_not_mark_divergence() -> None:
    """The common case -- every contribution agrees on the file's version -- must
    not be flagged, or every ordinary concurrent run would be reported as unsafe to
    apply."""
    accumulated = GetCodeActionsRunResult(file_version="v1", actions=[])
    other = GetCodeActionsRunResult(file_version="v1", actions=[])

    accumulated.update(other)

    assert accumulated.version_diverged is False
    assert accumulated.file_version == "v1"
