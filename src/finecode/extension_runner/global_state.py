from pathlib import Path
from typing import Literal

import finecode.extension_runner.context as context

runner_context: context.RunnerContext | None = None
# it's the same as `runner_context.project.path`, but it's available from the start of
# the runner, not from updating the config
project_dir_path: Path | None = None
log_level: Literal["TRACE", "INFO"] = "INFO"
