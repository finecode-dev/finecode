"""Minimal WM-side test harness for cancellation-propagation tests.

Scoped specifically to testing the cancellation-propagation chain across
``runner_client.py``, ``proxy_utils.py``, and ``wm_server.py`` — this is not
a general-purpose WM testing framework. It provides just enough fake
infrastructure (a fake ER-facing JSON-RPC client, an already-RUNNING runner,
a minimal single-action resolved project, and a wired-up ``WorkspaceContext``)
to exercise WM-side action dispatch without starting a real ER subprocess.
"""

from __future__ import annotations

import typing
from pathlib import Path

import finecode_jsonrpc
from finecode_jsonrpc.client import ResponseError

from finecode.wm_server import context, domain
from finecode.wm_server.runner import runner_client


class FakeErClient:
    """Fake ER-facing JSON-RPC client standing in for ``JsonRpcClient``.

    Must be configured with :meth:`configure_response` or
    :meth:`configure_error` before the code under test calls
    :meth:`send_request` — calling it unconfigured raises ``AssertionError``,
    to catch accidentally-unconfigured test calls rather than silently
    returning ``None``.
    """

    def __init__(self) -> None:
        self._response: typing.Any = None
        self._exception: BaseException | None = None
        self._configured = False
        self.sent_requests: list[tuple[str, typing.Any]] = []

    def configure_response(self, response: typing.Any) -> None:
        self._response = response
        self._exception = None
        self._configured = True

    def configure_error(self, exception: BaseException) -> None:
        self._exception = exception
        self._response = None
        self._configured = True

    async def send_request(
        self,
        method: str,
        params: typing.Any = None,
        timeout: float | None = None,
    ) -> typing.Any:
        assert self._configured, (
            "FakeErClient.send_request called without configure_response()/"
            "configure_error() — configure the fake before exercising code"
            " that calls send_request."
        )
        self.sent_requests.append((method, params))
        if self._exception is not None:
            raise self._exception
        return self._response

    def notify(self, method: str, params: typing.Any | None = None) -> None:
        self.sent_requests.append((method, params))


def make_cancelled_error(message: str = "cancelled") -> finecode_jsonrpc.ErrorOnRequest:
    """Build the transport-level exception the ER client raises when the ER
    signals a genuine JSON-RPC cancellation (code == REQUEST_CANCELLED)."""
    return finecode_jsonrpc.ErrorOnRequest(
        error=ResponseError(code=finecode_jsonrpc.REQUEST_CANCELLED, message=message)
    )


def make_error_on_request(code: int, message: str = "boom") -> finecode_jsonrpc.ErrorOnRequest:
    """Build a transport-level exception carrying an arbitrary (non-cancellation) code."""
    return finecode_jsonrpc.ErrorOnRequest(error=ResponseError(code=code, message=message))


def make_running_runner(
    *,
    working_dir_path: Path,
    env_name: str = "test_env",
    client: typing.Any | None = None,
) -> runner_client.ExtensionRunnerInfo:
    """Build an already-RUNNING ``ExtensionRunnerInfo``.

    Register it in a ``WorkspaceContext`` (see :func:`make_workspace_context`)
    so ``runner_manager.get_or_start_runner`` finds it immediately without
    starting a real subprocess.
    """
    runner = runner_client.ExtensionRunnerInfo(
        working_dir_path=working_dir_path,
        env_name=env_name,
        status=domain.ExtensionRunnerStatus.RUNNING,
        client=client if client is not None else FakeErClient(),
    )
    runner.initialized_event.set()
    return runner


def make_single_action_project(
    *,
    dir_path: Path,
    action_name: str,
    handler_env: str = "test_env",
    action_source: str = "test.actions.TestAction",
    handler_source: str = "test.handlers.TestHandler",
) -> domain.ResolvedProject:
    """Build a minimal resolved project with a single action and single
    handler — just enough for the single-env dispatch path in
    ``proxy_utils.run_action``.
    """
    handler = domain.ActionHandler(
        name="test_handler",
        source=handler_source,
        config={},
        env=handler_env,
        dependencies=[],
    )
    action = domain.Action(
        name=action_name,
        source=action_source,
        handlers=[handler],
        config={},
    )
    collected = domain.CollectedProject(
        name="test_project",
        dir_path=dir_path,
        def_path=dir_path / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={
            handler_env: domain.EnvConfig(
                runner_config=domain.RunnerConfig(debug=False)
            )
        },
        actions=[action],
        services=[],
        action_handler_configs={},
    )
    return domain.ResolvedProject.from_collected(collected)


def make_workspace_context(
    *,
    project: domain.Project,
    runner: runner_client.ExtensionRunnerInfo,
    env_name: str = "test_env",
) -> context.WorkspaceContext:
    """Build a ``WorkspaceContext`` with *project* and *runner* pre-registered
    so ``runner_manager.get_or_start_runner`` finds the runner immediately."""
    ws_context = context.WorkspaceContext(ws_dirs_paths=[project.dir_path])
    ws_context.ws_projects[project.dir_path] = project
    ws_context.ws_projects_extension_runners[project.dir_path] = {env_name: runner}
    return ws_context
