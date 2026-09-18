"""How the shared WM server is spawned, and with which of the caller's settings."""

from __future__ import annotations

import pathlib
import subprocess
import typing

import click.testing
import pytest

from finecode.wm_server import cli as wm_cli
from finecode.wm_server import wm_lifecycle


class _RecordedPopen:
    """Stand-in for ``subprocess.Popen`` that records how it was called."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, typing.Any]]] = []

    def __call__(self, command: list[str], **kwargs: typing.Any) -> object:
        self.calls.append((command, kwargs))
        return object()


@pytest.fixture
def spawned(monkeypatch, tmp_path) -> _RecordedPopen:
    """Record the spawn against a temp cache dir, with no server listening.

    The liveness check answers False once and True after, so the readiness poll
    returns immediately instead of waiting out its deadline.
    """
    monkeypatch.setattr(wm_lifecycle, "_cache_dir", lambda: tmp_path)
    answers = iter([False])
    monkeypatch.setattr(wm_lifecycle, "is_running", lambda: next(answers, True))
    recorded = _RecordedPopen()
    monkeypatch.setattr(subprocess, "Popen", recorded)
    return recorded


def test_shared_server_is_spawned_in_its_own_session(spawned, tmp_path) -> None:
    """The shared server outlives whichever client happened to start it.

    Sharing that client's process group would mean sharing its signals: a
    Ctrl-C in the terminal that started the server, or the exit of the
    short-lived container-start script that started it, would take it down.
    """
    wm_lifecycle.ensure_running(tmp_path)

    assert len(spawned.calls) == 1
    _, kwargs = spawned.calls[0]
    assert kwargs["start_new_session"] is True


def test_keep_alive_is_not_passed_by_default(spawned, tmp_path) -> None:
    """Keep-alive is asked for explicitly, never inherited.

    An ambient setting would also reach the dedicated servers started per
    command, and a workspace would accumulate servers that never stop.
    """
    wm_lifecycle.ensure_running(tmp_path)

    command, _ = spawned.calls[0]
    assert "--keep-alive" not in command


def test_keep_alive_is_forwarded_when_asked(spawned, tmp_path) -> None:
    wm_lifecycle.ensure_running(tmp_path, keep_alive=True)

    command, _ = spawned.calls[0]
    assert "--keep-alive" in command


def test_server_settings_reach_the_spawned_server(spawned, tmp_path) -> None:
    """Settings the caller chose are the settings the server actually runs with.

    They are consumed by the spawned process, not by the caller, so silently
    dropping one leaves a server that ignores what it was asked for — a WAL the
    operator believes is recording, or a lifetime they believe was extended.
    """
    wm_lifecycle.ensure_running(tmp_path, disconnect_timeout=300, wal_enabled=True)

    command, _ = spawned.calls[0]
    assert "--disconnect-timeout=300" in command
    assert "--wal" in command


def test_defaults_are_left_to_the_spawned_server(spawned, tmp_path) -> None:
    """Unasked-for settings are not forced on the server it starts.

    The server resolves its own defaults from config and the environment; a
    caller that passed nothing must not override that with its own.
    """
    wm_lifecycle.ensure_running(tmp_path)

    command, _ = spawned.calls[0]
    assert not any(part.startswith("--disconnect-timeout") for part in command)
    assert "--wal" not in command


def test_nothing_is_spawned_when_a_server_is_already_running(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(wm_lifecycle, "_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(wm_lifecycle, "is_running", lambda: True)
    recorded = _RecordedPopen()
    monkeypatch.setattr(subprocess, "Popen", recorded)

    wm_lifecycle.ensure_running(tmp_path, keep_alive=True)

    assert recorded.calls == []


def test_detach_is_refused_with_port_file(tmp_path: pathlib.Path) -> None:
    """Asking for a dedicated instance in the background is refused, not guessed.

    The two requests contradict each other, and the operator finds out at the
    prompt rather than from a server that quietly ignored half of them.
    """
    result = click.testing.CliRunner().invoke(
        wm_cli.start_wm_server,
        ["--detach", "--port-file", str(tmp_path / "wm_port")],
    )

    assert result.exit_code != 0
    assert "--detach cannot be combined with --port-file" in result.output


def test_detach_passes_on_the_server_settings_it_was_given(monkeypatch) -> None:
    """A backgrounded server is configured the way the command line asked.

    Nothing in the foreground consumes these settings, so an operator running
    the detached form has only the flags they typed to go by.
    """
    recorded: dict[str, typing.Any] = {}
    monkeypatch.setattr(
        wm_lifecycle,
        "ensure_running",
        lambda workdir, **kwargs: recorded.update(kwargs),
    )
    monkeypatch.setattr(wm_lifecycle, "is_running", lambda: True)

    result = click.testing.CliRunner().invoke(
        wm_cli.start_wm_server,
        ["--detach", "--keep-alive", "--wal", "--disconnect-timeout", "300"],
    )

    assert result.exit_code == 0, result.output
    assert recorded["keep_alive"] is True
    assert recorded["wal_enabled"] is True
    assert recorded["disconnect_timeout"] == 300
