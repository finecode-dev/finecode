from pathlib import Path
from typing import Literal

import finecode_extension_runner.context as context
from finecode_extension_runner import er_wal

runner_context: context.RunnerContext | None = None
# it's the same as `runner_context.project.dir_path`, but it's available from the start of
# the runner, not from updating the config
project_dir_path: Path | None = None
log_level: Literal["TRACE", "INFO"] = "INFO"
env_name: str = ""
log_file_path: Path | None = None
wal_writer: er_wal.ErWalWriter | None = None
