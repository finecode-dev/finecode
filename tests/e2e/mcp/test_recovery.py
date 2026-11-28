"""E2E tests for recovery over a real MCP session.

The MCP server is the surface PRD-0008 was written for: an assistant edits
FineCode while driving it, and everything it knows about the workspace came from
a tool list it fetched once. These drive a real MCP process over its stdio
protocol, against a real WM and a real extension runner.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_NEW_ACTION = """
[tool.finecode.action.lock_dependencies]
source = "fine_src_artifacts.LockDependenciesAction"

[[tool.finecode.action.lock_dependencies.handlers]]
name = "lock_dependencies_dispatch"
source = "fine_src_artifacts.LockDependenciesDispatchHandler"
env = "dev_workspace"
"""


class _McpSession:
    """A real MCP client speaking the stdio protocol (newline-delimited JSON)."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc = proc
        self._id = 0
        self._notifications: list[dict] = []

    def _send(self, message: dict) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(message) + "\n").encode())
        self._proc.stdin.flush()

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def request(self, method: str, params: dict | None = None, timeout: float = 120.0):
        self._id += 1
        request_id = self._id
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self._read_message(deadline)
            if message.get("id") == request_id:
                if "error" in message:
                    raise AssertionError(f"MCP error for {method}: {message['error']}")
                return message.get("result")
            if "id" not in message:
                self._notifications.append(message)
        raise AssertionError(f"No MCP response for {method} within {timeout}s")

    def _read_message(self, deadline: float) -> dict:
        assert self._proc.stdout is not None
        while time.monotonic() < deadline:
            line = self._proc.stdout.readline()
            if not line:
                raise AssertionError("MCP server closed its output")
            text = line.decode().strip()
            if text:
                return json.loads(text)
        raise AssertionError("Timed out reading from the MCP server")

    def notification_methods(self) -> list[str]:
        return [n.get("method") for n in self._notifications]

    def initialize(self) -> dict:
        result = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "e2e", "version": "1"},
            },
        )
        self.notify("notifications/initialized")
        return result

    def tool_names(self) -> set[str]:
        return {tool["name"] for tool in self.request("tools/list")["tools"]}

    def call(self, name: str, arguments: dict | None = None) -> dict:
        result = self.request(
            "tools/call", {"name": name, "arguments": arguments or {}}
        )
        return json.loads(result["content"][0]["text"])


@pytest.fixture
def mcp_session(workspace_dir_with_er, tmp_path):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "finecode",
            "start-mcp",
            "--workdir",
            str(workspace_dir_with_er),
            "--wm-port-file",
            str(tmp_path / "wm_port"),
        ],
        cwd=workspace_dir_with_er,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    def _forward_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            sys.stderr.buffer.write(line)
            sys.stderr.buffer.flush()

    threading.Thread(target=_forward_stderr, daemon=True).start()

    session = _McpSession(proc)
    try:
        yield session, Path(workspace_dir_with_er)
    finally:
        from tests.e2e.conftest import kill_group

        kill_group(proc)


def test_an_action_added_to_config_becomes_callable_without_reconnecting(
    mcp_session,
) -> None:
    """PRD-0008-AC4 — an action added to a project's configuration is offered and
    callable on the same MCP session, and the client is told its tool list changed.

    An assistant fetches the tool list once and has no reason to fetch it again.
    Without the notification the action it just configured is unreachable until
    the assistant is restarted — which is the session loss this PRD exists to
    prevent.
    """
    session, workspace_dir = mcp_session
    capabilities = session.initialize()["capabilities"]
    assert capabilities["tools"]["listChanged"] is True, (
        "a client may ignore the notification unless the capability was advertised"
    )

    assert "lock_dependencies" not in session.tool_names()

    pyproject = workspace_dir / "pyproject.toml"
    pyproject.write_text(pyproject.read_text() + _NEW_ACTION)

    result = session.call("reload_config", {"project": str(workspace_dir)})
    assert result["projects"][0]["status"] == "recovered", result
    assert result["projects"][0]["actionsAdded"] == ["lock_dependencies"]

    assert "notifications/tools/list_changed" in session.notification_methods()
    # Same session, same process: no reconnect happened on either side.
    assert "lock_dependencies" in session.tool_names()


def test_the_recovery_ladder_is_reachable_over_mcp(mcp_session) -> None:
    """PRD-0008-AC13 — the recovery operations an editor can perform are the same
    ones an assistant can perform, over its own surface.

    A ladder that exists on one surface only sends whoever is on the other one
    back to restarting their session, which is the workaround this replaces.
    """
    session, workspace_dir = mcp_session
    session.initialize()

    assert {
        "reload_action",
        "restart_runner",
        "reload_config",
        "restart_wm",
    } <= session.tool_names()

    # Each rung reaches the same WM method the LSP and CLI surfaces call, with
    # the same addressing.
    restarted = session.call(
        "restart_runner", {"project": str(workspace_dir), "env": "dev_workspace"}
    )
    assert [entry["status"] for entry in restarted["restarted"]] == ["RUNNING"]

    recovered = session.call("reload_config", {"project": str(workspace_dir)})
    assert recovered["projects"][0]["status"] == "recovered"
