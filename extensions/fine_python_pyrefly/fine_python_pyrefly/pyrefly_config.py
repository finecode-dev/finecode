"""Server-wide pyrefly settings, owned by the shared pyrefly LSP server.

The same settings must apply in LSP mode and in CLI mode, so they live in a
service rather than on one handler: multiple pyrefly handlers share one LSP server per
runner, and a per-handler surface for a per-server setting would drift.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Literal

from finecode_extension_api import code_action, service
from finecode_extension_api.interfaces import (
    icommandrunner,
    iextensionrunnerinfoprovider,
    ilogger,
    iprojectinfoprovider,
    iworkspaceinfoprovider,
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
    # "workspace" runs one host in the workspace root and forwards every other
    # project to it (ADR-0108); "per-project" keeps one server per project.
    topology: Literal["per-project", "workspace"] = "workspace"


class PyreflyConfig(service.Service):
    """Owns the generated pyrefly config and the CLI severity flags."""

    def __init__(
        self,
        config: PyreflySettings,
        command_runner: icommandrunner.ICommandRunner,
        extension_runner_info_provider: iextensionrunnerinfoprovider.IExtensionRunnerInfoProvider,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        workspace_info_provider: iworkspaceinfoprovider.IWorkspaceInfoProvider,
        logger: ilogger.ILogger,
    ) -> None:
        self._config = config
        self._command_runner = command_runner
        self._extension_runner_info_provider = extension_runner_info_provider
        self._project_info_provider = project_info_provider
        self._workspace_info_provider = workspace_info_provider
        self._logger = logger
        self._pyrefly_bin_path = Path(sys.executable).parent / "pyrefly"
        self._lsp_config_path: Path | None = None

    async def init(self) -> None:
        """Check for a conflicting project config, then generate and validate ours.

        Runs in both modes, once per runner, and always — even with an empty
        `errors` table: the generated config is what scopes pyrefly to this
        project's files, and every project gets one. It is written under
        `.finecode/` rather than the virtualenv cache so it is not itself a
        dependency input, and is isolated per runner environment.
        """
        project_dir = self._project_info_provider.get_current_project_dir_path()
        project_config = find_project_pyrefly_config(project_dir)
        if project_config is not None:
            raise code_action.ActionFailedException(
                f"Found pyrefly configuration at {project_config}. Pyrefly is"
                " configured only through the PyreflyConfig service."
                f" Remove {project_config}."
            )
        # A workspace read, not a fallback: an answer that cannot be obtained
        # fails the runner start rather than rendering a config without the
        # nested projects' excludes, which would let this project claim their
        # files.
        workspace_projects = (
            await self._workspace_info_provider.get_workspace_projects()
        )
        exclude_dirs = [
            project.path
            for project in workspace_projects
            if project.path != project_dir
            and project.path.is_relative_to(project_dir)
        ]
        config_path = (
            project_dir
            / ".finecode"
            / "pyrefly"
            / self._extension_runner_info_provider.get_current_env_name()
            / "pyrefly.toml"
        )
        _refuse_intervening_ignore_files(config_path.parent, project_dir)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        content = render_lsp_config(project_dir, self._config.errors, exclude_dirs)
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
        """The generated config file; always set after a successful ``init``."""
        return self._lsp_config_path

    @property
    def topology(self) -> str:
        """The configured topology: "per-project" or "workspace"."""
        return self._config.topology

    def cli_args(self) -> list[str]:
        return cli_severity_args(self._config.errors)


def _refuse_intervening_ignore_files(config_dir: Path, project_dir: Path) -> None:
    """Fail when an ignore file between the generated config and the project
    would shadow pyrefly's upward ignore discovery.

    Pyrefly 1.3.0 searches upward from the config for the first ``.gitignore``,
    ``.ignore`` and ``.git/info/exclude``; one of them between the config and
    the project root changes which files pyrefly considers owned, silently.
    Translating or skipping it is not an option.

    Raises:
        ActionFailedException: one of the three files exists in a directory on
            the way from *config_dir* up to, excluding, *project_dir*.
    """
    current = config_dir
    while current != project_dir:
        for candidate in (
            current / ".gitignore",
            current / ".ignore",
            current / ".git" / "info" / "exclude",
        ):
            if candidate.is_file():
                raise code_action.ActionFailedException(
                    f"Found ignore file at {candidate}, between the generated"
                    f" pyrefly config ({config_dir}) and the project directory"
                    f" ({project_dir}). It shadows pyrefly's project ignore"
                    " discovery. Remove it, or move the generated config so it"
                    " is not nested under it."
                )
        parent = current.parent
        if parent == current:
            return
        current = parent
