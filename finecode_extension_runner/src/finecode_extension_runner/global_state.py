from pathlib import Path
from typing import Literal

from finecode_extension_runner import er_wal

project_dir_path: Path | None = None
log_level: Literal["TRACE", "INFO"] = "INFO"
env_name: str = ""
log_file_path: Path | None = None
wal_writer: er_wal.ErWalWriter | None = None
