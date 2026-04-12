import dataclasses
from pathlib import Path

from finecode_extension_api import code_action
from finecode_extension_api.actions.code_quality import precommit_action
from finecode_extension_api.interfaces import icommandrunner, ilogger


@dataclasses.dataclass
class StagedFilesDiscoveryHandlerConfig(code_action.ActionHandlerConfig): ...


class StagedFilesDiscoveryHandler(
    code_action.ActionHandler[
        precommit_action.PrecommitAction, StagedFilesDiscoveryHandlerConfig
    ]
):
    """Detect staged files and write them to run_context.staged_files.

    Uses payload.file_paths when provided; otherwise calls git diff --cached.
    """

    def __init__(
        self,
        logger: ilogger.ILogger,
        command_runner: icommandrunner.ICommandRunner,
    ) -> None:
        self.logger = logger
        self.command_runner = command_runner

    async def run(
        self,
        payload: code_action.RunActionPayload,
        run_context: code_action.RunActionContext[code_action.RunActionPayload],
    ) -> code_action.RunActionResult:
        if not isinstance(payload, precommit_action.PrecommitRunPayload):
            raise code_action.ActionFailedException(
                "Expected PrecommitRunPayload in StagedFilesDiscoveryHandler"
            )
        if not isinstance(run_context, precommit_action.PrecommitRunContext):
            raise code_action.ActionFailedException(
                "Expected PrecommitRunContext in StagedFilesDiscoveryHandler"
            )
        if payload.file_paths is not None:
            run_context.staged_files = payload.file_paths
            self.logger.info(
                f"Using {len(run_context.staged_files)} explicitly provided file(s)."
            )
        else:
            run_context.staged_files = await self._get_staged_files()
            self.logger.info(
                f"Detected {len(run_context.staged_files)} staged file(s)."
            )

        return precommit_action.PrecommitRunResult()

    async def _get_staged_files(self) -> list[Path]:
        """Run git diff --cached --name-only --diff-filter=ACMR and return absolute paths."""
        proc = await self.command_runner.run(
            "git diff --cached --name-only --diff-filter=ACMR"
        )
        await proc.wait_for_end()
        exit_code = proc.get_exit_code()
        if exit_code != 0:
            raise code_action.ActionFailedException(
                f"git diff --cached failed (exit {exit_code}): {proc.get_error_output().strip()}"
            )

        # git outputs paths relative to the repo root; resolve them to absolute paths
        repo_root = await self._get_repo_root()
        paths: list[Path] = []
        for line in proc.get_output().splitlines():
            line = line.strip()
            if line:
                paths.append(repo_root / line)
        return paths

    async def _get_repo_root(self) -> Path:
        proc = await self.command_runner.run("git rev-parse --show-toplevel")
        await proc.wait_for_end()
        exit_code = proc.get_exit_code()
        if exit_code != 0:
            raise code_action.ActionFailedException(
                f"git rev-parse --show-toplevel failed (exit {exit_code}): {proc.get_error_output().strip()}"
            )
        return Path(proc.get_output().strip())
