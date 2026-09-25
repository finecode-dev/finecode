"""Tests for ``apply_lint_fixes_files``, the pass loop behind the ``--fix``-style
workflow (ADR-0085)."""

from __future__ import annotations

import pathlib
import typing
from collections.abc import Callable

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ifileeditor, iprojectactionrunner
from finecode_extension_api.resource_uri import (
    ResourceUri,
    path_to_resource_uri,
    resource_uri_to_path,
)
from finecode_extension_runner.testing import InMemoryFileEditor, NoOpLogger

from fine_lint.apply_code_actions_action import ApplyCodeActionsRunPayload
from fine_lint.apply_code_actions_handler import ApplyCodeActionsHandler
from fine_lint.apply_lint_fixes_files_action import (
    ApplyLintFixesFilesRunPayload,
    ConvergenceStatus,
)
from fine_lint.apply_lint_fixes_files_handler import ApplyLintFixesFilesHandler
from fine_lint.get_lint_fixes_action import (
    GetLintFixesRunPayload,
    GetLintFixesRunResult,
)
from fine_lint.lint_fix import FixApplicability, LintFix, Position, Range, TextEdit

_META = code_action.RunActionMeta(
    trigger=code_action.RunActionTrigger.USER, dev_env=code_action.DevEnv.CLI
)
_META_SYSTEM = code_action.RunActionMeta(
    trigger=code_action.RunActionTrigger.SYSTEM, dev_env=code_action.DevEnv.CLI
)
_AUTHOR = ifileeditor.FileOperationAuthor(id="test")


class _FakeUserMessenger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def error(self, message: str) -> None: ...

    def info(self, message: str) -> None: ...


class _RunContextStub:
    """Only the attribute ApplyLintFixesFilesHandler reads off the run context."""

    def __init__(self, meta: code_action.RunActionMeta) -> None:
        self.meta = meta


def _pos(line: int, character: int) -> Position:
    return Position(line=line, character=character)


def _make_fix(
    fix_id: str,
    file_uri: ResourceUri,
    start: Position,
    end: Position,
    new_text: str,
    applicability: FixApplicability = FixApplicability.SAFE,
    kind: str = "quickfix",
) -> LintFix:
    edit_range = Range(start=start, end=end)
    return LintFix(
        fix_id=fix_id,
        title=fix_id,
        kind=kind,
        edits={file_uri: [TextEdit(range=edit_range, new_text=new_text)]},
        target_range=edit_range,
        target_codes=[],
        applicability=applicability,
    )


class _ScriptedActionRunner:
    """Answers ``get_lint_fixes`` from a content-driven script, and routes
    ``apply_code_actions`` to a real ``ApplyCodeActionsHandler`` over the same
    file editor -- so the pass loop's content mutations are genuine, not a
    canned outcome."""

    def __init__(
        self,
        file_editor: InMemoryFileEditor,
        script: Callable[[ResourceUri, str], list[LintFix]],
    ) -> None:
        self._file_editor = file_editor
        self._script = script
        self._apply_handler = ApplyCodeActionsHandler(
            file_editor=file_editor,
            action_runner=typing.cast(iprojectactionrunner.IProjectActionRunner, self),
            logger=NoOpLogger(),
        )

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
        if isinstance(payload, GetLintFixesRunPayload):
            file_path = resource_uri_to_path(payload.file_path)
            async with self._file_editor.session(_AUTHOR) as session:
                version = await session.read_file_version(file_path)
                async with session.read_file(file_path) as file_info:
                    content = file_info.content
            fixes = self._script(payload.file_path, content)
            return GetLintFixesRunResult(file_version=version, fixes=fixes)
        if isinstance(payload, ApplyCodeActionsRunPayload):
            return await self._apply_handler.run(
                payload, typing.cast(typing.Any, _RunContextStub(meta))
            )
        raise NotImplementedError

    def run_action_iter(
        self,
        action_type: iprojectactionrunner.ActionRef,
        payload: typing.Any,
        meta: code_action.RunActionMeta,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
    ) -> typing.Any:
        raise NotImplementedError


def _make_handler(
    file_editor: InMemoryFileEditor,
    script: Callable[[ResourceUri, str], list[LintFix]],
    user_messenger: _FakeUserMessenger | None = None,
) -> ApplyLintFixesFilesHandler:
    action_runner = _ScriptedActionRunner(file_editor, script)
    return ApplyLintFixesFilesHandler(
        file_editor=file_editor,
        action_runner=typing.cast(
            iprojectactionrunner.IProjectActionRunner, action_runner
        ),
        logger=NoOpLogger(),
        user_messenger=typing.cast(
            typing.Any,
            user_messenger if user_messenger is not None else _FakeUserMessenger(),
        ),
    )


async def test_a_fix_revealed_only_after_an_earlier_fix_is_applied_lands_in_a_later_pass(
    tmp_path: pathlib.Path,
) -> None:
    """A fix that only becomes visible once an earlier one has been written must
    still get applied in the same run -- a user asking to fix a file should not
    have to re-run the command themselves to pick up what the first fix
    exposed.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "import os\nimport sys\n")
    file_uri = path_to_resource_uri(file_path)

    def script(uri: ResourceUri, content: str) -> list[LintFix]:
        if "import os" in content:
            return [_make_fix("remove_os", uri, _pos(0, 0), _pos(1, 0), "")]
        if "import sys" in content:
            return [_make_fix("remove_sys", uri, _pos(0, 0), _pos(1, 0), "")]
        return []

    handler = _make_handler(file_editor, script)
    result = await handler.run(
        ApplyLintFixesFilesRunPayload(file_paths=[file_uri]),
        typing.cast(typing.Any, _RunContextStub(_META)),
    )

    assert file_editor.contents(file_path) == ""
    assert result.applied_counts[file_uri] == 2
    assert result.status == ConvergenceStatus.CONVERGED


async def test_dry_run_previews_pass_one_and_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """A dry run must show what the fix workflow would do without touching the
    file -- letting a user or an automated task-driven workflow review a
    preview before committing to it. It only covers the first pass: a fix
    that would only appear after an earlier one is written stays invisible to
    the preview, and the run reports that plainly (status ``PREVIEWED``)
    rather than claiming to have converged.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "import os\nimport sys\n")
    file_uri = path_to_resource_uri(file_path)

    def script(uri: ResourceUri, content: str) -> list[LintFix]:
        if "import os" in content:
            return [_make_fix("remove_os", uri, _pos(0, 0), _pos(1, 0), "")]
        if "import sys" in content:
            return [_make_fix("remove_sys", uri, _pos(0, 0), _pos(1, 0), "")]
        return []

    handler = _make_handler(file_editor, script)
    result = await handler.run(
        ApplyLintFixesFilesRunPayload(file_paths=[file_uri], dry_run=True),
        typing.cast(typing.Any, _RunContextStub(_META)),
    )

    assert result.status == ConvergenceStatus.PREVIEWED
    assert result.passes == 1
    assert result.applied_counts[file_uri] == 1
    assert result.resulting_content[file_uri] == "import sys\n"
    assert file_editor.contents(file_path) == "import os\nimport sys\n"


async def test_two_fixes_undoing_each_other_terminate_via_oscillation_not_max_passes(
    tmp_path: pathlib.Path,
) -> None:
    """Two fixes that keep reverting each other's work must be caught and
    reported immediately, not left to spin until an arbitrary pass budget runs
    out -- a run that could detect this instantly but doesn't wastes the user's
    time for no better answer.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "x=1\n")
    file_uri = path_to_resource_uri(file_path)

    def script(uri: ResourceUri, content: str) -> list[LintFix]:
        if content == "x=1\n":
            return [_make_fix("flip_to_2", uri, _pos(0, 0), _pos(1, 0), "x=2\n")]
        if content == "x=2\n":
            return [_make_fix("flip_to_1", uri, _pos(0, 0), _pos(1, 0), "x=1\n")]
        return []

    handler = _make_handler(file_editor, script)
    result = await handler.run(
        ApplyLintFixesFilesRunPayload(file_paths=[file_uri], max_passes=5),
        typing.cast(typing.Any, _RunContextStub(_META)),
    )

    assert result.status == ConvergenceStatus.OSCILLATED
    assert result.passes < 5
    assert [fix.fix_id for fix in result.remaining_fixes[file_uri]] == ["flip_to_1"]


async def test_unsafe_fixes_are_skipped_by_default(tmp_path: pathlib.Path) -> None:
    """An unsafe fix must not be written unless the caller opted in -- applying
    a behavior-changing fix without being asked is the kind of surprise that
    makes an automated "fix everything" workflow unsafe to trust.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "S,U,D")
    file_uri = path_to_resource_uri(file_path)

    def script(uri: ResourceUri, content: str) -> list[LintFix]:
        fixes = []
        if "S," in content:
            idx = content.index("S,")
            fixes.append(
                _make_fix(
                    "fix_s",
                    uri,
                    _pos(0, idx),
                    _pos(0, idx + 2),
                    "",
                    applicability=FixApplicability.SAFE,
                )
            )
        if "U," in content:
            idx = content.index("U,")
            fixes.append(
                _make_fix(
                    "fix_u",
                    uri,
                    _pos(0, idx),
                    _pos(0, idx + 2),
                    "",
                    applicability=FixApplicability.UNSAFE,
                )
            )
        if "D" in content:
            idx = content.index("D")
            fixes.append(
                _make_fix(
                    "fix_d",
                    uri,
                    _pos(0, idx),
                    _pos(0, idx + 1),
                    "",
                    applicability=FixApplicability.DISPLAY_ONLY,
                )
            )
        return fixes

    handler = _make_handler(file_editor, script)
    result = await handler.run(
        ApplyLintFixesFilesRunPayload(file_paths=[file_uri]),
        typing.cast(typing.Any, _RunContextStub(_META)),
    )

    assert file_editor.contents(file_path) == "U,D"
    assert result.status == ConvergenceStatus.CONVERGED


async def test_include_unsafe_applies_unsafe_fixes_but_never_display_only(
    tmp_path: pathlib.Path,
) -> None:
    """Opting in to unsafe fixes must apply them, but a display-only fix must
    never be written even then -- it carries no edits a user has reviewed and
    accepted, so there is nothing safe to automate about it regardless of the
    unsafe flag.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "S,U,D")
    file_uri = path_to_resource_uri(file_path)

    def script(uri: ResourceUri, content: str) -> list[LintFix]:
        fixes = []
        if "S," in content:
            idx = content.index("S,")
            fixes.append(
                _make_fix(
                    "fix_s",
                    uri,
                    _pos(0, idx),
                    _pos(0, idx + 2),
                    "",
                    applicability=FixApplicability.SAFE,
                )
            )
        if "U," in content:
            idx = content.index("U,")
            fixes.append(
                _make_fix(
                    "fix_u",
                    uri,
                    _pos(0, idx),
                    _pos(0, idx + 2),
                    "",
                    applicability=FixApplicability.UNSAFE,
                )
            )
        if "D" in content:
            idx = content.index("D")
            fixes.append(
                _make_fix(
                    "fix_d",
                    uri,
                    _pos(0, idx),
                    _pos(0, idx + 1),
                    "",
                    applicability=FixApplicability.DISPLAY_ONLY,
                )
            )
        return fixes

    handler = _make_handler(file_editor, script)
    result = await handler.run(
        ApplyLintFixesFilesRunPayload(file_paths=[file_uri], include_unsafe=True),
        typing.cast(typing.Any, _RunContextStub(_META)),
    )

    assert file_editor.contents(file_path) == "D"
    assert result.status == ConvergenceStatus.CONVERGED


async def test_kinds_filter_matches_a_sub_kind_hierarchically(
    tmp_path: pathlib.Path,
) -> None:
    """Asking for ``source.fixAll`` must also apply a tool's more specific
    ``source.fixAll.ruff`` fix -- LSP kinds are hierarchical, and a caller
    should not have to enumerate every tool's specific sub-kind just to ask
    for the general category.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "x=1\n")
    file_uri = path_to_resource_uri(file_path)
    fix = _make_fix(
        "fixall_ruff",
        file_uri,
        _pos(0, 0),
        _pos(1, 0),
        "x=2\n",
        kind="source.fixAll.ruff",
    )

    def script(uri: ResourceUri, content: str) -> list[LintFix]:
        return [fix] if content == "x=1\n" else []

    handler = _make_handler(file_editor, script)
    result = await handler.run(
        ApplyLintFixesFilesRunPayload(file_paths=[file_uri], kinds=["source.fixAll"]),
        typing.cast(typing.Any, _RunContextStub(_META)),
    )

    assert file_editor.contents(file_path) == "x=2\n"
    assert result.applied_counts[file_uri] == 1


async def test_kinds_filter_excludes_a_fix_of_an_unrelated_kind(
    tmp_path: pathlib.Path,
) -> None:
    """A fix whose kind does not fall under the requested one must not be
    applied -- otherwise a caller asking specifically for ``source.fixAll``
    fixes (e.g. a fix-on-save hook) would get unrelated quickfixes it never
    asked for.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "x=1\n")
    file_uri = path_to_resource_uri(file_path)
    fix = _make_fix(
        "quickfix_only", file_uri, _pos(0, 0), _pos(1, 0), "x=2\n", kind="quickfix"
    )

    def script(uri: ResourceUri, content: str) -> list[LintFix]:
        return [fix] if content == "x=1\n" else []

    handler = _make_handler(file_editor, script)
    result = await handler.run(
        ApplyLintFixesFilesRunPayload(file_paths=[file_uri], kinds=["source.fixAll"]),
        typing.cast(typing.Any, _RunContextStub(_META)),
    )

    assert file_editor.contents(file_path) == "x=1\n"
    assert result.applied_counts[file_uri] == 0
    assert result.status == ConvergenceStatus.CONVERGED


async def test_non_convergence_reports_the_remaining_fixes(
    tmp_path: pathlib.Path,
) -> None:
    """A linter that never stabilizes must not fail silently -- the caller
    needs to know the run gave up, and which fixes it still had queued, rather
    than getting back a result indistinguishable from a clean, fully-fixed
    file.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "Z")
    file_uri = path_to_resource_uri(file_path)

    def script(uri: ResourceUri, content: str) -> list[LintFix]:
        # An always-progressing fix (keeps the pass loop busy every pass) plus
        # two fixes that always conflict with each other -- one of the two is
        # therefore never applied, no matter how many passes run.
        progress = _make_fix(
            f"grow_{len(content)}",
            uri,
            _pos(0, len(content)),
            _pos(0, len(content)),
            "x",
        )
        stuck_a = _make_fix("stuck_a", uri, _pos(0, 0), _pos(0, 0), "A")
        stuck_b = _make_fix("stuck_b", uri, _pos(0, 0), _pos(0, 0), "B")
        return [progress, stuck_a, stuck_b]

    handler = _make_handler(file_editor, script)
    result = await handler.run(
        ApplyLintFixesFilesRunPayload(file_paths=[file_uri], max_passes=3),
        typing.cast(typing.Any, _RunContextStub(_META)),
    )

    assert result.status == ConvergenceStatus.MAX_PASSES_REACHED
    assert result.passes == 3
    assert [fix.fix_id for fix in result.remaining_fixes[file_uri]] == ["stuck_b"]


async def test_oscillation_warns_a_user_run_naming_the_oscillating_fix(
    tmp_path: pathlib.Path,
) -> None:
    """A run triggered by a person that gives up because two fixes keep undoing
    each other must tell that person which fix is responsible -- otherwise the
    run looks like a normal, complete fix from the CLI or IDE even though the
    file was left exactly as it was.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "x=1\n")
    file_uri = path_to_resource_uri(file_path)

    def script(uri: ResourceUri, content: str) -> list[LintFix]:
        if content == "x=1\n":
            return [_make_fix("flip_to_2", uri, _pos(0, 0), _pos(1, 0), "x=2\n")]
        if content == "x=2\n":
            return [_make_fix("flip_to_1", uri, _pos(0, 0), _pos(1, 0), "x=1\n")]
        return []

    user_messenger = _FakeUserMessenger()
    handler = _make_handler(file_editor, script, user_messenger=user_messenger)
    result = await handler.run(
        ApplyLintFixesFilesRunPayload(file_paths=[file_uri], max_passes=5),
        typing.cast(typing.Any, _RunContextStub(_META)),
    )

    assert result.status == ConvergenceStatus.OSCILLATED
    assert len(user_messenger.warnings) == 1
    assert "flip_to_1" in user_messenger.warnings[0]
    assert str(file_uri) in user_messenger.warnings[0]


async def test_max_passes_reached_warns_a_user_run_with_the_remaining_count(
    tmp_path: pathlib.Path,
) -> None:
    """A run triggered by a person that exhausts its pass budget without
    converging must tell that person the run was truncated -- and how many
    fixes are still queued -- rather than returning a result that looks
    identical to a fully fixed file.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "Z")
    file_uri = path_to_resource_uri(file_path)

    def script(uri: ResourceUri, content: str) -> list[LintFix]:
        progress = _make_fix(
            f"grow_{len(content)}",
            uri,
            _pos(0, len(content)),
            _pos(0, len(content)),
            "x",
        )
        stuck_a = _make_fix("stuck_a", uri, _pos(0, 0), _pos(0, 0), "A")
        stuck_b = _make_fix("stuck_b", uri, _pos(0, 0), _pos(0, 0), "B")
        return [progress, stuck_a, stuck_b]

    user_messenger = _FakeUserMessenger()
    handler = _make_handler(file_editor, script, user_messenger=user_messenger)
    result = await handler.run(
        ApplyLintFixesFilesRunPayload(file_paths=[file_uri], max_passes=3),
        typing.cast(typing.Any, _RunContextStub(_META)),
    )

    assert result.status == ConvergenceStatus.MAX_PASSES_REACHED
    assert len(user_messenger.warnings) == 1
    assert "3" in user_messenger.warnings[0]
    assert "1" in user_messenger.warnings[0]


async def test_oscillation_and_max_passes_stay_quiet_for_system_runs(
    tmp_path: pathlib.Path,
) -> None:
    """The same non-convergence cases must not prompt a person when the request
    came from the system rather than a user -- editors and other automated
    callers should not be interrupted, though the same information must still
    be discoverable in the ER logs (R-505's precedent).
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "x=1\n")
    file_uri = path_to_resource_uri(file_path)

    def script(uri: ResourceUri, content: str) -> list[LintFix]:
        if content == "x=1\n":
            return [_make_fix("flip_to_2", uri, _pos(0, 0), _pos(1, 0), "x=2\n")]
        if content == "x=2\n":
            return [_make_fix("flip_to_1", uri, _pos(0, 0), _pos(1, 0), "x=1\n")]
        return []

    user_messenger = _FakeUserMessenger()
    handler = _make_handler(file_editor, script, user_messenger=user_messenger)
    result = await handler.run(
        ApplyLintFixesFilesRunPayload(file_paths=[file_uri], max_passes=5),
        typing.cast(typing.Any, _RunContextStub(_META_SYSTEM)),
    )

    assert result.status == ConvergenceStatus.OSCILLATED
    assert user_messenger.warnings == []


async def test_converged_run_produces_no_warning(tmp_path: pathlib.Path) -> None:
    """A run that fixes everything cleanly must never surface a warning to the
    user -- a message on a successful run would train people to ignore
    warnings entirely.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "import os\nimport sys\n")
    file_uri = path_to_resource_uri(file_path)

    def script(uri: ResourceUri, content: str) -> list[LintFix]:
        if "import os" in content:
            return [_make_fix("remove_os", uri, _pos(0, 0), _pos(1, 0), "")]
        if "import sys" in content:
            return [_make_fix("remove_sys", uri, _pos(0, 0), _pos(1, 0), "")]
        return []

    user_messenger = _FakeUserMessenger()
    handler = _make_handler(file_editor, script, user_messenger=user_messenger)
    result = await handler.run(
        ApplyLintFixesFilesRunPayload(file_paths=[file_uri]),
        typing.cast(typing.Any, _RunContextStub(_META)),
    )

    assert result.status == ConvergenceStatus.CONVERGED
    assert user_messenger.warnings == []


async def test_a_last_pass_that_applies_everything_is_not_reported_as_truncated(
    tmp_path: pathlib.Path,
) -> None:
    """Running out of passes with nothing left unapplied is a successful run.

    The budget check used to fire on the last pass unconditionally, so a run
    whose final pass applied every fix it found still reported
    MAX_PASSES_REACHED -- which returns ERROR and warns the user that the run
    "exhausted the 3-pass budget without converging; 0 fix(es) remain
    unapplied". Only leftovers make an exhausted budget meaningful.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "")
    file_uri = path_to_resource_uri(file_path)

    def script(uri: ResourceUri, content: str) -> list[LintFix]:
        # One fix per pass, each revealed only by the previous one's result, so
        # the third and final pass is still productive and leaves nothing over.
        if len(content) < 3:
            return [
                _make_fix(
                    f"grow_{len(content)}",
                    uri,
                    _pos(0, len(content)),
                    _pos(0, len(content)),
                    "x",
                )
            ]
        return []

    user_messenger = _FakeUserMessenger()
    handler = _make_handler(file_editor, script, user_messenger=user_messenger)
    result = await handler.run(
        ApplyLintFixesFilesRunPayload(file_paths=[file_uri], max_passes=3),
        typing.cast(typing.Any, _RunContextStub(_META)),
    )

    assert file_editor.contents(file_path) == "xxx"
    assert result.passes == 3
    assert result.remaining_fixes == {}
    assert result.status == ConvergenceStatus.CONVERGED
    assert result.return_code == code_action.RunReturnCode.SUCCESS
    assert user_messenger.warnings == []


async def test_fixes_from_a_version_diverged_result_are_never_applied_as_a_batch(
    tmp_path: pathlib.Path,
) -> None:
    """``GetLintFixesRunResult.version_diverged`` says the contributions were
    computed against two different contents, and that such a result MUST NOT be
    used as an apply batch -- its edits are not simultaneously interpretable
    against one base version.

    The pass loop used to ignore the flag entirely and stamp the single
    result-level version onto every selection, which hid the mix from apply's
    version guard too. The fixes are held back and reported as remaining
    instead.
    """
    file_path = (tmp_path / "subject.py").resolve()
    file_editor = InMemoryFileEditor()
    file_editor.seed(file_path, "abcdef")
    file_uri = path_to_resource_uri(file_path)

    handler = _make_handler(
        file_editor,
        lambda uri, content: [_make_fix("f1", uri, _pos(0, 0), _pos(0, 1), "X")],
    )
    runner = typing.cast(typing.Any, handler.action_runner)
    inner_run_action = runner.run_action

    async def diverging_run_action(action_type, payload, meta, caller_kwargs=None):
        result = await inner_run_action(action_type, payload, meta, caller_kwargs)
        if isinstance(payload, GetLintFixesRunPayload):
            result.version_diverged = True
        return result

    runner.run_action = diverging_run_action

    result = await handler.run(
        ApplyLintFixesFilesRunPayload(file_paths=[file_uri]),
        typing.cast(typing.Any, _RunContextStub(_META)),
    )

    assert file_editor.contents(file_path) == "abcdef"
    assert result.applied_counts[file_uri] == 0
    assert [fix.fix_id for fix in result.remaining_fixes[file_uri]] == ["f1"]
