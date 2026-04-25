from pathlib import Path
from typing import Literal

project_dir_path: Path | None = None
log_level: Literal["TRACE", "INFO"] = "INFO"
env_name: str = ""
log_file_path: Path | None = None
