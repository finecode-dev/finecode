"""Recovery from the CLI only means something against a shared workspace server.

Every other CLI command defaults to starting a private server for the duration of
the command, which is right for running an action and wrong for recovering one:
the private server would be started, recovered and thrown away, while the
workspace an editor or agent is actually using stayed stale — and the command
would report success.
"""

from __future__ import annotations

import pathlib

import pytest
from click.testing import CliRunner

from finecode import cli as finecode_cli
from finecode.cli_app.commands import recover_cmd

_RECOVERY_COMMANDS = {
    "reload-action": ["--action", "pkg.LintAction"],
    "restart-runner": ["--project", "/tmp/project"],
    "reload-config": ["--project", "/tmp/project"],
    "restart-wm": [],
}


@pytest.mark.parametrize("command_name", sorted(_RECOVERY_COMMANDS))
def test_recovery_command_refuses_dedicated_server_mode(command_name: str) -> None:
    """The command fails, names the mode as the reason, and does not recover
    anything."""
    command = finecode_cli.cli.commands[command_name]
    result = CliRunner().invoke(command, _RECOVERY_COMMANDS[command_name])

    assert result.exit_code == 1
    assert "--shared-server" in result.output


@pytest.mark.parametrize("command_name", sorted(_RECOVERY_COMMANDS))
def test_recovery_command_offers_the_shared_server_flag(command_name: str) -> None:
    """The flag the refusal names is one the command actually accepts.

    A message naming an option that does not exist would leave the caller with no
    way forward at all.
    """
    command = finecode_cli.cli.commands[command_name]
    flags = {opt for param in command.params for opt in param.opts}

    assert "--shared-server" in flags


async def test_connecting_does_not_start_the_server_it_would_recover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no shared server up, the command fails instead of starting one.

    Starting one here would satisfy ``--shared-server`` with a server the
    command itself created — the private-server outcome the flag exists to
    refuse, reached past the guard rather than through it.
    """
    started: list[object] = []
    monkeypatch.setattr(
        recover_cmd.wm_lifecycle, "running_port", lambda: None, raising=True
    )
    monkeypatch.setattr(
        recover_cmd.wm_lifecycle,
        "ensure_running",
        lambda *args, **kwargs: started.append(args),
        raising=True,
    )

    with pytest.raises(recover_cmd.RecoveryFailed) as exc_info:
        await recover_cmd._connected_client(pathlib.Path("/tmp/workspace"))

    assert started == []
    assert "no finecode workspace server is running" in exc_info.value.message.lower()


def test_the_guard_names_the_command_it_refused() -> None:
    """The reason states which command was refused, so a caller running several
    in a script can tell which one stopped."""
    with pytest.raises(recover_cmd.RecoveryFailed) as exc_info:
        recover_cmd.require_shared_server(own_server=True, command="reload-config")

    assert "reload-config" in exc_info.value.message

    # Shared-server mode passes through: the guard is about the mode, not the
    # command.
    recover_cmd.require_shared_server(own_server=False, command="reload-config")
