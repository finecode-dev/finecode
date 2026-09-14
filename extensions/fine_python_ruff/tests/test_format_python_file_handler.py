from __future__ import annotations

from pathlib import Path

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
from finecode_extension_api.interfaces.ifileeditor import IFileEditor
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner.schemas import ServiceDeclaration
from finecode_extension_runner.testing import InMemoryFileEditor, handler_test_session

from fine_python_ruff.format_python_file_handler import RuffFormatFileHandler

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
