from __future__ import annotations

import types
import typing
from pathlib import Path

import pytest
from fine_format import format_file_action
from fine_format.format_file_action import (
    FormatFileRunPayload,
    FormatFileRunResult,
)
from fine_format.format_file_contract import (
    FormatFileHandlerTests,
)
from fine_python_lang.format_python_file_action import (
    FormatPythonFileAction,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces.ifileeditor import IFileEditor
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.schemas import ServiceDeclaration
from finecode_extension_runner.testing import InMemoryFileEditor, handler_test_session

from fine_python_ruff.format_python_file_handler import (
    RuffFormatFileHandler,
    RuffFormatFileHandlerConfig,
)

_ACTION_NAME = FormatPythonFileAction.__name__
_ACTION_SOURCE = (
    f"{FormatPythonFileAction.__module__}.{FormatPythonFileAction.__qualname__}"
)
_HANDLER_NAME = RuffFormatFileHandler.__name__
_HANDLER_SOURCE = (
    f"{RuffFormatFileHandler.__module__}.{RuffFormatFileHandler.__qualname__}"
)

_ACTIONS = {
    _ACTION_NAME: {
        "source": _ACTION_SOURCE,
        "handlers": [{"name": _HANDLER_NAME, "source": _HANDLER_SOURCE}],
    }
}

_LSP_SERVICE_DECLARATIONS = [
    ServiceDeclaration(
        interface="finecode_extension_api.interfaces.ijsonrpcclient.IJsonRpcClient",
        source="finecode_jsonrpc.jsonrpc_client.JsonRpcClientImpl",
    ),
    ServiceDeclaration(
        interface="finecode_extension_api.interfaces.ilspclient.ILspClient",
        source="finecode_extension_runner.impls.lsp_client.LspClientImpl",
    ),
]


def _actions_with_config(**handler_config) -> dict:
    return {
        _ACTION_NAME: {
            "source": _ACTION_SOURCE,
            "handlers": [
                {
                    "name": _HANDLER_NAME,
                    "source": _HANDLER_SOURCE,
                    "config": handler_config,
                }
            ],
        }
    }


# Adversarial inputs for the composed organize-imports-then-format behavior. The
# claim being checked is empirical, not structural: ruff's formatter reshapes an
# import block's layout without reordering it, so whether that reshaping ever
# re-triggers organize is a question about the real server, answered by running
# the handler twice on each input and checking the output stops moving.
_ROUND_TRIP_FIXTURES = {
    "moving_trailing_comment": (
        "from pathlib import Path\nimport os  # keep me\n\nprint(Path, os)\n"
    ),
    "long_from_import": (
        "from some_module import aaaaaaaa, bbbbbbbb, cccccccc, dddddddd,"
        " eeeeeeee, ffffffff, gggggggg\n"
    ),
    "magic_trailing_comma": "from mod import (\n    b,\n    a,\n)\n",
    "two_same_module_from_imports": "from mod import b\nfrom mod import a\n",
    "already_sorted": "import os\nimport sys\n\nprint(os, sys)\n",
    "noqa_i001": "import sys  # noqa: I001\nimport os\n\nprint(os, sys)\n",
    "future_import_and_leading_comment": (
        "# leading comment\nfrom __future__ import annotations\n\n"
        "import z\nimport a\n\nprint(a, z)\n"
    ),
}


class _RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def exception(self, exception: Exception) -> None: ...

    def trace(self, message: str) -> None: ...

    def info(self, message: str) -> None: ...

    def debug(self, message: str) -> None: ...

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def error(self, message: str) -> None: ...

    def disable(self, package: str) -> None: ...

    def enable(self, package: str) -> None: ...


class _StubProjectInfoProvider:
    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir

    def get_current_project_dir_path(self) -> Path:
        return self._project_dir


class _StubLspService:
    """Scripted stand-in that returns a fixed content per call, in order."""

    def __init__(self, organize_results: list[str], format_results: list[str]) -> None:
        self._organize_results = list(organize_results)
        self._format_results = list(format_results)
        self.organize_calls = 0
        self.format_calls = 0

    def add_settings_provider(self, provider: object) -> None: ...

    async def ensure_started(self, root_uri: str, meta: object) -> None: ...

    async def organize_imports(self, _file_path: Path, _content: str) -> str:
        result = self._organize_results[self.organize_calls]
        self.organize_calls += 1
        return result

    async def format_file(self, _file_path: Path, _content: str) -> str:
        result = self._format_results[self.format_calls]
        self.format_calls += 1
        return result


class TestRuffFormatFileHandler(FormatFileHandlerTests):
    handler_cls = RuffFormatFileHandler
    unformatted_snippet = "x=1\n"
    action_cls = FormatPythonFileAction
    _subject_filename = "subject.py"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _run(
        self, tmp_path: Path, snippet: str, save: bool
    ) -> tuple[FormatFileRunResult, InMemoryFileEditor]:
        return await self._run_impl(tmp_path, snippet, save, actions=_ACTIONS)

    async def _run_with_config(
        self, tmp_path: Path, snippet: str, save: bool, **handler_config
    ) -> tuple[FormatFileRunResult, InMemoryFileEditor]:
        return await self._run_impl(
            tmp_path, snippet, save, actions=_actions_with_config(**handler_config)
        )

    async def _run_impl(
        self,
        tmp_path: Path,
        snippet: str,
        save: bool,
        actions: dict,
    ) -> tuple[FormatFileRunResult, InMemoryFileEditor]:
        file_path = (tmp_path / self._subject_filename).resolve()
        file_editor = InMemoryFileEditor()
        file_editor.seed(file_path, snippet)

        async with handler_test_session(
            project_dir=tmp_path,
            actions=actions,
            service_declarations=_LSP_SERVICE_DECLARATIONS,
            service_overrides={IFileEditor: file_editor},
        ) as session:
            result = await session.run_action(
                _ACTION_NAME,
                FormatFileRunPayload(
                    file_path=path_to_resource_uri(file_path),
                    save=save,
                ),
            )
        return result, file_editor

    # ------------------------------------------------------------------
    # Override: base class calls handler_test_session directly without
    # the LSP service declarations
    # ------------------------------------------------------------------

    async def test_uses_context_file_info_not_disk(self, tmp_path: Path) -> None:
        first, _ = await self._run(tmp_path, self.unformatted_snippet, save=False)
        assert first.changed is True, "unformatted_snippet must require formatting"

        file_path = (tmp_path / self._subject_filename).resolve()
        file_path.write_text(first.code, encoding="utf-8")

        file_editor = InMemoryFileEditor()
        file_editor.seed(file_path, self.unformatted_snippet)

        async with handler_test_session(
            project_dir=tmp_path,
            actions=_ACTIONS,
            service_declarations=_LSP_SERVICE_DECLARATIONS,
            service_overrides={IFileEditor: file_editor},
        ) as session:
            result = await session.run_action(
                _ACTION_NAME,
                FormatFileRunPayload(
                    file_path=path_to_resource_uri(file_path),
                    save=False,
                ),
            )
        assert result.changed is True

    # ------------------------------------------------------------------
    # Handler-specific tests
    # ------------------------------------------------------------------

    async def test_formats_missing_spaces_around_operator(self, tmp_path: Path) -> None:
        result, _ = await self._run(tmp_path, "x=1\n", save=False)
        assert result.code == "x = 1\n"

    async def test_normalizes_single_quotes_to_double(self, tmp_path: Path) -> None:
        result, _ = await self._run(tmp_path, "x = 'hello'\n", save=False)
        assert result.changed is True
        assert result.code == 'x = "hello"\n'

    async def test_adds_missing_trailing_newline(self, tmp_path: Path) -> None:
        result, _ = await self._run(tmp_path, "x = 1", save=False)
        assert result.changed is True
        assert result.code == "x = 1\n"

    async def test_line_length_wraps_long_call(self, tmp_path: Path) -> None:
        # 48-char call that must wrap when line_length=40
        long_call = "x = my_function(first_argument, second_argument)\n"
        result, _ = await self._run_with_config(
            tmp_path, long_call, save=False, line_length=40
        )
        assert result.changed is True
        assert result.code.count("\n") > 1

    async def test_line_length_keeps_short_call(self, tmp_path: Path) -> None:
        # Same 48-char call fits in line_length=60 and is already formatted
        already_ok = "x = my_function(first_argument, second_argument)\n"
        result, _ = await self._run_with_config(
            tmp_path, already_ok, save=False, line_length=60
        )
        assert result.changed is False

    async def test_quote_style_reaches_ruff(self, tmp_path: Path) -> None:
        # `quoteStyle` is not one of ruff's client settings: sent that way it is dropped
        # in silence and the formatter normalizes to double quotes regardless of config
        result, _ = await self._run_with_config(
            tmp_path, "x = 'hello'\n", save=False, quote_style="single"
        )
        assert result.code in ("", "x = 'hello'\n")
        assert result.changed is False

    async def test_indent_width_reaches_ruff(self, tmp_path: Path) -> None:
        result, _ = await self._run_with_config(
            tmp_path, "def f():\n    return 1\n", save=False, indent_width=2
        )
        assert result.changed is True
        assert result.code == "def f():\n  return 1\n"

    async def test_unset_indent_width_leaves_the_project_config_alone(
        self, tmp_path: Path
    ) -> None:
        # editor configuration outranks the project's own, so sending a default here
        # would quietly overrule any project that chose something else
        (tmp_path / "pyproject.toml").write_text(
            "[project]\nname = 'probe'\nversion = '0'\n[tool.ruff]\nindent-width = 2\n",
            encoding="utf-8",
        )
        result, _ = await self._run(tmp_path, "def f():\n    return 1\n", save=False)
        assert result.changed is True
        assert result.code == "def f():\n  return 1\n"

    # ------------------------------------------------------------------
    # Composed organize-imports + formatter behavior
    # ------------------------------------------------------------------

    async def _round_trip(self, tmp_path: Path, snippet: str) -> tuple[str, str, bool]:
        first, _ = await self._run(tmp_path, snippet, save=False)
        once = first.code if first.changed else snippet
        second, _ = await self._run(tmp_path, once, save=False)
        twice = second.code if second.changed else once
        return once, twice, second.changed

    @pytest.mark.parametrize(
        "snippet",
        list(_ROUND_TRIP_FIXTURES.values()),
        ids=list(_ROUND_TRIP_FIXTURES),
    )
    async def test_composed_round_trip_reaches_a_fixed_point(
        self, tmp_path: Path, snippet: str
    ) -> None:
        """A second format must leave the first result byte-for-byte unchanged.

        Import organization and formatting are two rewrites of the same lines, so
        an input they disagree about would flip back and forth on every save — the
        file changes, and changes again back, forever. The test is the evidence for
        the claim; a structural argument about the two tools agreeing is not, since
        neither is versioned against the other.
        """
        once, twice, second_changed = await self._round_trip(tmp_path, snippet)

        assert second_changed is False
        assert twice == once

    async def test_sorts_unsorted_imports_in_one_call(self, tmp_path: Path) -> None:
        result, _ = await self._run(
            tmp_path,
            "from pathlib import Path\nimport os\n\nprint(Path, os)\n",
            save=False,
        )
        assert result.changed is True
        assert result.code == "import os\nfrom pathlib import Path\n\nprint(Path, os)\n"

    async def test_moving_trailing_comment_survives_reordering(
        self, tmp_path: Path
    ) -> None:
        result, _ = await self._run(
            tmp_path, _ROUND_TRIP_FIXTURES["moving_trailing_comment"], save=False
        )
        assert result.code == (
            "import os  # keep me\nfrom pathlib import Path\n\nprint(Path, os)\n"
        )

    async def test_future_import_stays_first_after_a_leading_comment(
        self, tmp_path: Path
    ) -> None:
        result, _ = await self._run(
            tmp_path,
            _ROUND_TRIP_FIXTURES["future_import_and_leading_comment"],
            save=False,
        )
        lines = result.code.splitlines()
        assert lines[0] == "# leading comment"
        assert lines[1] == "from __future__ import annotations"

    async def test_noqa_i001_suppression_leaves_the_block_alone(
        self, tmp_path: Path
    ) -> None:
        result, _ = await self._run(
            tmp_path, _ROUND_TRIP_FIXTURES["noqa_i001"], save=False
        )
        assert result.changed is False

    async def test_already_sorted_imports_are_unchanged(self, tmp_path: Path) -> None:
        result, _ = await self._run(
            tmp_path, _ROUND_TRIP_FIXTURES["already_sorted"], save=False
        )
        assert result.changed is False

    # ------------------------------------------------------------------
    # Convergence guard (no real-server fixture reaches it)
    # ------------------------------------------------------------------

    async def _run_with_stub(
        self,
        tmp_path: Path,
        snippet: str,
        organize_results: list[str],
        format_results: list[str],
    ) -> tuple[FormatFileRunResult, _StubLspService, _RecordingLogger]:
        lsp_service = _StubLspService(organize_results, format_results)
        logger = _RecordingLogger()
        handler = RuffFormatFileHandler(
            config=RuffFormatFileHandlerConfig(),
            logger=logger,
            project_info_provider=typing.cast(
                typing.Any, _StubProjectInfoProvider(tmp_path)
            ),
            action_runner=typing.cast(typing.Any, object()),
            lsp_service=typing.cast(typing.Any, lsp_service),
        )
        run_context = types.SimpleNamespace(
            meta=code_action.RunActionMeta(
                trigger=code_action.RunActionTrigger.USER,
                dev_env=code_action.DevEnv.CLI,
            ),
            file_info=format_file_action.FileInfo(snippet, "v1"),
        )
        result = await handler.run(
            FormatFileRunPayload(
                file_path=path_to_resource_uri((tmp_path / "subject.py").resolve()),
                save=False,
            ),
            typing.cast(typing.Any, run_context),
        )
        return result, lsp_service, logger

    async def test_guard_falls_back_to_format_only_when_reorganize_disagrees(
        self, tmp_path: Path
    ) -> None:
        """A file the two steps disagree about must not report the disputed content.

        Returning the organized-then-formatted result would hand the caller content
        that changes again on the next run, so a format-on-save loop would keep
        rewriting it. The format-only result is the stable fixed point instead.
        """
        result, lsp_service, logger = await self._run_with_stub(
            tmp_path,
            "ORIGINAL",
            organize_results=["ORGANIZED", "DIFFERENT"],
            format_results=["FORMATTED", "FORMAT_ONLY"],
        )

        assert result.code == "FORMAT_ONLY"
        assert result.changed is True
        assert lsp_service.organize_calls == 2
        assert lsp_service.format_calls == 2
        assert logger.warnings, "non-convergence must be logged"
        assert "does not converge" in logger.warnings[0]

    async def test_guard_accepts_a_converged_composition(self, tmp_path: Path) -> None:
        result, lsp_service, logger = await self._run_with_stub(
            tmp_path,
            "ORIGINAL",
            organize_results=["ORGANIZED", "FORMATTED"],
            format_results=["FORMATTED"],
        )

        assert result.code == "FORMATTED"
        assert lsp_service.organize_calls == 2
        assert lsp_service.format_calls == 1
        assert logger.warnings == []

    async def test_guard_is_skipped_when_neither_step_changes_anything(
        self, tmp_path: Path
    ) -> None:
        """A clean file must cost two LSP calls, not the guard's third."""
        result, lsp_service, logger = await self._run_with_stub(
            tmp_path,
            "CLEAN",
            organize_results=["CLEAN"],
            format_results=["CLEAN"],
        )

        assert result.changed is False
        assert lsp_service.organize_calls == 1
        assert lsp_service.format_calls == 1
        assert logger.warnings == []
