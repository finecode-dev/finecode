# docs: docs/cli.md
import pathlib

from finecode.wm_client import ApiClient
from finecode.wm_server import wm_lifecycle


class VersionCheckFailed(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


async def get_version(
    workdir_path: pathlib.Path,
    own_server: bool = True,
    log_level: str = "INFO",
) -> str:
    """Start (or attach to) the WM server and return the version it reports.

    Deliberately round-trips through a real server instead of reading
    ``finecode.__version__`` locally: that only proves the CLI process's own
    package metadata is intact, not that the server can actually complete its
    startup path (spawn, import, bind, respond) — which is what every other
    client depends on and what actually breaks (e.g. ADR-0090's process
    budget import chain crashing before the server's own logger exists).
    """
    port_file = None
    try:
        if own_server:
            port_file = wm_lifecycle.start_own_server(workdir_path, log_level=log_level)
            try:
                port = await wm_lifecycle.wait_until_ready_from_file(port_file)
            except TimeoutError as exc:
                raise VersionCheckFailed(str(exc)) from exc
        else:
            wm_lifecycle.ensure_running(workdir_path)
            try:
                port = await wm_lifecycle.wait_until_ready()
            except TimeoutError as exc:
                raise VersionCheckFailed(str(exc)) from exc

        client = ApiClient()
        await client.connect("127.0.0.1", port)
        try:
            info = await client.get_info()
        finally:
            await client.close()
        return info["version"]
    finally:
        if port_file is not None and port_file.exists():
            port_file.unlink(missing_ok=True)
