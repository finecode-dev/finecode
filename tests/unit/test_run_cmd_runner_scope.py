"""The CLI attaches lazily and never starts runners itself.

The WM owns runner startup (D3): ``run`` must pass ``start_runners=False`` to
``add_dir`` in both own- and shared-server modes, list actions through
``list_actions`` (names + project paths, so the WM's resolution gate
serves exactly the run's needs), and surface resolution failures as
``RunFailed`` — a raw API error would be printed as "Unexpected error" with
exit code 2 instead of the named failure and exit code 1.
"""

from __future__ import annotations

import pathlib
import typing

import pytest

from finecode.cli_app.commands import run_cmd
from finecode.cli_app.commands.run_cmd import RunFailed
from finecode.wm_client import ActionListing, ApiServerError


class _FakeApiClient:
    """Scriptable stand-in for ``ApiClient`` covering everything ``run_actions``
    touches, recording the calls that decide runner scope."""

    def __init__(
        self,
        actions: list[dict],
        workdir_path: pathlib.Path,
        unresolved_projects: list[dict] | None = None,
    ) -> None:
        self._actions = actions
        self._workdir_path = workdir_path
        self._unresolved_projects = unresolved_projects or []
        self.add_dir_calls: list[tuple[pathlib.Path, bool]] = []
        self.list_actions_calls: list[dict] = []
        self.run_batch_projects_calls: list[list[str] | None] = []
        self.closed = False

    def configure_reconnect(
        self,
        policy: object,
        on_reattach: typing.Callable[[], typing.Awaitable[object]] | None = None,
        **kwargs: object,
    ) -> None:
        self._on_reattach = on_reattach

    def on_notification(self, method: str, handler: object) -> None: ...

    def on_request(self, method: str, handler: object) -> None: ...

    async def subscribe_logs(self, min_level: str) -> None: ...

    async def set_config_overrides(
        self, handler_overrides: object, service_overrides: object
    ) -> None: ...

    async def connect(self, host: str, port: int, **kwargs: object) -> None:
        # Mirrors the real client: ``connect`` runs the attach session, which
        # is what calls ``add_dir``.
        if getattr(self, "_on_reattach", None) is not None:
            await self._on_reattach(first_connect=True)

    async def add_dir(
        self, dir_path: pathlib.Path, start_runners: bool = False
    ) -> dict:
        self.add_dir_calls.append((dir_path, start_runners))
        return {"projects": []}

    async def list_projects(self) -> list[dict]:
        return [{"name": "a", "path": str(self._workdir_path)}]

    async def list_actions(
        self,
        *,
        project: str | None = None,
        names: list[str] | None = None,
        projects: list[str] | None = None,
    ) -> ActionListing:
        self.list_actions_calls.append(
            {"project": project, "names": names, "projects": projects}
        )
        return ActionListing(
            actions=self._actions,
            unresolved_projects=self._unresolved_projects,
        )

    async def start_runners(
        self, projects: list[str] | None = None, **kwargs: object
    ) -> None:
        raise AssertionError("the CLI must never ask the WM to start runners")

    async def get_payload_schemas(
        self,
        project: str,
        action_sources: list[str],
        *,
        start_runners: bool = False,
        run_options: dict | None = None,
    ) -> dict:
        return {}

    async def run_batch(
        self,
        action_sources: list[str],
        projects: list[str] | None = None,
        **kwargs: object,
    ) -> dict:
        self.run_batch_projects_calls.append(projects)
        return {"results": {}, "returnCode": 0}

    async def close(self) -> None:
        self.closed = True


async def _run(
    actions: list[dict],
    *,
    names: list[str],
    monkeypatch: pytest.MonkeyPatch,
    own_server: bool = True,
    projects_names: list[str] | None = None,
    unresolved_projects: list[dict] | None = None,
    workdir_path: pathlib.Path,
) -> _FakeApiClient:
    async def _fake_wait_ready(*a: object, **k: object) -> int:
        return 1234

    client = _FakeApiClient(
        actions, workdir_path=workdir_path, unresolved_projects=unresolved_projects
    )
    monkeypatch.setattr(
        run_cmd.wm_lifecycle,
        "start_own_server",
        lambda *a, **k: pathlib.Path("/tmp/fake.finecode_port"),
    )
    monkeypatch.setattr(
        run_cmd.wm_lifecycle,
        "wait_until_ready_from_file",
        _fake_wait_ready,
    )
    if not own_server:
        monkeypatch.setattr(
            run_cmd.wm_lifecycle,
            "ensure_running",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            run_cmd.wm_lifecycle,
            "wait_until_ready",
            _fake_wait_ready,
        )
    monkeypatch.setattr(run_cmd.ApiClient, "__new__", lambda cls: client)
    await run_cmd.run_actions(
        workdir_path=workdir_path,
        projects_names=projects_names,
        actions=names,
        action_payload={},
        raw_action_payload={},
        concurrently=False,
        handler_config_overrides=None,
        service_config_overrides=None,
        save_results=False,
        map_payload_fields=None,
        own_server=own_server,
        log_level="INFO",
    )
    return client


def _action(name: str, source: str, scope: str, project: str = "") -> dict:
    return {
        "name": name,
        "source": source,
        "scope": scope,
        "project": project,
        "handlers": [],
    }


async def test_own_server_attaches_lazily_and_lists_by_name(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An own-server run attaches without starting runners and lists the exact
    requested names through ``list_actions``."""
    client = await _run(
        [_action("lint", "pkg.LintAction", "project")],
        names=["lint"],
        monkeypatch=monkeypatch,
        workdir_path=tmp_path,
    )

    assert client.add_dir_calls == [(tmp_path, False)]
    assert client.list_actions_calls == [
        {"project": None, "names": ["lint"], "projects": None}
    ]
    assert client.run_batch_projects_calls == [None]
    assert client.closed


async def test_shared_server_attaches_lazily_too(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shared-server run attaches just as lazily: the WM decides what to
    start, and the run lists the exact requested names."""
    client = await _run(
        [_action("lint", "pkg.LintAction", "project")],
        names=["lint"],
        monkeypatch=monkeypatch,
        own_server=False,
        workdir_path=tmp_path,
    )

    assert client.add_dir_calls == [(tmp_path, False)]
    assert client.list_actions_calls == [
        {"project": None, "names": ["lint"], "projects": None}
    ]
    assert client.closed


async def test_list_actions_error_becomes_run_failed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolution failure during ``list_actions`` surfaces as
    ``RunFailed`` with the named message — not "Unexpected error" (AC13)."""

    async def _raising_listing(self: _FakeApiClient, **kwargs: object) -> object:
        raise ApiServerError(-32603, "project '/x' failed to resolve")

    monkeypatch.setattr(_FakeApiClient, "list_actions", _raising_listing)

    with pytest.raises(RunFailed) as excinfo:
        await _run(
            [_action("lint", "pkg.LintAction", "project")],
            names=["lint"],
            monkeypatch=monkeypatch,
            workdir_path=tmp_path,
        )
    assert "failed to resolve" in str(excinfo.value)


async def test_unresolved_projects_become_run_failed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listing that resolved some projects but not a sibling fails the run,
    naming the project and its reason (AC13)."""
    with pytest.raises(RunFailed) as excinfo:
        await _run(
            [_action("lint", "pkg.LintAction", "project")],
            names=["lint"],
            monkeypatch=monkeypatch,
            workdir_path=tmp_path,
            unresolved_projects=[
                {
                    "project": str(tmp_path / "sibling"),
                    "error": "venv missing",
                }
            ],
        )
    message = str(excinfo.value)
    assert str(tmp_path / "sibling") in message
    assert "venv missing" in message


async def test_project_filter_passes_project_paths_to_listing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--project`` paths reach ``list_actions`` so the WM resolves
    exactly those projects."""
    client = await _run(
        [_action("lint", "pkg.LintAction", "project", project=str(tmp_path))],
        names=["lint"],
        monkeypatch=monkeypatch,
        projects_names=["a"],
        workdir_path=tmp_path,
    )

    assert client.list_actions_calls == [
        {
            "project": None,
            "names": ["lint"],
            "projects": [str(tmp_path)],
        }
    ]
    assert client.run_batch_projects_calls == [[str(tmp_path)]]
    assert client.closed
