"""Server-wide pyrefly settings, owned by the shared pyrefly LSP server.

The same settings must apply in LSP mode and in CLI mode, so they live in a
service rather than on one handler: multiple pyrefly handlers share one LSP server per
runner, and a per-handler surface for a per-server setting would drift.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

from finecode_extension_api import code_action, service
from finecode_extension_api.interfaces import (
    icommandrunner,
    iextensionrunnerinfoprovider,
    ilogger,
    iprojectinfoprovider,
)

from fine_python_pyrefly._error_config import (
    PyreflyErrorSeverity,
    cli_severity_args,
    dump_config_args,
    find_project_pyrefly_config,
    render_lsp_config,
)


@dataclasses.dataclass
class PyreflySettings:
    errors: dict[str, PyreflyErrorSeverity] = dataclasses.field(default_factory=dict)


class PyreflyConfig(service.Service):
    """Owns the generated pyrefly config and the CLI severity flags."""

    def __init__(
        self,
        config: PyreflySettings,
        command_runner: icommandrunner.ICommandRunner,
        extension_runner_info_provider: iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self._config = config
        self._command_runner = command_runner
        self._extension_runner_info_provider = extension_runner_info_provider
        self._project_info_provider = project_info_provider
        self._logger = logger
        self._pyrefly_bin_path = Path(sys.executable).parent / "pyrefly"
        self._lsp_config_path: Path | None = None

    async def init(self) -> None:
        """Check for a conflicting project config, then generate and validate ours.

        Runs in both modes and once per runner: CLI mode pays one ``dump-config``
        and gets the same fail-early behaviour as LSP mode.
        """
        if not self._config.errors:
            return
        project_dir = self._project_info_provider.get_current_project_dir_path()
        project_config = find_project_pyrefly_config(project_dir)
        if project_config is not None:
            raise code_action.ActionFailedException(
                f"Found pyrefly configuration at {project_config}. Type-check "
                "configuration belongs in the `PyreflyConfig` service's `errors`: "
                "remove the project pyrefly config or remove `errors` from the "
                "service config."
            )
        config_path = (
            self._extension_runner_info_provider.get_cache_dir_path()
            / "pyrefly"
            / "pyrefly.toml"
        )
        config_path.parent.mkdir(parents=True, exist_ok=True)
        content = render_lsp_config(project_dir, self._config.errors)
        if (
            not config_path.exists()
            or config_path.read_text(encoding="utf-8") != content
        ):
            config_path.write_text(content, encoding="utf-8")
        dump_config_process = await self._command_runner.run(
            dump_config_args(self._pyrefly_bin_path, config_path)
        )
        await dump_config_process.wait_for_end()
        if dump_config_process.get_exit_code() != 0:
            raise code_action.ActionFailedException(
                f"Generated pyrefly config {config_path} is invalid: "
                f"{dump_config_process.get_output()}"
                f"{dump_config_process.get_error_output()}"
            )
        self._lsp_config_path = config_path
        self._logger.debug(f"pyrefly LSP config written to {config_path}")

    @property
    def lsp_config_path(self) -> Path | None:
        """The generated config file, or ``None`` when nothing is configured."""
        return self._lsp_config_path

    def cli_args(self) -> list[str]:
        return cli_severity_args(self._config.errors)
