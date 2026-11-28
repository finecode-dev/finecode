"""A question the WM stops waiting for is withdrawn from its client, end to end.

Driven against a real WM dispatch loop and real extension runners, because the
guarantees are about order on the wire between three parties: a client that can
answer, the WM, and a handler in another process. The client sees, in order, a
question, then the withdrawal of that question, and only then the next question
or the response of the run that asked. A question left on a person's screen
after the WM has stopped waiting would take the answer meant for the next one.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib
import shutil
import signal
import sys
import sysconfig
import typing
import uuid

import pytest

from finecode.wm_server import context, domain, wm_server
from finecode.wm_server._jsonrpc import _read_message, _write_message
from finecode.wm_server.services import shutdown_service
from tests.integration.conftest import InProcClient
from tests.integration.test_streaming_delegation import (
    _copy_env,
    _repair_copied_venv_path,
)

FIXTURE_MODULE = "question_withdrawal_fixture"
ELICIT = "client/elicit"
WITHDRAWN = "client/elicitWithdrawn"
LEAF_ENV = "dev_no_runtime"


class _Client(InProcClient):
    """An in-process WM client that records every frame and can answer questions."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        ws_context: context.WorkspaceContext,
    ) -> None:
        self.frames: list[dict[str, typing.Any]] = []
        super().__init__(reader, writer, ws_context)

    async def _read_loop(self) -> None:
        try:
            while True:
                msg = await _read_message(self._reader)
                if msg is None:
                    break
                self.frames.append(msg)
                if "id" in msg and ("result" in msg or "error" in msg):
                    future = self._pending.pop(msg["id"], None)
                    if future and not future.done():
                        future.set_result(msg)
        except (
            asyncio.IncompleteReadError,
            ConnectionResetError,
            asyncio.CancelledError,
        ):
            pass

    def events(self) -> list[tuple[str, int | str]]:
        """Questions, withdrawals and the run's response, in arrival order."""
        seen: list[tuple[str, int | str]] = []
        for frame in self.frames:
            method = frame.get("method")
            if method == ELICIT:
                seen.append(("question", frame["id"]))
            elif method == WITHDRAWN:
                params = frame["params"]
                seen.append((f"withdrawn:{params['reason']}", params["id"]))
            elif "returnCode" in (frame.get("result") or {}):
                seen.append(("response", frame["id"]))
        return seen

    async def question(
        self, ordinal: int, timeout: float = 30.0
    ) -> dict[str, typing.Any]:
        """The *ordinal*-th (1-based) ``client/elicit`` frame, once it has arrived."""

        async def _wait() -> dict[str, typing.Any]:
            while True:
                questions = [f for f in self.frames if f.get("method") == ELICIT]
                if len(questions) >= ordinal:
                    return questions[ordinal - 1]
                await asyncio.sleep(0.02)

        return await asyncio.wait_for(_wait(), timeout=timeout)

    async def withdrawal(self, question_id: int, timeout: float = 30.0) -> str:
        """The reason the question *question_id* was withdrawn for."""

        async def _wait() -> str:
            while True:
                for frame in self.frames:
                    if (
                        frame.get("method") == WITHDRAWN
                        and frame["params"]["id"] == question_id
                    ):
                        return str(frame["params"]["reason"])
                await asyncio.sleep(0.02)

        return await asyncio.wait_for(_wait(), timeout=timeout)

    def answer(self, question: dict[str, typing.Any], value: str) -> None:
        _write_message(
            self._writer,
            {
                "jsonrpc": "2.0",
                "id": question["id"],
                "result": {"outcome": "answered", "value": value},
            },
        )


@pytest.fixture
async def wm() -> typing.AsyncIterator[
    tuple[_Client, typing.Callable[[], typing.Awaitable[_Client]]]
]:
    ctx = context.WorkspaceContext([])
    server = await asyncio.start_server(
        lambda r, w: asyncio.ensure_future(wm_server._handle_client(r, w, ctx)),
        "127.0.0.1",
        0,
    )
    port = server.sockets[0].getsockname()[1]
    clients: list[_Client] = []

    async def connect() -> _Client:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        client = _Client(reader, writer, ctx)
        clients.append(client)
        return client

    first = await connect()
    try:
        yield first, connect
    finally:
        await shutdown_service.on_shutdown(ctx)
        for client in clients:
            await client.close()
        server.close()
        await server.wait_closed()


def _prepare_project(tmp_path: pathlib.Path) -> pathlib.Path:
    source_env = pathlib.Path(sys.executable).parent.parent
    project_dir = tmp_path / "project"
    fixture_modules = tmp_path / "fixture_modules"
    project_dir.mkdir(parents=True)
    fixture_modules.mkdir(parents=True)
    shutil.copy(
        pathlib.Path(__file__).parent / "fixtures" / f"{FIXTURE_MODULE}.py",
        fixture_modules / f"{FIXTURE_MODULE}.py",
    )
    venvs_dir = project_dir / ".venvs"
    venvs_dir.mkdir()
    purelib_relative = pathlib.Path(sysconfig.get_paths()["purelib"]).relative_to(
        source_env
    )
    for env_name in ("dev_workspace", LEAF_ENV):
        _copy_env(source_env, venvs_dir / env_name)
        _repair_copied_venv_path(venvs_dir / env_name, source_env)
        purelib = venvs_dir / env_name / purelib_relative
        (purelib / f"{FIXTURE_MODULE}.pth").write_text(
            f"{fixture_modules}\n", encoding="utf-8"
        )
    (project_dir / "pyproject.toml").write_text(
        f"""[project]
name = "question-withdrawal-probe"
version = "0.0.0"

[dependency-groups]
dev_workspace = ["finecode"]
dev_no_runtime = ["finecode_extension_runner"]

[tool.finecode.action.ask]
source = "{FIXTURE_MODULE}.AskAction"
handlers = [{{ name = "ask", source = "{FIXTURE_MODULE}.AskHandler", env = "{LEAF_ENV}" }}]

[tool.finecode.action.nested_ask]
source = "{FIXTURE_MODULE}.NestedAskAction"
handlers = [{{ name = "nested", source = "{FIXTURE_MODULE}.NestedAskHandler", env = "dev_workspace" }}]
""",
        encoding="utf-8",
    )
    return project_dir


async def _open_project(
    client: _Client,
    tmp_path: pathlib.Path,
    elicitation: dict[str, bool] | None = None,
) -> pathlib.Path:
    project_dir = _prepare_project(tmp_path)
    await client.request(
        "client/initialize",
        {
            "clientId": f"question-withdrawal-{uuid.uuid4().hex}",
            "capabilities": {"elicitation": elicitation or {"choice": True}},
        },
        timeout=60.0,
    )
    await client.request(
        "workspace/addDir", {"dirPath": str(project_dir)}, timeout=60.0
    )
    project = client.ws_context.ws_projects.get(project_dir)
    assert project is not None
    assert project.status == domain.ProjectStatus.CONFIG_VALID
    return project_dir


def _start_run(
    client: _Client,
    project_dir: pathlib.Path,
    action: str,
    **payload: typing.Any,
) -> tuple[str, asyncio.Task[typing.Any]]:
    """Start a run with a streaming token, which is what makes the client its origin."""
    request_id = str(uuid.uuid4())
    task = asyncio.ensure_future(
        client.request(
            "actions/run",
            {
                "actionSource": f"{FIXTURE_MODULE}.{action}",
                "project": str(project_dir),
                "params": payload,
                "options": {
                    "resultFormats": ["json"],
                    "trigger": "user",
                    "devEnv": "ci",
                },
                "partialResultToken": f"token-{request_id}",
            },
            timeout=90.0,
        )
    )
    return request_id, task


def _outcomes(client: _Client) -> list[str]:
    """What the handler reported, as the streamed result delivered it."""
    reported = [
        frame["params"]["value"]["resultByFormat"]["json"]["outcomes"]
        for frame in client.frames
        if frame.get("method") == "actions/partialResult"
        and "outcomes"
        in frame["params"]["value"].get("resultByFormat", {}).get("json", {})
    ]
    assert reported, "the run delivered no result"
    return list(reported[-1])


async def _settle(*tasks: asyncio.Task[typing.Any]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def test_a_question_that_expires_is_withdrawn_before_the_next_one(
    wm, tmp_path
) -> None:
    client, _ = wm
    project_dir = await _open_project(client, tmp_path)
    _, run = _start_run(
        client, project_dir, "AskAction", mode="ask_twice", timeout_sec=1.0
    )
    try:
        await run
    finally:
        await _settle(run)

    first, second = [f for f in client.frames if f.get("method") == ELICIT]
    assert _outcomes(client) == ["unavailable:None", "unavailable:None"]
    assert [e for e in client.events() if e[0] != "response"] == [
        ("question", first["id"]),
        ("withdrawn:expired", first["id"]),
        ("question", second["id"]),
        ("withdrawn:expired", second["id"]),
    ]


async def test_an_abandoned_ask_is_withdrawn_while_the_run_continues(
    wm, tmp_path
) -> None:
    client, _ = wm
    project_dir = await _open_project(client, tmp_path)
    release = project_dir / "release"
    _, run = _start_run(
        client, project_dir, "AskAction", mode="abandon", release_path=str(release)
    )
    try:
        question = await client.question(1)
        release.write_text("go\n", encoding="utf-8")
        assert await client.withdrawal(question["id"]) == "noLongerNeeded"
        await run
    finally:
        await _settle(run)

    assert _outcomes(client) == ["abandoned"]
    assert [kind for kind, _ in client.events()] == [
        "question",
        "withdrawn:noLongerNeeded",
        "response",
    ]


async def test_a_new_question_follows_the_withdrawal_of_the_abandoned_one(
    wm, tmp_path
) -> None:
    client, _ = wm
    project_dir = await _open_project(client, tmp_path)
    release = project_dir / "release"
    _, run = _start_run(
        client,
        project_dir,
        "AskAction",
        mode="abandon_then_ask",
        release_path=str(release),
    )
    try:
        first = await client.question(1)
        release.write_text("go\n", encoding="utf-8")
        second = await client.question(2)
        client.answer(second, "revert")
        await run
    finally:
        await _settle(run)

    assert _outcomes(client) == ["abandoned", "answered:revert"]
    assert [e for e in client.events() if e[0] != "response"] == [
        ("question", first["id"]),
        ("withdrawn:noLongerNeeded", first["id"]),
        ("question", second["id"]),
    ]


async def test_a_run_that_returns_withdraws_the_ask_it_left_pending(
    wm, tmp_path
) -> None:
    client, _ = wm
    project_dir = await _open_project(client, tmp_path)
    release = project_dir / "release"
    _, run = _start_run(
        client,
        project_dir,
        "AskAction",
        mode="return_pending",
        release_path=str(release),
    )
    try:
        question = await client.question(1)
        release.write_text("go\n", encoding="utf-8")
        await run
    finally:
        await _settle(run)

    assert _outcomes(client) == ["returned"]
    assert [kind for kind, _ in client.events()] == [
        "question",
        "withdrawn:noLongerNeeded",
        "response",
    ]
    assert wm_server._pending_client_requests == {}
    assert question["id"] not in wm_server._pending_questions


async def test_shutting_the_wm_down_withdraws_the_question_as_a_cancelled_run(
    wm, tmp_path
) -> None:
    client, connect = wm
    project_dir = await _open_project(client, tmp_path)
    _, run = _start_run(client, project_dir, "AskAction", mode="ask")
    try:
        question = await client.question(1)
        other = await connect()
        await other.request("server/shutdown")
        assert await client.withdrawal(question["id"]) == "runCancelled"
    finally:
        await _settle(run)


@pytest.mark.parametrize("action", ["AskAction", "NestedAskAction"])
async def test_the_asking_runner_dying_withdraws_the_question(
    wm, tmp_path, action
) -> None:
    client, _ = wm
    project_dir = await _open_project(client, tmp_path)
    _, run = _start_run(client, project_dir, action, mode="ask")
    try:
        question = await client.question(1)
        leaf = client.ws_context.ws_projects_extension_runners[project_dir][LEAF_ENV]
        assert leaf.client is not None and leaf.client.pid is not None
        os.kill(leaf.client.pid, signal.SIGKILL)
        assert await client.withdrawal(question["id"]) == "noLongerNeeded"
    finally:
        await _settle(run)


async def test_a_text_question_cancelled_with_its_run_is_withdrawn(
    wm, tmp_path
) -> None:
    client, connect = wm
    project_dir = await _open_project(
        client, tmp_path, {"choice": True, "text": True, "multiChoice": True}
    )
    _, run = _start_run(client, project_dir, "AskAction", mode="ask_text")
    try:
        question = await client.question(1)
        assert question["params"]["kind"] == "text"
        other = await connect()
        await other.request("server/shutdown")
        assert await client.withdrawal(question["id"]) == "runCancelled"
    finally:
        await _settle(run)
