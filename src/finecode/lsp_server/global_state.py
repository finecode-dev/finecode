import asyncio
from pathlib import Path

from finecode.wm_client import ApiClient

# Set only after the WM server is connected AND all workspace/addDir calls succeed.
# Handlers that gate on this event must not call WM before it fires.
server_initialized = asyncio.Event()
wm_client: ApiClient | None = None
partial_result_tokens: dict[str | int, tuple[str, str]] = {}
# Open documents as last sent to the WM, by URI: {"uri", "version", "text"}.
# The WM holds this state on the LSP server's behalf and a restarted WM has none
# of it, so the LSP server keeps what it needs to re-supply after a reconnect
# (ADR-0074 rule 3).
opened_documents: dict[str, dict] = {}
wm_log_level: str = "INFO"
lsp_log_file_path: Path | None = None
