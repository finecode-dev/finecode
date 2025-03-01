import os
from pathlib import Path

import command_runner

from finecode_extension_api.interfaces import icommandrunner


class CommandRunner(icommandrunner.ICommandRunner):
    # TODO: stdout and stderr
    async def run(
        self, cmd: str, cwd: Path | None = None, env: dict[str, str] | None = None
    ):
        if cwd is not None:
            old_current_dir = os.getcwd()
            os.chdir(cwd)
        # old_virtual_env_value: str | None = None
        # if "VIRTUAL_ENV" in os.environ:
        #     old_virtual_env_value = os.environ["VIRTUAL_ENV"]
        #     del os.environ["VIRTUAL_ENV"]
        # exit_code, output =
        command_runner.command_runner_threaded(cmd)
        if cwd is not None:
            os.chdir(old_current_dir)
        # if old_virtual_env_value is not None:
        #     os.environ["VIRTUAL_ENV"] = old_virtual_env_value
