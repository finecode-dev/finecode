from __future__ import annotations

import pathlib
from typing import cast

import pytest
from fine_check_imports.check_imports_action import (
    CheckImportsRunPayload,
    CheckImportsRunResult,
)
from fine_inspect_code.diagnostic_types import DiagnosticSeverity, Position, Range
from fine_python_lang.check_python_imports_action import CheckPythonImportsAction
from finecode_extension_api.resource_uri import path_to_resource_uri
from finecode_extension_runner._services import run_action as run_action_service
from finecode_extension_runner.testing import run_handler

from fine_python_import_linter.check_python_imports_handler import (
    ImportLinterCheckPythonImportsHandler,
)

pytestmark = pytest.mark.anyio


def _write_files(base: pathlib.Path, files: dict[str, str]) -> None:
    for rel_path, content in files.items():
        p = base / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def _config_uri(project_dir: pathlib.Path):
    return path_to_resource_uri(project_dir / "pyproject.toml")


async def _run(tmp_path, handler_config=None, payload=None) -> CheckImportsRunResult:
    result = await run_handler(
        ImportLinterCheckPythonImportsHandler,
        payload
        if payload is not None
        else CheckImportsRunPayload(src_artifact_def_path=_config_uri(tmp_path)),
        action_cls=CheckPythonImportsAction,
        project_dir=tmp_path,
        handler_config=handler_config or {},
    )
    return cast(CheckImportsRunResult, result)


async def test_forbidden_contract_reports_broken_violation(
    tmp_path: pathlib.Path,
) -> None:
    _write_files(
        tmp_path,
        {
            "myapp/__init__.py": "",
            "myapp/high.py": "import myapp.low\n",
            "myapp/low.py": "",
        },
    )
    handler_config = {
        "root_packages": ["myapp"],
        "contracts": [
            {
                "type": "forbidden",
                "name": "no_high_importing_low",
                "options": {
                    "source_modules": ["myapp.high"],
                    "forbidden_modules": ["myapp.low"],
                },
            }
        ],
    }

    result = await _run(tmp_path, handler_config=handler_config)

    diagnostics = result.messages[_config_uri(tmp_path)]
    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic.source == "import-linter"
    assert diagnostic.code == "no_high_importing_low"
    assert diagnostic.severity == DiagnosticSeverity.ERROR
    assert diagnostic.range == Range(start=Position(0, 0), end=Position(0, 0))
    assert "myapp.high" in diagnostic.message
    assert "myapp.low" in diagnostic.message


async def test_forbidden_contract_kept_no_violation(tmp_path: pathlib.Path) -> None:
    _write_files(
        tmp_path,
        {
            "myapp/__init__.py": "",
            "myapp/high.py": "",
            "myapp/low.py": "",
        },
    )
    handler_config = {
        "root_packages": ["myapp"],
        "contracts": [
            {
                "type": "forbidden",
                "name": "no_high_importing_low",
                "options": {
                    "source_modules": ["myapp.high"],
                    "forbidden_modules": ["myapp.low"],
                },
            }
        ],
    }

    result = await _run(tmp_path, handler_config=handler_config)

    assert result.messages == {_config_uri(tmp_path): []}


async def test_layers_contract_with_boolean_exhaustive_field(
    tmp_path: pathlib.Path,
) -> None:
    _write_files(
        tmp_path,
        {
            "myapp/__init__.py": "",
            "myapp/high/__init__.py": "",
            "myapp/low/__init__.py": "import myapp.high\n",
        },
    )
    handler_config = {
        "root_packages": ["myapp"],
        "contracts": [
            {
                "type": "layers",
                "name": "myapp_layers",
                "options": {
                    "containers": ["myapp"],
                    "layers": ["high", "low"],
                    "exhaustive": True,
                },
            }
        ],
    }

    result = await _run(tmp_path, handler_config=handler_config)

    diagnostics = result.messages[_config_uri(tmp_path)]
    assert len(diagnostics) == 1
    assert diagnostics[0].code == "myapp_layers"
    assert diagnostics[0].source == "import-linter"


async def test_inline_contracts_take_precedence_over_config_file(
    tmp_path: pathlib.Path,
) -> None:
    _write_files(
        tmp_path,
        {
            "myapp/__init__.py": "",
            "myapp/a.py": "import myapp.b\n",
            "myapp/b.py": "",
            "pyproject.toml": (
                "[tool.importlinter]\n"
                'root_packages = ["myapp"]\n'
                "\n"
                "[[tool.importlinter.contracts]]\n"
                'name = "file_contract"\n'
                'type = "forbidden"\n'
                'source_modules = ["myapp.a"]\n'
                'forbidden_modules = ["myapp.b"]\n'
            ),
        },
    )
    # The on-disk pyproject.toml contract above is BROKEN (a imports b). The
    # inline contract below forbids the opposite (unbroken) direction, so if
    # inline contracts correctly take precedence, there must be no violation.
    handler_config = {
        "root_packages": ["myapp"],
        "contracts": [
            {
                "type": "forbidden",
                "name": "inline_contract",
                "options": {
                    "source_modules": ["myapp.b"],
                    "forbidden_modules": ["myapp.a"],
                },
            }
        ],
    }

    result = await _run(tmp_path, handler_config=handler_config)

    assert result.messages == {_config_uri(tmp_path): []}


async def test_file_based_discovery_fallback_when_no_inline_contracts(
    tmp_path: pathlib.Path,
) -> None:
    _write_files(
        tmp_path,
        {
            "myapp/__init__.py": "",
            "myapp/a.py": "import myapp.b\n",
            "myapp/b.py": "",
            "pyproject.toml": (
                "[tool.importlinter]\n"
                'root_packages = ["myapp"]\n'
                "\n"
                "[[tool.importlinter.contracts]]\n"
                'name = "file_contract"\n'
                'type = "forbidden"\n'
                'source_modules = ["myapp.a"]\n'
                'forbidden_modules = ["myapp.b"]\n'
            ),
        },
    )

    result = await _run(tmp_path, handler_config={})

    diagnostics = result.messages[_config_uri(tmp_path)]
    assert len(diagnostics) == 1
    assert diagnostics[0].code == "file_contract"
    assert diagnostics[0].source == "import-linter"


async def test_no_config_no_op(tmp_path: pathlib.Path) -> None:
    _write_files(tmp_path, {"myapp/__init__.py": ""})

    result = await _run(tmp_path, handler_config={})

    assert result.messages == {}


async def test_missing_src_artifact_def_path_raises(tmp_path: pathlib.Path) -> None:
    payload = CheckImportsRunPayload(src_artifact_def_path=None)

    # CheckPythonImportsAction runs its handlers concurrently, and concurrent-mode
    # failures are reported with a generic message (the specific handler error
    # goes to ER logs, not into the exception text) — see the raise site in
    # finecode_extension_runner/_services/run_action.py for the concurrent path.
    with pytest.raises(run_action_service.ActionFailedException) as exc_info:
        await _run(tmp_path, handler_config={}, payload=payload)

    assert "CheckPythonImportsAction" in exc_info.value.message
    assert "failed" in exc_info.value.message


async def test_invalid_contract_config_raises(tmp_path: pathlib.Path) -> None:
    _write_files(
        tmp_path,
        {
            "myapp/__init__.py": "",
            "myapp/high/__init__.py": "",
            "myapp/low/__init__.py": "",
        },
    )
    # `exhaustive` requires `containers` — deliberately omitted here.
    handler_config = {
        "root_packages": ["myapp"],
        "contracts": [
            {
                "type": "layers",
                "name": "bad_layers",
                "options": {
                    "layers": ["high", "low"],
                    "exhaustive": True,
                },
            }
        ],
    }

    # See the comment in test_missing_src_artifact_def_path_raises: concurrent-mode
    # failures carry a generic message, not the handler's specific error text.
    with pytest.raises(run_action_service.ActionFailedException) as exc_info:
        await _run(tmp_path, handler_config=handler_config)

    assert "CheckPythonImportsAction" in exc_info.value.message
    assert "failed" in exc_info.value.message
