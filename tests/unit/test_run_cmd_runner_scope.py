"""The CLI attaches lazily and never starts runners itself.

The WM owns runner startup (D3): ``run`` must pass ``start_runners=False`` to
``add_dir`` in both own- and shared-server modes, list actions through
``list_actions`` (names + project paths, so the WM's resolution gate
serves exactly the run's needs), and surface resolution failures as
``RunFailed`` — a raw API error would be printed as "Unexpected error" with
exit code 2 instead of the named failure and exit code 1.
"""

from __future__ import annotations

import asyncio
import pathlib
import typing

import pytest

from finecode.cli_app.commands import run_cmd
from finecode.cli_app.commands.run_cmd import RunFailed
from finecode.wm_client import ActionListing, ApiServerError


@pytest.fixture(autouse=True)
def _without_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler-installation tests assume no CI; D13 mutes it otherwise."""
    monkeypatch.delenv("CI", raising=False)


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
        self.notification_handlers: dict[
            str,
            typing.Callable[[dict[str, typing.Any]], typing.Awaitable[None]],
        ] = {}
        self.partial_results_to_send: list[dict[str, typing.Any]] = []
        self.run_batch_result: dict[str, typing.Any] = {
            "results": {},
            "returnCode": 0,
        }
        self.run_batch_error: ApiServerError | None = None
        self.closed = False

    def configure_reconnect(
        self,
        policy: object,
        on_reattach: typing.Callable[[], typing.Awaitable[object]] | None = None,
        **kwargs: object,
    ) -> None:
        self._on_reattach = on_reattach

    def on_notification(
        self,
        method: str,
        handler: typing.Callable[[dict[str, typing.Any]], typing.Awaitable[None]],
    ) -> None:
        self.notification_handlers[method] = handler

    def on_request(self, method: str, handler: object, **kwargs: object) -> None: ...

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
        handler = self.notification_handlers.get("actions/partialResult")
        if handler is not None:
            for partial in self.partial_results_to_send:
                await handler({"value": partial})
                await asyncio.sleep(0)
        if self.run_batch_error is not None:
            raise self.run_batch_error
        return self.run_batch_result

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
    client: _FakeApiClient | None = None,
    workdir_path: pathlib.Path,
) -> _FakeApiClient:
    async def _fake_wait_ready(*a: object, **k: object) -> int:
        return 1234

    if client is None:
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


async def test_partial_results_are_previewed_on_a_terminal_stderr_in_arrival_order(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """People can watch live output without mixing previews into piped stdout."""
    client = _FakeApiClient(
        [_action("run agent task", "pkg.RunAgentTask", "project")], tmp_path
    )
    client.partial_results_to_send = [
        {
            "project": str(tmp_path),
            "results": {
                "pkg.RunAgentTask": {
                    "resultByFormat": {"string": "\x1b[31mfirst snapshot\x1b[0m"},
                    "returnCode": 0,
                }
            },
        },
        {
            "project": str(tmp_path),
            "interpreter": "cpython@3.14",
            "results": {
                "pkg.RunAgentTask": {
                    "resultByFormat": {"string": "second snapshot"},
                    "returnCode": 0,
                }
            },
        },
    ]

    monkeypatch.setattr(run_cmd.sys.stderr, "isatty", lambda: True)
    await _run(
        [_action("run agent task", "pkg.RunAgentTask", "project")],
        names=["run agent task"],
        monkeypatch=monkeypatch,
        client=client,
        workdir_path=tmp_path,
    )
    await client.notification_handlers["actions/partialResult"](
        {
            "value": {
                "project": str(tmp_path),
                "results": {
                    "pkg.RunAgentTask": {
                        "resultByFormat": {"string": "late snapshot"},
                        "returnCode": 0,
                    }
                },
            }
        }
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.index("first snapshot") < captured.err.index("second snapshot")
    assert "late snapshot" not in captured.err
    assert "\x1b[31mfirst snapshot\x1b[0m" in captured.err
    assert "-- preview: run agent task (" + str(tmp_path) + ") --" in captured.err
    assert (
        "-- preview: run agent task [cpython@3.14] (" + str(tmp_path) + ") --"
        in captured.err
    )
    assert "\r\x1b[K" in captured.err


async def test_partial_results_are_not_previewed_when_stderr_is_not_a_terminal(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An agent's shell tool captures stderr into the model's context, so a
    piped run shows only the final result on stdout."""
    client = _FakeApiClient(
        [_action("run agent task", "pkg.RunAgentTask", "project")], tmp_path
    )
    client.partial_results_to_send = [
        {
            "project": str(tmp_path),
            "results": {
                "pkg.RunAgentTask": {
                    "resultByFormat": {"string": text},
                    "returnCode": 0,
                }
            },
        }
        for text in ("first snapshot", "first snapshot grows")
    ]
    client.run_batch_result = {
        "results": {
            str(tmp_path): {
                "pkg.RunAgentTask": {
                    "resultByFormat": {"string": "final answer"},
                    "returnCode": 0,
                }
            }
        },
        "returnCode": 0,
    }

    monkeypatch.setattr(run_cmd.sys.stderr, "isatty", lambda: False)
    await _run(
        [_action("run agent task", "pkg.RunAgentTask", "project")],
        names=["run agent task"],
        monkeypatch=monkeypatch,
        client=client,
        workdir_path=tmp_path,
    )

    captured = capsys.readouterr()
    assert "snapshot" not in captured.err
    assert "-- preview:" not in captured.err
    assert "final answer" in captured.out


async def test_cumulative_previews_print_only_the_new_text_under_one_label(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A snapshot that extends the previous one adds only its new part; a
    single project's stream is headed once, not before every snapshot."""
    client = _FakeApiClient(
        [_action("run agent task", "pkg.RunAgentTask", "project")], tmp_path
    )
    client.partial_results_to_send = [
        {
            "project": str(tmp_path),
            "results": {
                "pkg.RunAgentTask": {
                    "resultByFormat": {"string": text},
                    "returnCode": 0,
                }
            },
        }
        for text in ("one", "one two", "one two", "one two three")
    ]

    monkeypatch.setattr(run_cmd.sys.stderr, "isatty", lambda: True)
    await _run(
        [_action("run agent task", "pkg.RunAgentTask", "project")],
        names=["run agent task"],
        monkeypatch=monkeypatch,
        client=client,
        workdir_path=tmp_path,
    )

    err = capsys.readouterr().err
    label = "-- preview: run agent task (" + str(tmp_path) + ") --"
    assert err.count(label) == 1
    assert err.count("one") == 1
    assert err.index("one") < err.index(" two") < err.index(" three")


async def test_a_snapshot_that_does_not_extend_the_previous_one_is_printed_whole(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _FakeApiClient(
        [_action("run agent task", "pkg.RunAgentTask", "project")], tmp_path
    )
    client.partial_results_to_send = [
        {
            "project": str(tmp_path),
            "results": {
                "pkg.RunAgentTask": {
                    "resultByFormat": {"string": text},
                    "returnCode": 0,
                }
            },
        }
        for text in ("first draft", "second draft")
    ]

    monkeypatch.setattr(run_cmd.sys.stderr, "isatty", lambda: True)
    await _run(
        [_action("run agent task", "pkg.RunAgentTask", "project")],
        names=["run agent task"],
        monkeypatch=monkeypatch,
        client=client,
        workdir_path=tmp_path,
    )

    err = capsys.readouterr().err
    label = "-- preview: run agent task (" + str(tmp_path) + ") --"
    assert err.count(label) == 2
    assert "first draft" in err and "second draft" in err


async def test_preview_waiting_behind_a_question_is_discarded_when_run_finishes(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A preview held behind an open question is not printed after completion."""
    client = _FakeApiClient(
        [_action("run agent task", "pkg.RunAgentTask", "project")], tmp_path
    )
    client.partial_results_to_send = [
        {
            "project": str(tmp_path),
            "results": {
                "pkg.RunAgentTask": {
                    "resultByFormat": {"string": "waiting snapshot"},
                    "returnCode": 0,
                }
            },
        }
    ]

    class _HoldingPrompt:
        """A terminal question that is on screen for the whole run."""

        def __init__(self, *, prompt_idle: asyncio.Event, **_kwargs: object) -> None:
            prompt_idle.clear()

        async def ask(self, _request_id: int | str, _params: dict | None) -> dict:
            return {}

        async def on_withdrawn(self, _params: dict | None) -> None: ...

        def end_all_on_connection_lost(self) -> None: ...

    monkeypatch.setattr(run_cmd.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(run_cmd, "TerminalQuestions", _HoldingPrompt)
    monkeypatch.setattr(run_cmd.sys.stderr, "isatty", lambda: True)

    await _run(
        [_action("run agent task", "pkg.RunAgentTask", "project")],
        names=["run agent task"],
        monkeypatch=monkeypatch,
        client=client,
        workdir_path=tmp_path,
    )

    assert "waiting snapshot" not in capsys.readouterr().err


async def test_final_blocks_use_result_order_and_skip_failed_empty_slots(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Final text is stable in request order and a failed empty slot is harmless."""
    actions = [
        _action("agent", "pkg.Agent", "project"),
        _action("lint", "pkg.Lint", "project"),
        _action("failed", "pkg.Fail", "project"),
    ]
    client = _FakeApiClient(actions, tmp_path)
    client.run_batch_result = {
        "results": {
            "/project-b": {
                "pkg.Agent": {
                    "resultByFormat": {"string": "\x1b[31magent-b\x1b[0m\n"},
                    "returnCode": 0,
                },
                "pkg.Lint": {
                    "resultByFormat": {"string": "lint-b\n"},
                    "returnCode": 0,
                },
                "pkg.Fail": {"resultByFormat": {}, "returnCode": 1},
            },
            "/project-a": {
                "pkg.Lint": {
                    "resultByFormat": {"string": "lint-a\n"},
                    "returnCode": 0,
                }
            },
        },
        "returnCode": 1,
    }

    await _run(
        actions,
        names=["agent", "lint", "failed"],
        monkeypatch=monkeypatch,
        client=client,
        workdir_path=tmp_path,
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.index("/project-b") < captured.out.index("agent-b")
    assert captured.out.index("agent-b") < captured.out.index("/project-a")
    assert "lint-a" in captured.out
    assert "lint-b" in captured.out
    assert "pkg.Fail" not in captured.out
    assert "\x1b[31magent-b\x1b[0m" in captured.out
    assert captured.out.count("/project-b") == 2


async def test_final_blocks_suppress_workspace_project_header(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Workspace output has no repeated root path header."""
    actions = [_action("inspect", "pkg.Inspect", "workspace")]
    client = _FakeApiClient(actions, tmp_path)
    client.run_batch_result = {
        "results": {
            str(tmp_path): {
                "pkg.Inspect": {
                    "resultByFormat": {"string": "workspace result\n"},
                    "returnCode": 0,
                }
            }
        },
        "returnCode": 0,
    }

    await _run(
        actions,
        names=["inspect"],
        monkeypatch=monkeypatch,
        client=client,
        workdir_path=tmp_path,
    )

    assert capsys.readouterr().out == "workspace result\n"


async def test_run_batch_error_does_not_print_final_blocks(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A disconnected batch does not print incomplete final results."""
    actions = [_action("agent", "pkg.Agent", "project")]
    client = _FakeApiClient(actions, tmp_path)
    client.run_batch_error = ApiServerError(-32603, "WM disconnected")

    with pytest.raises(RunFailed, match="WM disconnected"):
        await _run(
            actions,
            names=["agent"],
            monkeypatch=monkeypatch,
            client=client,
            workdir_path=tmp_path,
        )

    assert capsys.readouterr().out == ""
