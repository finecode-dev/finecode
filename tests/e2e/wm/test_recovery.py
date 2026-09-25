"""E2E tests for in-session recovery against a real WM server and a real ER.

These are the criteria that cannot be established in process, because every one
of them is "the recovery made the new configuration take effect" — which is only
observable when there is a real extension runner holding the old state to
replace.

Each test drives the shared ``ApiClient`` over TCP against a WM subprocess with
its own port file, so it can run alongside a developer's IDE session.
"""

from __future__ import annotations

import asyncio
import contextlib
import pathlib

import pytest

from finecode.wm_client import ApiClient
from tests.e2e.conftest import kill_group, start_server, wait_for_file

# An action the project does not have yet, whose class the runner's environment
# already provides and whose class no preset has registered — ADR-0007 allows an
# action class to be registered once. The edit under test is the configuration.
_SECOND_ACTION = """
[tool.finecode.action.lock_dependencies]
source = "fine_src_artifacts.LockDependenciesAction"

[[tool.finecode.action.lock_dependencies.handlers]]
name = "lock_dependencies_dispatch"
source = "fine_src_artifacts.LockDependenciesDispatchHandler"
env = "dev_workspace"
"""


class _Wm:
    def __init__(self, proc, port: int) -> None:
        self.proc = proc
        self.port = port


async def _start_wm(workspace_dir: pathlib.Path, port_file: pathlib.Path) -> _Wm:
    proc = start_server(
        [
            "start-wm-server",
            "--port-file",
            str(port_file),
            "--disconnect-timeout",
            "120",
        ],
        cwd=workspace_dir,
    )
    assert wait_for_file(port_file, timeout=30), (
        "WM server did not write its port file within 30 s"
    )
    return _Wm(proc, int(port_file.read_text().strip()))


async def _connected(wm: _Wm, workspace_dir: pathlib.Path) -> ApiClient:
    client = ApiClient()

    async def _noop(_: object) -> None:
        pass

    client.on_notification("actions/treeChanged", _noop)
    client.on_notification("server/userMessage", _noop)
    await client.connect("127.0.0.1", wm.port, client_id="e2e")
    await client.add_dir(workspace_dir)
    return client


@pytest.fixture
async def wm_with_er(workspace_dir_with_er, tmp_path):
    """A running WM with a real extension runner for the test workspace."""
    wm = await _start_wm(workspace_dir_with_er, tmp_path / "wm_port")
    client = await _connected(wm, workspace_dir_with_er)
    try:
        yield client, workspace_dir_with_er
    finally:
        await client.close()
        kill_group(wm.proc)


async def test_a_runner_that_comes_back_is_reported_running(wm_with_er) -> None:
    """PRD-0008-AC16 — a runner recovery reports the runner's post-recovery
    state, and a runner that does come back is reported as running.

    The failure limb (PRD-0008-AC6) is covered in process; this is the half that
    needs a real process to actually come back up, and without it "reported as
    failed" could be satisfied by a recovery that always reports failure.
    """
    client, workspace_dir = wm_with_er

    result = await client.restart_runner(
        project=str(workspace_dir), env="dev_workspace"
    )

    assert result["failed"] == []
    assert result["refused"] == []
    assert [(entry["env"], entry["status"]) for entry in result["restarted"]] == [
        ("dev_workspace", "RUNNING")
    ]

    # Still usable afterwards, which is what "came back" has to mean.
    runners = await client.list_runners()
    assert [r["status"] for r in runners] == ["RUNNING"]


async def test_configuration_edited_on_disk_takes_effect_without_reconnecting(
    wm_with_er,
) -> None:
    """PRD-0008-AC3 — an edit to a project's configuration is in effect after a
    recovery, on the connection that was already open.

    This is the whole point of the capability: the editor or agent that asked for
    the recovery keeps its session, and the next thing it does uses the
    configuration that is now on disk rather than the one read at startup.
    """
    client, workspace_dir = wm_with_er

    before = {action["name"] for action in await client.list_actions()}
    assert "lock_dependencies" not in before

    pyproject = workspace_dir / "pyproject.toml"
    pyproject.write_text(pyproject.read_text() + _SECOND_ACTION)

    projects = await client.reload_config(project=str(workspace_dir))

    assert projects[0]["status"] == "recovered", projects
    assert projects[0]["actionsAdded"] == ["lock_dependencies"]
    assert projects[0]["actionsRemoved"] == []

    # Same client, same connection: nothing was restarted on this side.
    after = {action["name"] for action in await client.list_actions()}
    assert "lock_dependencies" in after


async def test_recovery_names_the_environment_command_when_the_env_is_gone(
    wm_with_er,
) -> None:
    """PRD-0008-AC14 — when a recovery cannot finish because the environment no
    longer satisfies the configuration, the report names the command to run.

    Here the environment is removed outright, which is the same class of failure
    as a dependency added to pyproject.toml without reinstalling: the runner
    cannot come back, and what the caller needs is the environment command, not
    the runner's import error.
    """
    client, workspace_dir = wm_with_er

    # Take the environment away underneath the running runner.
    venv_dir = workspace_dir / ".venvs" / "dev_workspace"
    venv_dir.rename(workspace_dir / ".venvs" / "dev_workspace_moved")

    result = await client.restart_runner(
        project=str(workspace_dir), env="dev_workspace"
    )

    assert result["restarted"] == []
    failure = result["failed"][0]
    assert failure["status"] == "NO_VENV"
    assert "prepare-envs" in failure["nextStep"]
    assert "--env=dev_workspace" in failure["nextStep"]


async def test_a_recovery_leaves_another_client_connected(
    wm_with_er, workspace_dir_with_er
) -> None:
    """PRD-0008-AC8 — a project-scoped recovery performed by one client leaves a
    second client connected and working, with nothing to reconnect.

    The in-process version of this asserts the same thing against the dispatch
    loop; this one proves it across two real TCP connections to a real server,
    where a recovery that disturbed the other connection would actually show.
    """
    client, workspace_dir = wm_with_er
    bystander = ApiClient()

    async def _noop(_: object) -> None:
        pass

    bystander.on_notification("actions/treeChanged", _noop)
    bystander.on_notification("server/userMessage", _noop)
    await bystander.connect("127.0.0.1", client._writer.get_extra_info("peername")[1])
    try:
        assert await bystander.list_projects() != []

        await client.reload_config(project=str(workspace_dir))

        # Same connection, no reconnect.
        assert await bystander.list_projects() != []
        assert bystander.is_connected
    finally:
        await bystander.close()


@pytest.fixture
def _no_foreign_shared_wm():
    """The WM-replacement test needs the shared discovery file, which is the one
    a developer's own session uses. Refusing to run against someone else's server
    is better than replacing it."""
    from finecode.wm_server import wm_lifecycle

    if wm_lifecycle.running_port() is not None:
        pytest.skip(
            f"a WM server is already listening on the shared discovery file "
            f"({wm_lifecycle.discovery_file_path()}); this test replaces whatever "
            f"server that file points at, so it does not run against a foreign one"
        )


async def test_wm_replacement_keeps_the_requesting_client_usable(
    workspace_dir_with_er, _no_foreign_shared_wm
) -> None:
    """PRD-0008-AC9 — a client that replaces the WM server is usable afterwards
    with no intervention, and learns which other clients it disconnected before
    doing it.

    Replacement is the rung that picks up an edit to FineCode's own code. It is
    only usable at all because the clients that did not ask for it come back on
    their own; before ADR-0074 the honest choice would have been to refuse
    whenever anyone else was attached.
    """
    from finecode.wm_client import ReconnectPolicy
    from finecode.wm_server import wm_lifecycle

    await asyncio.to_thread(wm_lifecycle.ensure_running, workspace_dir_with_er)
    port = await wm_lifecycle.wait_until_ready(timeout=30)

    client = ApiClient()
    attached: list[bool] = []

    async def _attach(*, first_connect: bool) -> None:
        attached.append(first_connect)
        await client.add_dir(workspace_dir_with_er)

    async def _noop(_: object) -> None:
        pass

    client.on_notification("actions/treeChanged", _noop)
    client.on_notification("server/userMessage", _noop)
    client.configure_reconnect(
        ReconnectPolicy(may_start_server=True, workdir=workspace_dir_with_er),
        on_reattach=_attach,
    )
    await client.connect("127.0.0.1", port, client_id="e2e-initiator")

    bystander = ApiClient()
    bystander.on_notification("actions/treeChanged", _noop)
    bystander.on_notification("server/userMessage", _noop)
    await bystander.connect("127.0.0.1", port, client_id="e2e-bystander")

    try:
        # Disclosed before the replacement, which is what makes it informative
        # rather than a surprise.
        info = await client.get_info()
        assert "e2e-bystander" in info["clients"]
        old_pid = info["pid"]

        replacement = await wm_lifecycle.replace_running_server(
            client, workspace_dir_with_er, timeout=60
        )

        assert replacement["port"] != replacement["previousPort"]
        assert attached == [True, False], "the client did not re-attach its session"

        # Usable with no intervention: a request on the same client object.
        new_info = await client.get_info()
        assert new_info["pid"] != old_pid
        assert await client.list_projects() != []
    finally:
        await bystander.close()
        await client.close()
        # This test uses the shared discovery file, so it must not leave the
        # server it started behind for the next test or the developer.
        port_now = wm_lifecycle.running_port()
        if port_now is not None:
            stopper = ApiClient()
            await stopper.connect("127.0.0.1", port_now)
            with contextlib.suppress(ConnectionError, OSError):
                await stopper.request("server/shutdown")
            await stopper.close()
