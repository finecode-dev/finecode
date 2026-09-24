# docs: docs/cli.md
"""Recovery and stop commands: make a running workspace pick up what changed on disk,
or stop the workspace.

Every one of them requires ``--shared-server``. Recovery only means something to
a workspace someone else is already using: starting a private server, recovering
it and exiting would report success while changing nothing that outlives the
command — the failure this capability exists to remove, reintroduced by the
capability itself (ADR-0077 rule 4).
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import click
from loguru import logger

from finecode.wm_client import ApiClient, ApiError, ReconnectPolicy
from finecode.wm_server import wm_lifecycle


class RecoveryFailed(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


def require_shared_server(own_server: bool, command: str) -> None:
    """Raise unless the command is running against a shared server.

    Raises:
        RecoveryFailed: the command was invoked in dedicated-server mode.
    """
    if own_server:
        raise RecoveryFailed(
            f"'{command}' needs --shared-server. Without it this command starts a "
            f"workspace server of its own, recovers that, and exits — the running "
            f"workspace an editor or agent is using would be untouched, and the "
            f"success reported here would mean nothing."
        )


async def _connected_client(workdir_path: pathlib.Path) -> ApiClient:
    """Connect to the workspace server that is already running.

    Deliberately does not start one. ``require_shared_server`` refuses the
    dedicated-server flag because recovering a server this command started is a
    success that means nothing — starting one here whenever none happened to be
    listening would reintroduce exactly that, past the guard rather than
    through it (ADR-0077 rule 4).

    Raises:
        RecoveryFailed: no workspace server is listening.
    """
    port = await asyncio.to_thread(wm_lifecycle.running_port)
    if port is None:
        raise RecoveryFailed(
            "No FineCode workspace server is running, so there is nothing to "
            "recover. Start one — an editor with the FineCode LSP, the MCP "
            "server, or 'python -m finecode start-wm-server' — and run this "
            "again."
        )

    client = ApiClient()

    async def _noop(_: object) -> None:
        pass

    client.on_notification("actions/treeChanged", _noop)

    async def _attach_session(*, first_connect: bool) -> None:
        await client.add_dir(workdir_path)

    client.configure_reconnect(
        ReconnectPolicy(workdir=workdir_path), on_reattach=_attach_session
    )
    try:
        await client.connect("127.0.0.1", port)
    except (ApiError, ConnectionError, OSError) as exc:
        raise RecoveryFailed(str(exc)) from exc
    return client


def _report(result: object) -> None:
    click.echo(json.dumps(result, indent=2))


async def reload_action(
    workdir_path: pathlib.Path,
    action: str,
    project: str | None,
    own_server: bool = True,
) -> None:
    require_shared_server(own_server, "reload-action")
    client = await _connected_client(workdir_path)
    try:
        result = await client.reload_action(action_source=action, project=project)
    except ApiError as exc:
        raise RecoveryFailed(str(exc)) from exc
    finally:
        await client.close()
    _report(result)


async def restart_runner(
    workdir_path: pathlib.Path,
    project: str | None,
    all_projects: bool,
    env: str | None,
    kill_in_flight_runs: bool,
    own_server: bool = True,
) -> None:
    require_shared_server(own_server, "restart-runner")
    client = await _connected_client(workdir_path)
    try:
        result = await client.restart_runner(
            project=project,
            all_projects=all_projects,
            env=env,
            kill_in_flight_runs=kill_in_flight_runs,
        )
    except ApiError as exc:
        raise RecoveryFailed(str(exc)) from exc
    finally:
        await client.close()
    _report(result)


async def reload_config(
    workdir_path: pathlib.Path,
    project: str | None,
    all_projects: bool,
    rescan: bool,
    kill_in_flight_runs: bool,
    own_server: bool = True,
) -> None:
    require_shared_server(own_server, "reload-config")
    client = await _connected_client(workdir_path)
    try:
        projects = await client.reload_config(
            project=project,
            all_projects=all_projects,
            rescan=rescan,
            kill_in_flight_runs=kill_in_flight_runs,
        )
    except ApiError as exc:
        raise RecoveryFailed(str(exc)) from exc
    finally:
        await client.close()
    _report({"projects": projects})


async def restart_wm(workdir_path: pathlib.Path, own_server: bool = True) -> None:
    require_shared_server(own_server, "restart-wm")
    client = await _connected_client(workdir_path)
    try:
        info = await client.get_info()
        other_clients = info.get("clients", [])
        if other_clients:
            # Disclosure, not a gate: they reconnect on their own (ADR-0074).
            logger.warning(
                f"Replacing the workspace server disconnects {len(other_clients)} "
                f"other client(s): {', '.join(other_clients)}"
            )
        replacement = await wm_lifecycle.replace_running_server(client, workdir_path)
    except (ApiError, TimeoutError) as exc:
        raise RecoveryFailed(str(exc)) from exc
    finally:
        await client.close()
    _report(
        {
            "restarted": True,
            "previousPid": info.get("pid"),
            "port": replacement["port"],
            "otherClientsDisconnected": other_clients,
        }
    )


async def stop_wm(
    workdir_path: pathlib.Path, own_server: bool = True, timeout: float = 30
) -> None:
    require_shared_server(own_server, "stop-wm")
    previous_port = await asyncio.to_thread(wm_lifecycle.running_port)
    if previous_port is None:
        raise RecoveryFailed(
            "No FineCode workspace server is running, so there is nothing to stop. "
            "Start one — an editor with the FineCode LSP, the MCP server, or "
            "'python -m finecode start-wm-server' — and run this again."
        )
    client = await _connected_client(workdir_path)
    try:
        try:
            await client.shutdown()
        except (ConnectionError, OSError) as exception:
            # Same as replace_running_server (wm_lifecycle.py):
            # the server may close the socket before its response is read;
            # it is stopping either way, which the wait below verifies.
            logger.debug(f"WM server closed the connection on shutdown: {exception}")
    finally:
        await client.close()
    try:
        await wm_lifecycle.wait_until_stopped(timeout=timeout)
    except TimeoutError as exception:
        raise RecoveryFailed(
            f"FineCode WM server on port {previous_port} did not stop within {timeout}s"
        ) from exception
    _report({"stopped": True, "port": previous_port})
