import collections.abc
import contextlib
import dataclasses
import importlib
import io
import os
import pathlib
import sys
from typing import Any, cast

from fine_check_imports.check_imports_action import (
    CheckImportsRunContext,
    CheckImportsRunPayload,
    CheckImportsRunResult,
)
from fine_inspect_code.diagnostic_types import (
    Diagnostic,
    DiagnosticSeverity,
    Position,
    Range,
)
from fine_python_lang.check_python_imports_action import CheckPythonImportsAction
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ilogger, iprocessexecutor
from finecode_extension_api.resource_uri import (
    ResourceUri,
    path_to_resource_uri,
    resource_uri_to_path,
)

@contextlib.contextmanager
def _chdir(path: pathlib.Path) -> collections.abc.Iterator[None]:
    """Temporarily change the process working directory.

    Equivalent to contextlib.chdir (3.13+), reimplemented here because this
    extension supports Python 3.11+.
    """
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


@contextlib.contextmanager
def _sys_path_entry(path: pathlib.Path) -> collections.abc.Iterator[None]:
    """Temporarily add ``path`` to sys.path.

    grimp resolves root_packages via ``importlib.util.find_spec``, which consults
    sys.path — not the process's cwd. ``_chdir`` alone does not make a project's
    top-level packages importable; this must be paired with it.
    """
    path_str = str(path)
    sys.path.insert(0, path_str)
    try:
        yield
    finally:
        sys.path.remove(path_str)


# import-linter's built-in contract types (mirrors importlinter.application.use_cases
# ._get_built_in_contract_types — that helper is private, so the small registration
# list is replicated here rather than imported).
_BUILT_IN_CONTRACT_TYPES = [
    "forbidden: importlinter.contracts.forbidden.ForbiddenContract",
    "layers: importlinter.contracts.layers.LayersContract",
    "independence: importlinter.contracts.independence.IndependenceContract",
    "protected: importlinter.contracts.protected.ProtectedContract",
    "acyclic_siblings: importlinter.contracts.acyclic_siblings.AcyclicSiblingsContract",
]


@dataclasses.dataclass
class ImportLinterContractConfig:
    type: str
    """Contract type, e.g. 'layers', 'forbidden', 'independence', 'protected',
    'acyclic_siblings', or a custom type registered via import-linter plugins."""
    name: str
    options: dict[str, Any] = dataclasses.field(default_factory=dict)
    """Contract-type-specific fields, passed through verbatim (e.g. 'layers'/
    'containers' for a layers contract, 'source_modules'/'forbidden_modules' for
    a forbidden contract). Mirrors the fields import-linter itself expects in a
    [[tool.importlinter.contracts]] TOML entry."""


@dataclasses.dataclass
class ImportLinterCheckPythonImportsHandlerConfig(code_action.ActionHandlerConfig):
    config_filename: str | None = None
    """Explicit import-linter config file name (e.g. 'pyproject.toml', '.importlinter').
    None = import-linter's own discovery order (pyproject.toml, setup.cfg, .importlinter)
    in the project directory. Ignored when `contracts` is non-empty."""

    root_packages: list[str] = dataclasses.field(default_factory=list)
    include_external_packages: bool = False
    exclude_type_checking_imports: bool = False
    contracts: list[ImportLinterContractConfig] = dataclasses.field(default_factory=list)
    """Contracts defined directly in FineCode config (e.g. shared by a preset across
    projects) instead of a separate import-linter config file. When non-empty, this
    takes precedence over `config_filename`/file discovery entirely."""


def _register_contract_types(user_options) -> None:
    from importlinter.domain.contract import registry

    plugin_entries = list(user_options.session_options.get("contract_types", []))
    for entry in _BUILT_IN_CONTRACT_TYPES + plugin_entries:
        type_name, class_path = entry.split(": ")
        module_name, class_name = class_path.rsplit(".", 1)
        module = importlib.import_module(module_name)
        contract_class = getattr(module, class_name)
        registry.register(contract_class, type_name)


def _render_violation_text(contract, check) -> str:
    # Reuse the contract's own render_broken_contract — every built-in and
    # custom contract type already implements it correctly, so this avoids
    # writing a bespoke metadata parser per contract type. import-linter's
    # output goes through a module-level rich.Console singleton; redirect
    # it to an in-memory buffer for the duration of this call only.
    from importlinter.application import output as il_output
    from rich.console import Console

    buffer = io.StringIO()
    original_console = il_output.console
    il_output.console = Console(file=buffer, highlight=False, width=100)
    try:
        contract.render_broken_contract(check)
    finally:
        il_output.console = original_console
    return buffer.getvalue().strip()


def _stringify_booleans(options: dict[str, Any]) -> dict[str, Any]:
    # import-linter's own config readers (TomlFileUserOptionReader) stringify
    # booleans before handing them to UserOptions, because contract fields
    # (e.g. BooleanField) are written to parse the same "True"/"False" strings
    # that come out of INI files. Mirror that here so inline TOML booleans
    # (parsed as real bool by cattrs) are understood the same way.
    return {
        key: (str(value) if isinstance(value, bool) else value)
        for key, value in options.items()
    }


def _build_user_options_from_config(config: ImportLinterCheckPythonImportsHandlerConfig):
    from importlinter.application.user_options import UserOptions

    session_options = _stringify_booleans(
        {
            "root_packages": list(config.root_packages),
            "include_external_packages": config.include_external_packages,
            "exclude_type_checking_imports": config.exclude_type_checking_imports,
        }
    )
    contracts_options = [
        _stringify_booleans({"type": contract.type, "name": contract.name, **contract.options})
        for contract in config.contracts
    ]
    return UserOptions(session_options=session_options, contracts_options=contracts_options)


@dataclasses.dataclass
class _ImportLinterCheckOutcome:
    messages: dict[ResourceUri, list[Diagnostic]]
    config_found: bool
    kept_count: int = 0
    broken_count: int = 0
    warnings_count: int = 0


def _run_import_linter_check(
    config_uri: ResourceUri,
    project_dir: pathlib.Path,
    config: ImportLinterCheckPythonImportsHandlerConfig,
) -> _ImportLinterCheckOutcome:
    # Runs in a worker process (see ImportLinterCheckPythonImportsHandler.run):
    # import-linter's graph-building can be slow on large codebases, and this
    # keeps that work off the ER's event loop.
    from importlinter import configuration as il_configuration
    from importlinter.application.use_cases import read_user_options, create_report

    # import-linter's global `settings` (timer, user-option readers, graph
    # builder, ...) is only populated as a side effect of importing
    # `importlinter.cli`, which this handler never imports. Configure it
    # explicitly instead — cheap, and idempotent across repeated submissions
    # to the same pooled worker process.
    il_configuration.configure()

    # import-linter has no "project root" parameter of its own: config
    # discovery depends on the process's current working directory, and
    # grimp's module import depends on sys.path. Scope both to this call only.
    with _chdir(project_dir), _sys_path_entry(project_dir):
        if config.contracts:
            # Contracts are defined inline (e.g. shared via a preset), so no
            # [tool.importlinter]/.importlinter file is needed at all.
            user_options = _build_user_options_from_config(config)
        else:
            try:
                user_options = read_user_options(config_filename=config.config_filename)
            except FileNotFoundError:
                return _ImportLinterCheckOutcome(messages={}, config_found=False)

        _register_contract_types(user_options)
        report = create_report(user_options)

        if report.could_not_run:
            reasons = "; ".join(
                f"{name}: {exc}" for name, exc in report.invalid_contract_options.items()
            )
            raise code_action.ActionFailedException(
                f"import-linter: invalid contract configuration — {reasons}"
            )

        messages: dict[ResourceUri, list[Diagnostic]] = {}
        for contract, check in report.get_contracts_and_checks():
            if check.kept:
                continue
            text = _render_violation_text(contract, check)
            messages.setdefault(config_uri, []).append(
                Diagnostic(
                    range=Range(
                        start=Position(line=0, character=0),
                        end=Position(line=0, character=0),
                    ),
                    message=text,
                    source="import-linter",
                    code=contract.name,
                    severity=DiagnosticSeverity.ERROR,
                )
            )

        if not messages:
            # Config was found and evaluated but every contract was kept — record an
            # empty entry so DiagnosticFilesRunResult.to_text() renders "path: OK"
            # instead of silently omitting the project (indistinguishable from "not
            # checked at all").
            messages[config_uri] = []

        return _ImportLinterCheckOutcome(
            messages=messages,
            config_found=True,
            kept_count=report.kept_count,
            broken_count=report.broken_count,
            warnings_count=report.warnings_count,
        )


class ImportLinterCheckPythonImportsHandler(
    code_action.ActionHandler[
        CheckPythonImportsAction, ImportLinterCheckPythonImportsHandlerConfig
    ]
):
    """Run import-linter against a project's configured architectural contracts.

    A project with no ``[tool.importlinter]`` (or ``.importlinter``/``setup.cfg``)
    configuration is a no-op — import-linter is opt-in per project, so this must
    not be treated as an error.
    """

    def __init__(
        self,
        config: ImportLinterCheckPythonImportsHandlerConfig,
        logger: ilogger.ILogger,
        process_executor: iprocessexecutor.IProcessExecutor,
    ) -> None:
        self.config = config
        self.logger = logger
        self.process_executor = process_executor

    async def run(
        self,
        payload: CheckImportsRunPayload,
        run_context: CheckImportsRunContext,
    ) -> CheckImportsRunResult:
        if payload.src_artifact_def_path is None:
            raise code_action.ActionFailedException(
                "ImportLinterCheckPythonImportsHandler requires a resolved "
                "src_artifact_def_path (the dispatch handler must resolve it first)"
            )

        project_def_path = pathlib.Path(resource_uri_to_path(payload.src_artifact_def_path))
        project_dir = project_def_path.parent
        config_uri = path_to_resource_uri(project_def_path)

        outcome = cast(
            _ImportLinterCheckOutcome,
            await self.process_executor.submit(
                _run_import_linter_check, config_uri, project_dir, self.config
            ),
        )

        if not outcome.config_found:
            self.logger.debug(
                "import-linter: no [tool.importlinter]/.importlinter configuration "
                "found in project — skipping."
            )
            return CheckImportsRunResult(messages={})

        self.logger.debug(
            f"import-linter: kept={outcome.kept_count} broken={outcome.broken_count} "
            f"warnings={outcome.warnings_count}"
        )

        if not outcome.messages and outcome.broken_count:
            self.logger.warning(
                "import-linter: report has broken contracts but no diagnostics were "
                f"produced (broken_count={outcome.broken_count})"
            )
        return CheckImportsRunResult(messages=outcome.messages)
