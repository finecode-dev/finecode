"""`dump-config` forwards `--no-format` as `format_output: false` on the action payload.

The env-prep dump and the user-facing dump are different things: uv handlers dump to a
private temp dir unformatted, so the only formatted `finecode_config_dump/` writes come
from `dump-config`. The escape hatch for a formatter that cannot run is `--no-format` on
this command — and it must reach the action as `format_output: false`, not as an absent
key (absent means "format, please").
"""

from __future__ import annotations

import pathlib

import pytest

from finecode.cli_app.commands import dump_config_cmd


class _FakeApiClient:
    def __init__(self, projects: list[dict]) -> None:
        self._projects = projects
        self.run_actions: list[dict] = []
        self._closed = False

    async def connect(self, host: str, port: int) -> None: ...

    def on_notification(self, method: str, handler: object) -> None: ...

    async def add_dir(self, dir_path: pathlib.Path) -> None: ...

    async def list_projects(self) -> list[dict]:
        return self._projects

    async def get_project_raw_config(self, _project: str) -> dict:
        return {"tool": {}}

    async def run_action(
        self, action_source: str, project: str, params: dict, options: dict
    ) -> dict:
        self.run_actions.append(
            {
                "action_source": action_source,
                "project": project,
                "params": params,
                "options": options,
            }
        )
        return {}

    async def close(self) -> None:
        self._closed = True


@pytest.fixture
def fake_server(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[_FakeApiClient, dict]:
    """Run `dump_config` against a fake WM client, with a fake port file that
    `start_own_server` pretends to produce."""
    projects = [{"name": "proj", "path": str(tmp_path)}]
    client = _FakeApiClient(projects)
    port_file = tmp_path / "port.json"

    def _start_own_server(
        _workdir: pathlib.Path, log_level: str = "INFO"
    ) -> pathlib.Path:
        _ = log_level
        port_file.write_text("34303")
        return port_file

    async def _wait_ready(_port_file_path: pathlib.Path) -> int:
        return 34303

    monkeypatch.setattr(dump_config_cmd, "ApiClient", lambda: client)
    monkeypatch.setattr(
        dump_config_cmd.wm_lifecycle, "start_own_server", _start_own_server
    )
    monkeypatch.setattr(
        dump_config_cmd.wm_lifecycle, "wait_until_ready_from_file", _wait_ready
    )
    return client, {"cwd": tmp_path, "project_name": "proj"}


async def test_no_format_maps_to_format_output_false(
    fake_server: tuple[_FakeApiClient, dict],
) -> None:
    """`format_output=False` sends `format_output: false` on the payload, so
    the `dump_config_format` handler skips its formatter dispatch and the dump
    is written as rendered."""
    client, base = fake_server

    await dump_config_cmd.dump_config(
        workdir_path=base["cwd"],
        project_name=base["project_name"],
        format_output=False,
    )

    assert len(client.run_actions) == 1
    assert client.run_actions[0]["params"]["format_output"] is False


async def test_default_omits_format_output_from_the_payload(
    fake_server: tuple[_FakeApiClient, dict],
) -> None:
    """By default the payload carries no `format_output` key: the action's own
    default (format) applies, and the payload is otherwise unchanged."""
    client, base = fake_server

    await dump_config_cmd.dump_config(
        workdir_path=base["cwd"],
        project_name=base["project_name"],
        format_output=True,
    )

    assert len(client.run_actions) == 1
    assert "format_output" not in client.run_actions[0]["params"]
