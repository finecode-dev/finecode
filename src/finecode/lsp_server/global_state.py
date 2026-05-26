import asyncio
from pathlib import Path

from finecode.wm_client import ApiClient

# Set only after the WM server is connected AND all workspace/addDir calls succeed.
# Handlers that gate on this event must not call WM before it fires.
server_initialized = asyncio.Event()
wm_client: ApiClient | None = None
partial_result_tokens: dict[str | int, tuple[str, str]] = {}
wm_log_level: str = "INFO"
lsp_log_file_path: Path | None = None
