"""Tests for the file-version pin ``get_lint_fixes`` establishes for a run."""

from __future__ import annotations

import dataclasses
import pathlib

from finecode_extension_api import code_action
from finecode_extension_api.interfaces.ifileeditor import IFileEditor
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.testing import InMemoryFileEditor, handler_test_session

from fine_lint.get_lint_fixes_action import (
    GetLintFixesAction,
    GetLintFixesRunContext,
    GetLintFixesRunPayload,
    GetLintFixesRunResult,
)


@dataclasses.dataclass
class _RecordSeenVersionHandlerConfig(code_action.ActionHandlerConfig): ...


class _RecordSeenVersionHandler(
    code_action.ActionHandler[GetLintFixesAction, _RecordSeenVersionHandlerConfig]
):
    """Reports whatever file_version the run context pinned, and never looks at the
    payload's own (optional) staleness guard."""

    def __init__(self, config: _RecordSeenVersionHandlerConfig) -> None:
        self.config = config

    async def run(
        self,
        payload: GetLintFixesRunPayload,
        run_context: GetLintFixesRunContext,
    ) -> GetLintFixesRunResult:
        return GetLintFixesRunResult(file_version=run_context.file_version, fixes=[])


_ACTION_NAME = GetLintFixesAction.__name__
_ACTION_SOURCE = f"{GetLintFixesAction.__module__}.{GetLintFixesAction.__qualname__}"
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

    Before this pin existed, a handler had no reliable version to compute fixes
    against unless the caller happened to pass one in the payload -- and the LSP
    entry point never does. Concurrent handlers each reading the file separately
    could then observe different content for what the caller intended as one
    request, and a result assembled from them would silently mix fixes computed
    against two different files.
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
            # No file_version on the payload -- the caller supplies no guard, as
            # the real LSP endpoint does today.
            GetLintFixesRunPayload(file_path=path_to_resource_uri(file_path)),
        )

    assert result is not None
    assert isinstance(result, GetLintFixesRunResult)
    assert result.file_version != ""


async def test_merging_a_diverged_contribution_marks_the_result_unusable_as_a_batch() -> (
    None
):
    """A result assembled from fixes computed against two different file contents
    must say so, rather than reporting one version label over an inconsistent set of
    edits.

    Applying such a result as one edit batch would silently mix edits that are not
    simultaneously valid against any single version of the file, corrupting
    whichever file that batch touches.
    """
    accumulated = GetLintFixesRunResult(file_version="v1", fixes=[])
    other = GetLintFixesRunResult(file_version="v2", fixes=[])

    accumulated.update(other)

    assert accumulated.version_diverged is True
    # The pinned version is kept, not overwritten by the diverged contribution's.
    assert accumulated.file_version == "v1"


async def test_merging_a_matching_contribution_does_not_mark_divergence() -> None:
    """The common case -- every contribution agrees on the file's version -- must
    not be flagged, or every ordinary concurrent run would be reported as unsafe to
    apply."""
    accumulated = GetLintFixesRunResult(file_version="v1", fixes=[])
    other = GetLintFixesRunResult(file_version="v1", fixes=[])

    accumulated.update(other)

    assert accumulated.version_diverged is False
    assert accumulated.file_version == "v1"
