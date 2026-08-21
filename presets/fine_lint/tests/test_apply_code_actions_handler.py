"""Tests for ``apply_code_actions``, the sole writer for code-action edits."""

from __future__ import annotations

import contextlib
import pathlib
import typing

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ifileeditor, iprojectactionrunner
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.testing import (
    FileOperation,
    FileOperationKind,
    InMemoryFileEditor,
    NoOpLogger,
)

from fine_lint import apply_code_actions_action as action_module
from fine_lint.apply_code_actions_action import (
    ApplyOutcome,
    CodeActionSelection,
    CreateFileOperation,
    DeleteFileOperation,
    RenameFileOperation,
    TextEditOperation,
)
from fine_lint.apply_code_actions_handler import ApplyCodeActionsHandler
from fine_lint.lint_fix import Position, Range, TextEdit
from fine_lint.resolve_code_action_action import ResolveCodeActionRunResult

_META = code_action.RunActionMeta(
    trigger=code_action.RunActionTrigger.USER, dev_env=code_action.DevEnv.CLI
)
_AUTHOR = ifileeditor.FileOperationAuthor(id="test")


class _RunContextStub:
    """Only the attribute ApplyCodeActionsHandler reads off the run context."""

    def __init__(self, meta: code_action.RunActionMeta) -> None:
        self.meta = meta


class _StubActionRunner:
    """Answers resolve_code_action from a canned table, keyed by (provider, action_id)."""

    def __init__(
        self, resolve_results: dict[tuple[str, str], ResolveCodeActionRunResult]
    ) -> None:
        self._resolve_results = resolve_results
        self.seen_payloads: list[typing.Any] = []

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
        self.seen_payloads.append(payload)
        return self._resolve_results[(payload.provider, payload.action_id)]

    def run_action_iter(
        self,
        action_type: iprojectactionrunner.ActionRef,
        payload: typing.Any,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> typing.Any:
        raise NotImplementedError


def _pos(line: int, character: int) -> Position:
    return Position(line=line, character=character)


def _range(sl: int, sc: int, el: int, ec: int) -> Range:
    return Range(start=_pos(sl, sc), end=_pos(el, ec))


def _edit(sl: int, sc: int, el: int, ec: int, new_text: str) -> TextEdit:
    return TextEdit(range=_range(sl, sc, el, ec), new_text=new_text)


async def _current_version(
    file_editor: InMemoryFileEditor, file_path: pathlib.Path
) -> str:
    async with file_editor.session(author=_AUTHOR) as session:
        return await session.read_file_version(file_path)


def _make_handler(
    file_editor: InMemoryFileEditor,
    action_runner: _StubActionRunner | None = None,
) -> ApplyCodeActionsHandler:
    return ApplyCodeActionsHandler(
        file_editor=file_editor,
        action_runner=typing.cast(
            iprojectactionrunner.IProjectActionRunner,
            action_runner or _StubActionRunner({}),
        ),
        logger=NoOpLogger(),
    )


async def test_two_non_overlapping_edits_from_different_providers_apply_together(
    tmp_path: pathlib.Path,
) -> None:
    """Two independent providers' fixes for the same file, submitted in one batch,
    must both land -- an agent applying a lint fix and a formatter fix together
    should not have to choose one over the other just because they arrived in the
    same request.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "abcdef")
    file_uri = path_to_resource_uri(file_path)
    base_version = await _current_version(file_editor, file_path)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=file_uri,
            operations=[
                TextEditOperation(
                    file_path=file_uri,
                    edits=[_edit(0, 0, 0, 1, "XYZ")],
                    file_version=base_version,
                )
            ],
        ),
        CodeActionSelection(
            provider="provider_b",
            action_id="b1",
            file_path=file_uri,
            operations=[
                TextEditOperation(
                    file_path=file_uri,
                    edits=[_edit(0, 3, 0, 4, "Q")],
                    file_version=base_version,
                )
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.APPLIED, 1: ApplyOutcome.APPLIED}
    assert file_editor.contents(file_path) == "XYZbcQef"


async def test_overlapping_edits_apply_the_first_and_defer_the_second(
    tmp_path: pathlib.Path,
) -> None:
    """Two fixes that touch the same text cannot both be applied. The caller's
    first (or preferred) choice must go through and the file must still be
    written with it -- refusing the whole file because of one conflict would
    lose an otherwise-good edit for no reason.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "import os\n")
    file_uri = path_to_resource_uri(file_path)
    base_version = await _current_version(file_editor, file_path)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=file_uri,
            operations=[
                TextEditOperation(
                    file_path=file_uri,
                    edits=[_edit(0, 0, 0, 7, "")],
                    file_version=base_version,
                )
            ],
        ),
        CodeActionSelection(
            provider="provider_b",
            action_id="b1",
            file_path=file_uri,
            operations=[
                TextEditOperation(
                    file_path=file_uri,
                    edits=[_edit(0, 0, 1, 0, "")],
                    file_version=base_version,
                )
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.APPLIED, 1: ApplyOutcome.DEFERRED}
    assert file_editor.writes != []
    assert file_editor.contents(file_path) == "os\n"


async def test_two_same_position_insertions_apply_one_and_defer_the_other(
    tmp_path: pathlib.Path,
) -> None:
    """Two providers each inserting text at the exact same point have no
    well-defined combined result -- accepting both would make the final text
    depend on arbitrary internal ordering rather than either provider's intent.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "abc")
    file_uri = path_to_resource_uri(file_path)
    base_version = await _current_version(file_editor, file_path)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=file_uri,
            operations=[
                TextEditOperation(
                    file_path=file_uri,
                    edits=[_edit(0, 1, 0, 1, "X")],
                    file_version=base_version,
                )
            ],
        ),
        CodeActionSelection(
            provider="provider_b",
            action_id="b1",
            file_path=file_uri,
            operations=[
                TextEditOperation(
                    file_path=file_uri,
                    edits=[_edit(0, 1, 0, 1, "Y")],
                    file_version=base_version,
                )
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.APPLIED, 1: ApplyOutcome.DEFERRED}
    assert file_editor.contents(file_path) == "aXbc"


async def test_selections_for_one_file_with_disagreeing_base_versions_are_refused(
    tmp_path: pathlib.Path,
) -> None:
    """A batch that mixes two different base versions for the same file has no
    single, simultaneously-valid interpretation, even if the edits themselves
    would not otherwise conflict -- applying it anyway could silently combine
    edits computed against two different documents.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "abcdef")
    file_uri = path_to_resource_uri(file_path)
    base_version = await _current_version(file_editor, file_path)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=file_uri,
            operations=[
                TextEditOperation(
                    file_path=file_uri,
                    edits=[_edit(0, 0, 0, 1, "X")],
                    file_version=base_version,
                )
            ],
        ),
        CodeActionSelection(
            provider="provider_b",
            action_id="b1",
            file_path=file_uri,
            operations=[
                TextEditOperation(
                    file_path=file_uri,
                    edits=[_edit(0, 5, 0, 6, "Y")],
                    file_version="a-different-version",
                )
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {
        0: ApplyOutcome.VERSION_CONFLICT,
        1: ApplyOutcome.VERSION_CONFLICT,
    }
    assert file_editor.writes == []
    assert file_editor.contents(file_path) == "abcdef"


async def test_a_stale_file_in_a_cross_file_fix_is_caught_even_when_it_now_matches_a_sibling(
    tmp_path: pathlib.Path,
) -> None:
    """THE BUG THIS FIXES: a cross-file fix edits two files, and one of them
    changed underneath the fix after it computed its edits. That file must be
    refused as ``VERSION_CONFLICT`` -- even in the specific case where its new
    (post-change) content happens to be byte-for-byte identical to the
    *other* file's content, so their content-hash versions coincide.

    A single version for the whole fix cannot tell these two files apart: it
    would validate the stale file against the healthy file's version, and
    because versions are content hashes, that check would pass -- not because
    the fix's edits for the stale file are actually safe, but because the
    stale file's *current* content happens to match a completely unrelated
    file. A repo full of empty ``__init__.py`` files makes two files sharing
    one hash the common case, not the exotic one. If this regresses, an
    agent applying a multi-file fix can silently corrupt a file that changed
    out from under it, with no error at all.
    """
    file_a = (tmp_path / "a.py").resolve()
    file_b = (tmp_path / "b.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_a, "same")
    # The fix examined file_b while it held "diff" and computed its edit for
    # file_b against that content -- this is file_b's true, correct base
    # version for this fix.
    file_editor.seed(file_b, "diff")
    uri_a = path_to_resource_uri(file_a)
    uri_b = path_to_resource_uri(file_b)
    file_a_version = await _current_version(file_editor, file_a)
    file_b_stale_version = await _current_version(file_editor, file_b)

    # Something else lands on file_b before the fix is applied, and its new
    # content happens to equal file_a's -- unrelated to the fix, but enough
    # to collide hashes with file_a.
    file_editor.seed(file_b, "same")
    assert file_a_version == await _current_version(file_editor, file_b)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="cross_file_fix",
            file_path=uri_a,
            operations=[
                TextEditOperation(
                    file_path=uri_a,
                    edits=[_edit(0, 0, 0, 1, "X")],
                    file_version=file_a_version,
                ),
                TextEditOperation(
                    file_path=uri_b,
                    edits=[_edit(0, 0, 0, 1, "Y")],
                    file_version=file_b_stale_version,
                ),
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.VERSION_CONFLICT}
    assert file_editor.writes == []
    assert file_editor.contents(file_a) == "same"
    assert file_editor.contents(file_b) == "same"


async def test_a_stale_base_version_is_refused_without_writing(
    tmp_path: pathlib.Path,
) -> None:
    """Edits computed against content that has since changed underneath them must
    not be written -- doing so would discard whatever produced the newer
    content, the same lost-update hazard the format pipeline's save step refuses
    against."""
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "abcdef")
    file_uri = path_to_resource_uri(file_path)
    stale_version = await _current_version(file_editor, file_path)
    file_editor.seed(file_path, "abcdef-changed")

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=file_uri,
            operations=[
                TextEditOperation(
                    file_path=file_uri,
                    edits=[_edit(0, 0, 0, 1, "X")],
                    file_version=stale_version,
                )
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.VERSION_CONFLICT}
    assert file_editor.writes == []
    assert file_editor.contents(file_path) == "abcdef-changed"


async def test_one_files_invalid_range_blocks_writes_to_every_file_in_the_batch(
    tmp_path: pathlib.Path,
) -> None:
    """A batch is validated as a whole before anything is written. One file's
    malformed edit must not let a *different* file in the same batch go through
    -- an agent applying several fixes at once needs an atomic outcome to reason
    about, not a partially-applied batch it has to diff to understand.
    """
    good_path = (tmp_path / "good.py").resolve()
    bad_path = (tmp_path / "bad.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(good_path, "abcdef")
    file_editor.seed(bad_path, "xy")
    good_uri = path_to_resource_uri(good_path)
    bad_uri = path_to_resource_uri(bad_path)
    good_version = await _current_version(file_editor, good_path)
    bad_version = await _current_version(file_editor, bad_path)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=good_uri,
            operations=[
                TextEditOperation(
                    file_path=good_uri,
                    edits=[_edit(0, 0, 0, 1, "X")],
                    file_version=good_version,
                )
            ],
        ),
        CodeActionSelection(
            provider="provider_b",
            action_id="b1",
            file_path=bad_uri,
            operations=[
                TextEditOperation(
                    file_path=bad_uri,
                    # Character 99 does not exist on line 0 of "xy".
                    edits=[_edit(0, 0, 0, 99, "Z")],
                    file_version=bad_version,
                )
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes[1] == ApplyOutcome.INVALID_RANGE
    assert file_editor.writes == []
    assert file_editor.contents(good_path) == "abcdef"
    assert file_editor.contents(bad_path) == "xy"


async def test_a_stub_selection_is_resolved_before_applying(
    tmp_path: pathlib.Path,
) -> None:
    """A selection built from a lazily-resolved code action (``operations=None``)
    must still apply -- an IDE or agent that only has a provider/action_id pair
    (not the operations themselves) needs apply to do the resolve step on its
    behalf.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "abcdef")
    file_uri = path_to_resource_uri(file_path)
    base_version = await _current_version(file_editor, file_path)

    action_runner = _StubActionRunner(
        {
            ("provider_a", "a1"): ResolveCodeActionRunResult(
                file_version=base_version,
                operations=[
                    TextEditOperation(
                        file_path=file_uri,
                        edits=[_edit(0, 0, 0, 1, "X")],
                        file_version=base_version,
                    )
                ],
            )
        }
    )
    handler = _make_handler(file_editor, action_runner)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=file_uri,
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.APPLIED}
    assert file_editor.contents(file_path) == "Xbcdef"
    assert len(action_runner.seen_payloads) == 1


async def test_an_unresolvable_selection_does_not_block_the_rest_of_the_batch(
    tmp_path: pathlib.Path,
) -> None:
    """A stale or already-applied action_id that no provider claims must be
    reported on its own and must not prevent the batch's other, still-valid
    selections from applying -- one dead reference should not sink an entire
    apply request.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "abcdef")
    file_uri = path_to_resource_uri(file_path)
    base_version = await _current_version(file_editor, file_path)

    action_runner = _StubActionRunner(
        {
            ("provider_a", "gone"): ResolveCodeActionRunResult(
                file_version=base_version, operations=None
            ),
            ("provider_b", "b1"): ResolveCodeActionRunResult(
                file_version=base_version,
                operations=[
                    TextEditOperation(
                        file_path=file_uri,
                        edits=[_edit(0, 0, 0, 1, "X")],
                        file_version=base_version,
                    )
                ],
            ),
        }
    )
    handler = _make_handler(file_editor, action_runner)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="gone",
            file_path=file_uri,
        ),
        CodeActionSelection(
            provider="provider_b",
            action_id="b1",
            file_path=file_uri,
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.UNRESOLVED, 1: ApplyOutcome.APPLIED}
    assert file_editor.contents(file_path) == "Xbcdef"


async def test_two_operations_for_the_same_file_in_one_selection_apply_in_order(
    tmp_path: pathlib.Path,
) -> None:
    """A single code action that edits one file twice (e.g. two independent
    fixes bundled by the same provider into one action) must have its second
    edit land against the text the first one produced -- not against the
    original text, which would place it at the wrong position or corrupt
    unrelated content.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "abc")
    file_uri = path_to_resource_uri(file_path)
    base_version = await _current_version(file_editor, file_path)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=file_uri,
            operations=[
                TextEditOperation(
                    file_path=file_uri,
                    edits=[_edit(0, 0, 0, 0, "X")],
                    file_version=base_version,
                ),
                # This edit's position (0, 1) only makes sense against the
                # content the first operation produced ("Xabc"): it targets
                # the "a" that is now at index 1, not the "b" that was there
                # in the original "abc".
                TextEditOperation(
                    file_path=file_uri,
                    edits=[_edit(0, 1, 0, 2, "Y")],
                ),
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.APPLIED}
    assert file_editor.contents(file_path) == "XYbc"


async def test_a_selection_that_creates_then_edits_applies_both_in_order(
    tmp_path: pathlib.Path,
) -> None:
    """A create followed by an edit of the created file must land as two steps
    in that order -- the edit is computed against the empty file the create
    produced, never against a file that does not exist."""
    new_path = (tmp_path / "new.py").resolve()
    file_editor = InMemoryFileEditor()
    new_uri = path_to_resource_uri(new_path)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=new_uri,
            operations=[
                CreateFileOperation(file_path=new_uri),
                TextEditOperation(
                    file_path=new_uri,
                    edits=[_edit(0, 0, 0, 0, "x = 1\n")],
                    file_version=None,
                ),
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.APPLIED}
    assert file_editor.contents(new_path) == "x = 1\n"
    assert file_editor.operations == [
        FileOperation(FileOperationKind.CREATE, (new_path,))
    ]


async def test_a_create_on_an_occupied_path_is_file_exists_and_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """Creating over an existing file without permission is a stale action, not
    a version disagreement -- the caller must be told the file already exists,
    and the rest of the batch must stay untouched."""
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "abcdef")
    file_uri = path_to_resource_uri(file_path)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=file_uri,
            operations=[CreateFileOperation(file_path=file_uri)],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.FILE_EXISTS}
    assert file_editor.writes == []
    assert file_editor.contents(file_path) == "abcdef"


async def test_a_text_edit_naming_a_missing_path_is_file_missing(
    tmp_path: pathlib.Path,
) -> None:
    """An edit naming a file that does not exist must be reported as missing,
    never as an exception escaping the action -- a caller gets an outcome it
    can act on rather than a traceback."""
    missing_path = (tmp_path / "missing.py").resolve()
    file_editor = InMemoryFileEditor()
    missing_uri = path_to_resource_uri(missing_path)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=missing_uri,
            operations=[
                TextEditOperation(
                    file_path=missing_uri,
                    edits=[_edit(0, 0, 0, 1, "X")],
                    file_version=None,
                )
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.FILE_MISSING}
    assert file_editor.writes == []


async def test_a_batch_naming_two_missing_files_reports_both(
    tmp_path: pathlib.Path,
) -> None:
    """One missing file must not hide another: reporting every missing path in
    one pass is the difference between one round-trip and two for a caller
    fixing a stale action."""
    first = (tmp_path / "first.py").resolve()
    second = (tmp_path / "second.py").resolve()
    file_editor = InMemoryFileEditor()
    first_uri = path_to_resource_uri(first)
    second_uri = path_to_resource_uri(second)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=first_uri,
            operations=[
                TextEditOperation(
                    file_path=first_uri,
                    edits=[_edit(0, 0, 0, 1, "X")],
                    file_version=None,
                )
            ],
        ),
        CodeActionSelection(
            provider="provider_b",
            action_id="b1",
            file_path=second_uri,
            operations=[
                TextEditOperation(
                    file_path=second_uri,
                    edits=[_edit(0, 0, 0, 1, "Y")],
                    file_version=None,
                )
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {
        0: ApplyOutcome.FILE_MISSING,
        1: ApplyOutcome.FILE_MISSING,
    }


async def test_an_edit_naming_a_created_path_must_carry_no_version(
    tmp_path: pathlib.Path,
) -> None:
    """A file that does not exist yet has no content hash to guard against, so
    an edit computed against a created path must carry ``None`` -- and a
    non-None version there is refused before anything is written."""
    new_path = (tmp_path / "new.py").resolve()
    file_editor = InMemoryFileEditor()
    new_uri = path_to_resource_uri(new_path)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=new_uri,
            operations=[
                CreateFileOperation(file_path=new_uri),
                TextEditOperation(
                    file_path=new_uri,
                    edits=[_edit(0, 0, 0, 0, "x = 1\n")],
                    file_version="a-version-that-cannot-exist",
                ),
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.VERSION_CONFLICT}
    assert file_editor.writes == []


async def test_a_rename_plus_an_edit_of_the_new_path_applies_to_the_renamed_file(
    tmp_path: pathlib.Path,
) -> None:
    """A rename followed by an edit naming the new path must edit the content
    the rename produced -- the edit carries no version because the new path
    has none, while the rename still guards the old path's version."""
    old_path = (tmp_path / "old.py").resolve()
    new_path = (tmp_path / "new.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(old_path, "abcdef")
    old_uri = path_to_resource_uri(old_path)
    new_uri = path_to_resource_uri(new_path)
    old_version = await _current_version(file_editor, old_path)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=old_uri,
            operations=[
                RenameFileOperation(
                    old_path=old_uri, new_path=new_uri, file_version=old_version
                ),
                TextEditOperation(
                    file_path=new_uri,
                    edits=[_edit(0, 0, 0, 1, "X")],
                    file_version=None,
                ),
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.APPLIED}
    assert file_editor.contents(new_path) == "Xbcdef"


async def test_delete_wins_over_a_foreign_edit_on_the_same_path(
    tmp_path: pathlib.Path,
) -> None:
    """A delete and a text edit from different selections have no simultaneous
    meaning; the first accepted selection wins and the other is deferred."""
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "abcdef")
    file_uri = path_to_resource_uri(file_path)
    base_version = await _current_version(file_editor, file_path)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=file_uri,
            operations=[
                DeleteFileOperation(file_path=file_uri, file_version=base_version)
            ],
        ),
        CodeActionSelection(
            provider="provider_b",
            action_id="b1",
            file_path=file_uri,
            operations=[
                TextEditOperation(
                    file_path=file_uri,
                    edits=[_edit(0, 0, 0, 1, "X")],
                    file_version=base_version,
                )
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.APPLIED, 1: ApplyOutcome.DEFERRED}
    assert file_editor.contents(file_path) == ""


async def test_a_foreign_edit_wins_over_a_delete_on_the_same_path(
    tmp_path: pathlib.Path,
) -> None:
    """The reverse order of the delete-vs-edit conflict: an edit accepted
    first keeps the file, and the later delete is deferred."""
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "abcdef")
    file_uri = path_to_resource_uri(file_path)
    base_version = await _current_version(file_editor, file_path)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=file_uri,
            operations=[
                TextEditOperation(
                    file_path=file_uri,
                    edits=[_edit(0, 0, 0, 1, "X")],
                    file_version=base_version,
                )
            ],
        ),
        CodeActionSelection(
            provider="provider_b",
            action_id="b1",
            file_path=file_uri,
            operations=[
                DeleteFileOperation(file_path=file_uri, file_version=base_version)
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.APPLIED, 1: ApplyOutcome.DEFERRED}
    assert file_editor.contents(file_path) == "Xbcdef"


async def test_create_wins_over_a_foreign_edit_on_an_existing_path(
    tmp_path: pathlib.Path,
) -> None:
    """A create that discards an existing file's content has no simultaneous
    meaning with an edit of that content; the first accepted selection wins."""
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "abcdef")
    file_uri = path_to_resource_uri(file_path)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=file_uri,
            operations=[CreateFileOperation(file_path=file_uri, overwrite=True)],
        ),
        CodeActionSelection(
            provider="provider_b",
            action_id="b1",
            file_path=file_uri,
            operations=[
                TextEditOperation(
                    file_path=file_uri,
                    edits=[_edit(0, 0, 0, 1, "X")],
                    file_version=None,
                )
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.APPLIED, 1: ApplyOutcome.DEFERRED}
    assert file_editor.contents(file_path) == ""


async def test_a_foreign_edit_on_a_path_another_selection_creates_is_file_missing(
    tmp_path: pathlib.Path,
) -> None:
    """An edit naming a path that does not exist and that its own selection
    does not create is missing -- it never reaches conflict resolution, so the
    create does not win by default."""
    new_path = (tmp_path / "new.py").resolve()
    file_editor = InMemoryFileEditor()
    new_uri = path_to_resource_uri(new_path)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=new_uri,
            operations=[CreateFileOperation(file_path=new_uri)],
        ),
        CodeActionSelection(
            provider="provider_b",
            action_id="b1",
            file_path=new_uri,
            operations=[
                TextEditOperation(
                    file_path=new_uri,
                    edits=[_edit(0, 0, 0, 1, "X")],
                    file_version=None,
                )
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes[1] == ApplyOutcome.FILE_MISSING
    assert file_editor.writes == []


async def test_a_rename_chain_moves_content_twice(
    tmp_path: pathlib.Path,
) -> None:
    """A chain of renames (a -> b -> c) must move the original content to the
    final path, with each link guarding nothing but the first path's version."""
    a_path = (tmp_path / "a.py").resolve()
    b_path = (tmp_path / "b.py").resolve()
    c_path = (tmp_path / "c.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(a_path, "abcdef")
    a_uri = path_to_resource_uri(a_path)
    b_uri = path_to_resource_uri(b_path)
    c_uri = path_to_resource_uri(c_path)
    a_version = await _current_version(file_editor, a_path)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=a_uri,
            operations=[
                RenameFileOperation(
                    old_path=a_uri, new_path=b_uri, file_version=a_version
                ),
                RenameFileOperation(old_path=b_uri, new_path=c_uri),
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.APPLIED}
    assert file_editor.contents(c_path) == "abcdef"


async def test_create_then_delete_leaves_the_path_gone(
    tmp_path: pathlib.Path,
) -> None:
    """A create immediately followed by a delete of the same path lands both,
    in order -- the net effect is that the file does not exist."""
    new_path = (tmp_path / "new.py").resolve()
    file_editor = InMemoryFileEditor()
    new_uri = path_to_resource_uri(new_path)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=new_uri,
            operations=[
                CreateFileOperation(file_path=new_uri),
                DeleteFileOperation(file_path=new_uri),
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.APPLIED}
    assert file_editor.contents(new_path) == ""
    assert file_editor.operations == [
        FileOperation(FileOperationKind.CREATE, (new_path,)),
        FileOperation(FileOperationKind.DELETE, (new_path,)),
    ]


async def test_a_rename_onto_a_path_the_batch_also_creates(
    tmp_path: pathlib.Path,
) -> None:
    """A rename onto a path an earlier operation in the same selection created
    must overwrite the empty created file with the renamed content."""
    old_path = (tmp_path / "old.py").resolve()
    new_path = (tmp_path / "new.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(old_path, "old content")
    old_uri = path_to_resource_uri(old_path)
    new_uri = path_to_resource_uri(new_path)
    old_version = await _current_version(file_editor, old_path)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=old_uri,
            operations=[
                CreateFileOperation(file_path=new_uri),
                RenameFileOperation(
                    old_path=old_uri,
                    new_path=new_uri,
                    overwrite=True,
                    file_version=old_version,
                ),
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.APPLIED}
    assert file_editor.contents(new_path) == "old content"


async def test_recursive_delete_without_opt_in_is_refused_unsafe(
    tmp_path: pathlib.Path,
) -> None:
    """A recursive delete is the one operation that can remove work never
    named in the batch, so it must be refused unless the run explicitly opted
    in -- and the refusal must leave the tree untouched."""
    directory = (tmp_path / "pkg").resolve()
    directory.mkdir()
    nested = directory / "module.py"
    nested.write_text("x = 1\n")
    file_editor = InMemoryFileEditor()
    file_editor.seed(nested, "x = 1\n")
    directory_uri = path_to_resource_uri(directory)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=directory_uri,
            operations=[DeleteFileOperation(file_path=directory_uri, recursive=True)],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.REFUSED_UNSAFE}
    assert file_editor.contents(nested) == "x = 1\n"
    assert nested.exists()


async def test_dry_run_reports_deleted_paths_without_deleting(
    tmp_path: pathlib.Path,
) -> None:
    """A preview of a delete must say which path would go, and must leave the
    file untouched on disk."""
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "abcdef")
    file_uri = path_to_resource_uri(file_path)
    base_version = await _current_version(file_editor, file_path)

    handler = _make_handler(file_editor)
    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(
            selections=[
                CodeActionSelection(
                    provider="provider_a",
                    action_id="a1",
                    file_path=file_uri,
                    operations=[
                        DeleteFileOperation(
                            file_path=file_uri, file_version=base_version
                        )
                    ],
                )
            ],
            dry_run=True,
        ),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.dry_run is True
    assert result.deleted_paths == [file_uri]
    assert file_editor.contents(file_path) == "abcdef"


async def test_merged_dry_runs_keep_both_deleted_path_lists(
    tmp_path: pathlib.Path,
) -> None:
    """Two previews merged into one result must not drop either preview's
    deleted paths -- a field the merge forgets is silently lost to the caller."""
    first = (tmp_path / "first.py").resolve()
    second = (tmp_path / "second.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(first, "a")
    file_editor.seed(second, "b")
    first_uri = path_to_resource_uri(first)
    second_uri = path_to_resource_uri(second)

    handler = _make_handler(file_editor)
    first_result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(
            selections=[
                CodeActionSelection(
                    provider="provider_a",
                    action_id="a1",
                    file_path=first_uri,
                    operations=[DeleteFileOperation(file_path=first_uri)],
                )
            ],
            dry_run=True,
        ),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )
    second_result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(
            selections=[
                CodeActionSelection(
                    provider="provider_b",
                    action_id="b1",
                    file_path=second_uri,
                    operations=[DeleteFileOperation(file_path=second_uri)],
                )
            ],
            dry_run=True,
        ),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    first_result.update(second_result)

    assert sorted(first_result.deleted_paths) == sorted([first_uri, second_uri])


class _FailSecondDeleteSession:
    """Wraps an in-memory session and raises on the second delete, so a test
    can force a commit-time failure after a first delete already landed."""

    def __init__(self, inner: typing.Any, delete_calls: list[int]) -> None:
        self._inner = inner
        self._delete_calls = delete_calls

    async def delete_file(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        self._delete_calls[0] += 1
        if self._delete_calls[0] == 2:
            raise OSError("injected delete failure")
        return await self._inner.delete_file(*args, **kwargs)

    def __getattr__(self, name: str) -> typing.Any:
        return getattr(self._inner, name)


class _FailSecondDeleteEditor(InMemoryFileEditor):
    def __init__(self) -> None:
        super().__init__()
        self._delete_calls = [0]

    @contextlib.asynccontextmanager
    async def session(self, author: ifileeditor.FileOperationAuthor) -> typing.Any:
        async with super().session(author) as inner:
            yield _FailSecondDeleteSession(inner, self._delete_calls)


async def test_a_failed_commit_step_after_a_delete_reports_partially_applied(
    tmp_path: pathlib.Path,
) -> None:
    """When a later commit step fails after an earlier delete already landed,
    the selection is partially applied -- retrying it would try to delete an
    already-deleted file, so it must not be reported as a plain write failure."""
    first = (tmp_path / "first.py").resolve()
    second = (tmp_path / "second.py").resolve()
    file_editor = _FailSecondDeleteEditor()
    file_editor.seed(first, "a")
    file_editor.seed(second, "b")
    first_uri = path_to_resource_uri(first)
    second_uri = path_to_resource_uri(second)
    first_version = await _current_version(file_editor, first)
    second_version = await _current_version(file_editor, second)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=first_uri,
            operations=[
                DeleteFileOperation(file_path=first_uri, file_version=first_version),
                DeleteFileOperation(file_path=second_uri, file_version=second_version),
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.PARTIALLY_APPLIED}
    assert file_editor.contents(first) == ""
    assert file_editor.contents(second) == "b"


async def test_a_missing_file_version_still_writes_guarded_only_by_the_claim(
    tmp_path: pathlib.Path,
) -> None:
    """An operation that supplies no base version is not skipped -- it still
    writes, guarded only by the exclusive claim taken while applying the
    batch. ``file_version=None`` means "no extra staleness guard", not "do
    nothing".
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "abcdef")
    file_uri = path_to_resource_uri(file_path)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=file_uri,
            operations=[
                TextEditOperation(
                    file_path=file_uri,
                    edits=[_edit(0, 0, 0, 1, "X")],
                    file_version=None,
                )
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.APPLIED}
    assert file_editor.writes != []
    assert file_editor.contents(file_path) == "Xbcdef"


async def test_dry_run_previews_content_without_writing(
    tmp_path: pathlib.Path,
) -> None:
    """A dry run must hand back the content a real apply would produce, so a
    caller can show it to a user or a task-driven workflow before committing
    -- but it must leave the file on disk exactly as it found it, and it must
    say so unambiguously so a caller can never mistake the preview for a
    completed write.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "abcdef")
    file_uri = path_to_resource_uri(file_path)
    base_version = await _current_version(file_editor, file_path)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=file_uri,
            operations=[
                TextEditOperation(
                    file_path=file_uri,
                    edits=[_edit(0, 0, 0, 1, "X")],
                    file_version=base_version,
                )
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections, dry_run=True),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.dry_run is True
    assert result.outcomes == {0: ApplyOutcome.APPLIED}
    assert result.resulting_content == {file_uri: "Xbcdef"}
    assert file_editor.writes == []
    assert file_editor.contents(file_path) == "abcdef"


async def test_dry_run_reports_the_same_conflict_outcomes_a_real_run_would(
    tmp_path: pathlib.Path,
) -> None:
    """A preview must be trustworthy about *which* selections would be
    deferred or refused, not just about the content of the ones that would
    succeed -- a caller deciding whether to proceed needs to see the same
    conflicts a real run would report.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "import os\n")
    file_uri = path_to_resource_uri(file_path)
    base_version = await _current_version(file_editor, file_path)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a",
            action_id="a1",
            file_path=file_uri,
            operations=[
                TextEditOperation(
                    file_path=file_uri,
                    edits=[_edit(0, 0, 0, 7, "")],
                    file_version=base_version,
                )
            ],
        ),
        CodeActionSelection(
            provider="provider_b",
            action_id="b1",
            file_path=file_uri,
            operations=[
                TextEditOperation(
                    file_path=file_uri,
                    edits=[_edit(0, 0, 1, 0, "")],
                    file_version=base_version,
                )
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections, dry_run=True),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes == {0: ApplyOutcome.APPLIED, 1: ApplyOutcome.DEFERRED}
    assert file_editor.writes == []
    assert file_editor.contents(file_path) == "import os\n"


async def test_a_selection_resolving_to_no_operations_is_not_reported_as_applied(
    tmp_path: pathlib.Path,
) -> None:
    """A selection whose operation list is empty wrote nothing, so it must not be
    tagged APPLIED.

    An empty operation list trivially conflicts with nothing, so the greedy pass
    used to accept it. That matters because the re-fix loop above counts APPLIED
    as progress: a lint fix carrying no edits (ruff offers those -- a quickfix
    whose `edit` is absent) would make every pass "apply" something while the
    file's content never changed, and the run would end as OSCILLATED with a
    user-facing warning on a workspace that was already clean.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "abcdef")
    file_uri = path_to_resource_uri(file_path)
    base_version = await _current_version(file_editor, file_path)

    handler = _make_handler(file_editor)
    selections = [
        CodeActionSelection(
            provider="provider_a", action_id="empty", file_path=file_uri, operations=[]
        ),
        CodeActionSelection(
            provider="provider_b",
            action_id="real",
            file_path=file_uri,
            operations=[
                TextEditOperation(
                    file_path=file_uri,
                    edits=[_edit(0, 0, 0, 1, "X")],
                    file_version=base_version,
                )
            ],
        ),
    ]

    result = await handler.run(
        action_module.ApplyCodeActionsRunPayload(selections=selections),
        typing.cast(typing.Any, _RunContextStub(meta=_META)),
    )

    assert result.outcomes.get(0) != ApplyOutcome.APPLIED
    assert result.outcomes[1] == ApplyOutcome.APPLIED
