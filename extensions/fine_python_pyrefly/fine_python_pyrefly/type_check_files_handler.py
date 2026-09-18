from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

from fine_python_lang.type_check_python_files_action import TypeCheckPythonFilesAction
from fine_type_check.diagnostic_types import (
    Diagnostic,
    DiagnosticFilesRunContext,
    DiagnosticFilesRunPayload,
    DiagnosticFilesRunResult,
    DiagnosticSeverity,
    Position,
    Range,
)
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    icommandrunner,
    iextensionrunnerinfoprovider,
    ilogger,
    iprojectinfoprovider,
    isrcartifactfileclassifier,
)
from finecode_extension_api.resource_uri import ResourceUri, resource_uri_to_path

from fine_python_pyrefly.pyrefly_lsp_service import PyreflyLspService


@dataclasses.dataclass
class PyreflyTypeCheckFilesHandlerConfig(code_action.ActionHandlerConfig):
    python_version: str | None = None
    use_cli: bool = False
    # How long a run waits for the LSP server's watched-file recheck to land
    # when no run document is open (DEC-11). An upper bound on the recheck of
    # even a heavily imported module; with an open run document the wait ends
    # at the recheck itself.
    recheck_barrier_sec: float = 2.0


class PyreflyTypeCheckFilesHandler(
    code_action.ActionHandler[
        TypeCheckPythonFilesAction, PyreflyTypeCheckFilesHandlerConfig
    ]
):
    """
    NOTE: pyrefly currently can check only saved files, not file content provided by
    FineCode. In environments like IDE, messages from pyrefly will be updated only after
    save of a file.
    """

    def __init__(
        self,
        config: PyreflyTypeCheckFilesHandlerConfig,
        logger: ilogger.ILogger,
        command_runner: icommandrunner.ICommandRunner,
        src_artifact_file_classifier: isrcartifactfileclassifier.ISrcArtifactFileClassifier,
        extension_runner_info_provider: iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        lsp_service: PyreflyLspService,
    ) -> None:
        self.config = config
        self.logger = logger
        self.command_runner = command_runner
        self.src_artifact_file_classifier = src_artifact_file_classifier
        self.extension_runner_info_provider = extension_runner_info_provider
        self.project_info_provider: iprojectinfoprovider.IProjectInfoProvider = (
            project_info_provider
        )
        self.lsp_service: PyreflyLspService = lsp_service

        self.pyrefly_bin_path = Path(sys.executable).parent / "pyrefly"

        if not self.config.use_cli:
            # Pyrefly uses pull-based config: the LSP server sends
            # workspace/configuration requests with section="python",
            # expecting responses like [{"pyrefly": {"displayTypeErrors": ...}}].
            # The same format is used for initializationOptions.
            # pythonPath/extraPaths are already set up by PyreflyLspService itself;
            # only add the type-check-specific setting here.
            self.lsp_service.update_settings(
                {
                    "pyrefly": {"displayTypeErrors": "force-on"},
                }
            )

    async def run_on_single_file(
        self, file_uri: ResourceUri
    ) -> DiagnosticFilesRunResult:
        file_path = resource_uri_to_path(file_uri)
        if self.config.use_cli:
            type_check_messages = await self.run_pyrefly_type_check_on_single_file(
                file_path
            )
        else:
            root_uri = (
                self.project_info_provider.get_current_project_dir_path().as_uri()
            )
            await self.lsp_service.ensure_started(root_uri)

            type_check_messages = await self.lsp_service.check_file(file_path)

        return DiagnosticFilesRunResult(messages={file_uri: type_check_messages})

    async def run(
        self,
        payload: DiagnosticFilesRunPayload,
        run_context: DiagnosticFilesRunContext,
    ) -> None:
        file_uris = [file_uri async for file_uri in payload]

        if not self.config.use_cli:
            # Order is load-bearing: the sweep must send its watched-file
            # notification and wait for the recheck *before* any per-file sync
            # goes out, or the first sync races the recheck and is answered
            # from the pre-recheck state.
            root_uri = (
                self.project_info_provider.get_current_project_dir_path().as_uri()
            )
            await self.lsp_service.ensure_started(root_uri)
            missing = await self.lsp_service.sync_watched_files(
                [resource_uri_to_path(u) for u in file_uris],
                recheck_timeout=self.config.recheck_barrier_sec,
            )
            # A missing path would raise FileNotFound inside check_file and
            # cancel the whole run (F42); drop it instead.
            file_uris = [u for u in file_uris if resource_uri_to_path(u) not in missing]
        for file_uri in file_uris:
            run_context.partial_result_scheduler.schedule(
                file_uri,
                self.run_on_single_file(file_uri),
            )

    async def run_pyrefly_type_check_on_single_file(
        self,
        file_path: Path,
    ) -> list[Diagnostic]:
        """Run pyrefly type checking on a single file"""
        type_check_messages: list[Diagnostic] = []

        try:
            # src artifact file classifier caches result, we can just get it each time again
            file_type = self.src_artifact_file_classifier.get_src_artifact_file_type(
                file_path=file_path
            )
            file_env = self.src_artifact_file_classifier.get_env_for_file_type(
                file_type=file_type
            )
        except NotImplementedError:
            self.logger.warning(
                f"Skip {file_path} because file type or env for it could be determined"
            )
            return type_check_messages

        venv_dir_path = self.extension_runner_info_provider.get_venv_dir_path_of_env(
            env_name=file_env
        )
        site_package_pathes = (
            self.extension_runner_info_provider.get_venv_site_packages(
                venv_dir_path=venv_dir_path
            )
        )
        interpreter_path = (
            self.extension_runner_info_provider.get_venv_python_interpreter(
                venv_dir_path=venv_dir_path
            )
        )

        # --skip-interpreter-query isn't used because it is not compatible
        # with --python-interpreter-path parameter
        # --disable-search-path-heuristics=true isn't used because pyrefly doesn't
        # recognize some imports without it. For example, it cannot resolve relative
        # imports in root __init__.py . Needs to be investigated
        cmd = [
            str(self.pyrefly_bin_path),
            "check",
            "--output-format=json",
            # path to python interpreter because pyrefly resolves .pth files only if
            # it is provided
            f"--python-interpreter-path='{interpreter_path!s}'",
        ]

        if self.config.python_version is not None:
            cmd.append(f"--python-version='{self.config.python_version}'")

        for path in site_package_pathes:
            cmd.append(f"--site-package-path={path!s}")
        cmd.append(str(file_path))

        cmd_str = " ".join(cmd)
        pyrefly_process = await self.command_runner.run(cmd_str)

        await pyrefly_process.wait_for_end()

        output = pyrefly_process.get_output()
        try:
            pyrefly_results = json.loads(output)
            for error in pyrefly_results["errors"]:
                type_check_messages.append(map_pyrefly_error_to_diagnostic(error))
        except json.JSONDecodeError as exception:
            raise code_action.ActionFailedException(
                f"Output of pyrefly is not json: {output}"
            ) from exception

        return type_check_messages


def map_pyrefly_error_to_diagnostic(error: dict) -> Diagnostic:
    """Map a pyrefly error to a diagnostic"""
    # Extract line/column info (pyrefly uses 1-based indexing)
    start_line = error["line"]
    start_column = error["column"]
    end_line = error["stop_line"]
    end_column = error["stop_column"]

    error_code = str(error.get("code", ""))
    code_description = error.get("name", "")
    severity = DiagnosticSeverity.ERROR

    return Diagnostic(
        range=Range(
            start=Position(line=start_line - 1, character=start_column),
            end=Position(line=end_line - 1, character=end_column),
        ),
        message=error.get("description", ""),
        code=error_code,
        code_description=code_description,
        source="pyrefly",
        severity=severity,
    )
