# docs: docs/cli.md
import pathlib

import click

from finecode.wm_client import ApiClient, ApiError
from finecode.wm_server import wm_lifecycle
from finecode.cli_app.log_render import render_log_records
from loguru import logger


class PrepareEnvsFailed(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


async def prepare_envs(
    workdir_path: pathlib.Path,
    recreate: bool,
    own_server: bool = True,
    log_level: str = "INFO",
    env_names: list[str] | None = None,
    interpreter_names: list[str] | None = None,
    project_names: list[str] | None = None,
    dev_env: str = "cli",
    verbose: bool = False,
) -> None:
    """Prepare all virtual environments for a workspace.

    Orchestration steps:
    1. Discover projects (without starting runners — envs may not exist yet).
    2. Check / remove dev_workspace environments as needed.
    2.5. Start workspace root dev_workspace runner (resolves preset-contributed
         actions such as ``fine_envs.CreateEnvsAction`` needed in step 3).
    3. Run ``create_envs`` + ``install_envs`` to create / update dev_workspace envs.
    4. Start all dev_workspace runners (resolves preset actions for all projects).
    5. Run ``create_envs`` to create all virtualenvs.
    6. Run ``install_envs`` to install all dependencies.

    ``env_names`` and ``interpreter_names`` (together with each matrix env's
    config-declared ``default_interpreters`` policy) select a subset of a
    matrix env's interpreter axis (PRD-0003 AC8): unselected matrix children
    are skipped in both step 5 and step 6. Non-matrix envs are always created
    in step 5; they are only installed in step 6 if selected (or if selection
    is inactive, in which case both steps cover every env). ``dev_env`` is
    used to resolve each matrix env's config-declared default interpreter
    subset when ``interpreter_names`` is not given.
    When ``project_names`` is given only those projects are prepared in steps 3, 5, and 6.
    """
    port_file = None
    try:
        if own_server:
            port_file = wm_lifecycle.start_own_server(workdir_path, log_level=log_level)
            try:
                port = await wm_lifecycle.wait_until_ready_from_file(port_file)
            except TimeoutError as exc:
                raise PrepareEnvsFailed(str(exc)) from exc
        else:
            wm_lifecycle.ensure_running(workdir_path)
            try:
                port = await wm_lifecycle.wait_until_ready()
            except TimeoutError as exc:
                raise PrepareEnvsFailed(str(exc)) from exc

        client = ApiClient()
        await client.connect("127.0.0.1", port)
        # Silence "unhandled notification" trace log — treeChanged is irrelevant in CLI mode.
        async def _noop(_: object) -> None: pass
        client.on_notification("actions/treeChanged", _noop)

        if verbose:
            async def _on_log_records(params: dict) -> None:
                for line in render_log_records(params):
                    click.echo(line, err=True)

            client.on_notification("server/logRecords", _on_log_records)
            # Stream WM+ER logs at the single general level (--log-level).
            await client.subscribe_logs(log_level)
        try:
            await _run(
                client,
                workdir_path,
                recreate,
                env_names,
                interpreter_names,
                project_names,
                dev_env,
            )
        finally:
            await client.close()
    finally:
        if port_file is not None and port_file.exists():
            port_file.unlink(missing_ok=True)


async def _run(
    client: ApiClient,
    workdir_path: pathlib.Path,
    recreate: bool,
    env_names: list[str] | None = None,
    interpreter_names: list[str] | None = None,
    project_names: list[str] | None = None,
    dev_env: str = "cli",
) -> None:
    try:
        await client.prepare_envs(
            workdir_path=workdir_path,
            recreate=recreate,
            env_names=env_names,
            interpreter_names=interpreter_names,
            project_names=project_names,
            dev_env=dev_env,
        )
    except ApiError as exc:
        raise PrepareEnvsFailed(str(exc)) from exc


__all__ = ["prepare_envs", "PrepareEnvsFailed"]
