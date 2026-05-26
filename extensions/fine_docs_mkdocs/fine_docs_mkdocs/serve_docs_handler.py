from __future__ import annotations

import asyncio
import dataclasses
import sys
from pathlib import Path

from finecode_extension_api import code_action
from fine_docs.serve_docs_action import (
    ServeDocsAction,
    ServeDocsRunContext,
    ServeDocsRunPayload,
    ServeDocsRunResult,
)
from finecode_extension_api.interfaces import ilogger, iprojectinfoprovider

_READY_MARKER = "Serving on "


@dataclasses.dataclass
class MkdocsServeDocsHandlerConfig(code_action.ActionHandlerConfig):
    pass


class MkdocsServeDocsHandler(
    code_action.ActionHandler[ServeDocsAction, MkdocsServeDocsHandlerConfig]
):
    def __init__(
        self,
        config: MkdocsServeDocsHandlerConfig,
        logger: ilogger.ILogger,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
    ) -> None:
        self.config = config
        self.logger = logger
        self.project_info_provider = project_info_provider
        self.mkdocs_bin = str(Path(sys.executable).parent / "mkdocs")

    async def run(
        self,
        payload: ServeDocsRunPayload,
        run_context: ServeDocsRunContext,
    ):
        project_dir = self.project_info_provider.get_current_project_dir_path()

        cmd = [
            self.mkdocs_bin,
            "serve",
            "--dev-addr",
            f"{payload.host}:{payload.port}",
        ]

        if payload.docs_source_dir is not None:
            self.logger.warning(
                "mkdocs does not support overriding the docs directory via CLI; "
                "ignoring docs_source_dir. Configure docs_dir in mkdocs.yml instead."
            )

        self.logger.debug(f"Starting mkdocs serve: {' '.join(cmd)}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=project_dir,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        base_url: str | None = None
        bound_host: str | None = None
        bound_port: int | None = None

        try:
            assert process.stderr is not None

            # Read lines until the server reports its address or the process exits.
            async for line_bytes in process.stderr:
                line = line_bytes.decode("utf-8", errors="replace").rstrip()
                if line:
                    self.logger.debug(f"mkdocs: {line}")
                if _READY_MARKER in line:
                    base_url, bound_host, bound_port = _parse_serving_line(
                        line, payload.host, payload.port
                    )
                    break

            if base_url is None:
                # stderr reached EOF — process exited before becoming ready
                await process.wait()
                raise code_action.ActionFailedException(
                    f"mkdocs serve exited before becoming ready "
                    f"(exit code {process.returncode})"
                )

            self.logger.info(f"mkdocs serve running at {base_url}")

            yield ServeDocsRunResult(
                base_url=base_url,
                bound_host=bound_host,
                bound_port=bound_port,
            )

            async with run_context.progress("mkdocs serve", cancellable=True) as prog:
                await prog.report(message=f"Serving at {base_url}")
                try:
                    # Drain remaining stderr so the pipe buffer never fills.
                    async for line_bytes in process.stderr:
                        line = line_bytes.decode("utf-8", errors="replace").rstrip()
                        if line:
                            self.logger.debug(f"mkdocs: {line}")
                except asyncio.CancelledError:
                    pass

        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            self.logger.debug(f"mkdocs serve exit code: {process.returncode}")


def _parse_serving_line(
    line: str, fallback_host: str, fallback_port: int
) -> tuple[str, str, int]:
    """Extract base_url, bound_host, bound_port from a 'Serving on ...' log line."""
    idx = line.index(_READY_MARKER)
    url_part = line[idx + len(_READY_MARKER):].strip().rstrip("/")
    base_url = url_part if url_part.startswith("http") else f"http://{url_part}"

    # Parse host and port from "http://host:port"
    netloc = base_url.split("//", 1)[1]
    if ":" in netloc:
        bound_host, port_str = netloc.rsplit(":", 1)
        try:
            bound_port = int(port_str)
        except ValueError:
            bound_port = fallback_port
    else:
        bound_host = netloc
        bound_port = fallback_port

    return base_url, bound_host, bound_port
