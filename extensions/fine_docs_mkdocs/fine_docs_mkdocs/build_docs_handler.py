from __future__ import annotations

import dataclasses
import shlex
import sys
from pathlib import Path

from finecode_extension_api import code_action
from fine_docs.build_docs_action import (
    BuildDocsAction,
    BuildDocsRunContext,
    BuildDocsRunPayload,
    BuildDocsRunResult,
)
from finecode_extension_api.interfaces import icommandrunner, ilogger, iprojectinfoprovider
from finecode_extension_api.resource_uri import path_to_resource_uri, resource_uri_to_path


@dataclasses.dataclass
class MkdocsBuildDocsHandlerConfig(code_action.ActionHandlerConfig):
    strict: bool = False
    """Enable mkdocs strict mode (--strict): treat warnings as errors."""


class MkdocsBuildDocsHandler(
    code_action.ActionHandler[BuildDocsAction, MkdocsBuildDocsHandlerConfig]
):
    def __init__(
        self,
        config: MkdocsBuildDocsHandlerConfig,
        logger: ilogger.ILogger,
        command_runner: icommandrunner.ICommandRunner,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
    ) -> None:
        self.config = config
        self.logger = logger
        self.command_runner = command_runner
        self.project_info_provider = project_info_provider
        self.mkdocs_bin = str(Path(sys.executable).parent / "mkdocs")

    async def run(
        self,
        payload: BuildDocsRunPayload,
        run_context: BuildDocsRunContext,
    ) -> BuildDocsRunResult:
        project_dir = self.project_info_provider.get_current_project_dir_path()

        cmd_parts = [self.mkdocs_bin, "build"]

        if payload.docs_source_dir is not None:
            self.logger.warning(
                "mkdocs does not support overriding the docs directory via CLI; "
                "ignoring docs_source_dir. Configure docs_dir in mkdocs.yml instead."
            )

        output_path: Path | None = None
        if payload.output_dir is not None:
            output_path = resource_uri_to_path(payload.output_dir)
            cmd_parts.extend(["--site-dir", str(output_path)])

        if self.config.strict:
            cmd_parts.append("--strict")

        cmd = shlex.join(cmd_parts)
        self.logger.debug(f"Running mkdocs build: {cmd}")

        process = await self.command_runner.run(cmd, cwd=project_dir)
        await process.wait_for_end()

        exit_code = process.get_exit_code()
        stdout = process.get_output()
        stderr = process.get_error_output()

        self.logger.debug(f"mkdocs build exit code: {exit_code}")
        if stdout:
            self.logger.debug(f"mkdocs build stdout:\n{stdout}")
        if stderr:
            self.logger.debug(f"mkdocs build stderr:\n{stderr}")

        if exit_code != 0:
            raise code_action.ActionFailedException(
                f"mkdocs build exited with code {exit_code}.\nOutput:\n{stderr or stdout}"
            )

        if output_path is None:
            # mkdocs default output directory
            output_path = project_dir / "site"

        return BuildDocsRunResult(output_dir=path_to_resource_uri(output_path))
