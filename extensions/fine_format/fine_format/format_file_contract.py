"""Contract and handler implementation test base classes for format_file handlers.

Two base classes are provided, covering two distinct layers of requirements:

``FormatFileContractTests``
    Action contract — observable guarantees to callers (result semantics,
    idempotency, no disk write when ``save=False``).

``FormatFileHandlerTests``
    Handler implementation contract — pipeline participation rules (reads from
    context, never writes to source file).  Inherits all action contract tests,
    so subclassing it alone is sufficient for content-transformation handlers.

Typical usage in a handler package::

    from fine_format.format_file_contract import (
        FormatFileHandlerTests,
    )

    class TestMyHandler(FormatFileHandlerTests):
        handler_cls = MyFormatHandler
        unformatted_snippet = "x=1\\n"
        _subject_filename = "subject.py"   # required if tool is extension-sensitive
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from finecode_extension_api.resource_uri import path_to_resource_uri

from fine_format.format_file_action import (
    FormatFileAction,
    FormatFileRunPayload,
    FormatFileRunResult,
)


class FormatFileContractTests:
    """Pytest base class for format_file handler contract compliance.

    Subclasses must set:

    ``handler_cls``
        The handler class to test.
    ``unformatted_snippet``
        Source code that the handler is guaranteed to modify.

    Optionally override:

    ``action_cls``
        Defaults to ``FormatFileAction``. Set to a language-specific subaction
        (e.g. ``FormatPythonFileAction``) when testing a language handler.
    ``_subject_filename``
        Name of the temporary file fed to the handler. Override when the tool
        is extension-sensitive (e.g. ``"subject.py"`` for a Python formatter).

    Handler-specific tests (exact output, config behaviour, tool error paths)
    belong in the same subclass alongside the inherited contract tests.
    """

    handler_cls: ClassVar[type]
    unformatted_snippet: ClassVar[str]
    action_cls: ClassVar[type] = FormatFileAction
    _subject_filename: ClassVar[str] = "subject"

    # -- setup helper --

    async def _run(
        self, tmp_path: Path, snippet: str, save: bool
    ) -> tuple[FormatFileRunResult, object]:
        from finecode_extension_api.interfaces.ifileeditor import IFileEditor
        from finecode_extension_runner.testing import InMemoryFileEditor, run_handler

        file_path = (tmp_path / self._subject_filename).resolve()
        file_editor = InMemoryFileEditor()
        file_editor.seed(file_path, snippet)

        result = await run_handler(
            self.handler_cls,
            FormatFileRunPayload(
                file_path=path_to_resource_uri(file_path),
                save=save,
            ),
            action_cls=self.action_cls,
            project_dir=tmp_path,
            service_overrides={IFileEditor: file_editor},
        )
        return result, file_editor

    # -- contract tests --

    async def test_changed_true_when_input_needs_formatting(
        self, tmp_path: Path
    ) -> None:
        result, _ = await self._run(tmp_path, self.unformatted_snippet, save=False)
        assert result.changed is True

    async def test_code_nonempty_and_differs_from_input_when_changed(
        self, tmp_path: Path
    ) -> None:
        result, _ = await self._run(tmp_path, self.unformatted_snippet, save=False)
        assert result.changed is True, "unformatted_snippet must require formatting"
        assert result.code != ""
        assert result.code != self.unformatted_snippet

    async def test_idempotent_returns_not_changed(self, tmp_path: Path) -> None:
        first, _ = await self._run(tmp_path, self.unformatted_snippet, save=False)
        assert first.changed is True, "unformatted_snippet must require formatting"
        second, _ = await self._run(tmp_path, first.code, save=False)
        assert second.changed is False

    async def test_code_empty_when_not_changed(self, tmp_path: Path) -> None:
        first, _ = await self._run(tmp_path, self.unformatted_snippet, save=False)
        assert first.changed is True, "unformatted_snippet must require formatting"
        second, _ = await self._run(tmp_path, first.code, save=False)
        assert second.code == ""

    async def test_no_disk_write_when_save_false(self, tmp_path: Path) -> None:
        result, file_editor = await self._run(
            tmp_path, self.unformatted_snippet, save=False
        )
        assert result.changed is True, "unformatted_snippet must require formatting"
        assert file_editor.writes == []


class FormatFileHandlerTests(FormatFileContractTests):
    """Pytest base class for format_file handler implementation compliance.

    Extends ``FormatFileContractTests`` with tests for the pipeline
    participation rules: a handler must read from ``run_context.file_info`` and
    must never write to the source file.

    Inherits all action contract tests, so this is the only base class needed
    for content-transformation handlers.
    """

    async def test_uses_context_file_info_not_disk(self, tmp_path: Path) -> None:
        # Get formatted content so we know what "already formatted" looks like.
        first, _ = await self._run(tmp_path, self.unformatted_snippet, save=False)
        assert first.changed is True, "unformatted_snippet must require formatting"

        # Write already-formatted content to the real file on disk, but give
        # InMemoryFileEditor the unformatted snippet. The handler must process
        # what is in run_context.file_info (context), not what is on disk.
        # If it reads from disk it sees formatted content → changed=False.
        # If it reads from context it sees unformatted content → changed=True.
        from finecode_extension_api.interfaces.ifileeditor import IFileEditor
        from finecode_extension_runner.testing import InMemoryFileEditor, run_handler

        file_path = (tmp_path / self._subject_filename).resolve()
        file_path.write_text(first.code, encoding="utf-8")

        file_editor = InMemoryFileEditor()
        file_editor.seed(file_path, self.unformatted_snippet)

        result = await run_handler(
            self.handler_cls,
            FormatFileRunPayload(
                file_path=path_to_resource_uri(file_path),
                save=False,
            ),
            action_cls=self.action_cls,
            project_dir=tmp_path,
            service_overrides={IFileEditor: file_editor},
        )
        assert result.changed is True

    async def test_never_writes_to_source_file(self, tmp_path: Path) -> None:
        # A content-transformation handler must never write to the source file.
        # Saving is SaveFormatFileHandler's job. Test with save=True to confirm
        # the handler ignores the flag entirely.
        result, file_editor = await self._run(
            tmp_path, self.unformatted_snippet, save=True
        )
        assert result.changed is True, "unformatted_snippet must require formatting"
        assert file_editor.writes == []
        # Also verify no direct filesystem write bypassing IFileEditor.
        file_path = (tmp_path / self._subject_filename).resolve()
        assert not file_path.exists()
