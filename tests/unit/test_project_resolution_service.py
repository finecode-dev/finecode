"""The on-demand project-resolution gate (B rules 1-5, 7).

The WM resolves a project at most once at a time, waits on any resolution
already in flight, and remembers attributable failures until the project's
configuration is reloaded.  Every test builds projects through
``tests/unit/resolution_fake.SwappingResolver``, which replaces the project
object on resolution like the real code does — a gate that returned a stale
pre-resolution reference fails.
"""

from __future__ import annotations

import asyncio
import pathlib
import typing

import pytest
import resolution_fake
from resolution_fake import SwappingResolver, make_preset_action

from finecode.wm_server import context, domain
from finecode.wm_server.services import project_resolution_service as prs


async def _pump_until(predicate: typing.Callable[[], bool], *, tries: int = 50) -> None:
    """Yield to the event loop until *predicate* holds (or the budget runs out)."""
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


def _make_collected(path: pathlib.Path) -> domain.CollectedProject:
    return domain.CollectedProject(
        name=path.name,
        dir_path=path,
        def_path=path / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={},
        actions=[],
        services=[],
        action_handler_configs={},
    )


def _make_plain(path: pathlib.Path, status: domain.ProjectStatus) -> domain.Project:
    return domain.Project(
        name=path.name, dir_path=path, def_path=path / "pyproject.toml", status=status
    )


def _make_context(
    projects: list[domain.Project],
) -> context.WorkspaceContext:
    ws_context = context.WorkspaceContext(ws_dirs_paths=[p.dir_path for p in projects])
    for project in projects:
        ws_context.ws_projects[project.dir_path] = project
    return ws_context


_WORKSPACE_ACTION = make_preset_action(
    name="root_only_action",
    source="fine_inspect_code.InspectCodeAction",
    scope=domain.ActionScope.WORKSPACE,
)


async def test_already_resolved_projects_trigger_no_call(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolved project is served from the context; the gate does not start
    anything for it."""
    a = tmp_path / "a"
    resolved = domain.ResolvedProject.from_collected(_make_collected(a))
    ws_context = _make_context([resolved])
    resolver = SwappingResolver(ws_context, monkeypatch)

    outcome = await prs.ensure_projects_resolved([a], ws_context)

    assert resolver.calls == []
    assert outcome.failed == {}
    assert outcome.require([a]) == [resolved]


async def test_batch_carries_exactly_the_unresolved_config_valid_projects(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit targets resolve in one batch; a NO_FINECODE target is a failure
    with a reason, and the resolved objects are the fresh post-resolution ones,
    not the pre-call references."""
    a, b, nf = tmp_path / "a", tmp_path / "b", tmp_path / "no_finecode"
    ws_context = _make_context(
        [
            _make_collected(a),
            _make_collected(b),
            _make_plain(nf, domain.ProjectStatus.NO_FINECODE),
        ]
    )
    resolver = SwappingResolver(
        ws_context, monkeypatch, preset_actions=[_WORKSPACE_ACTION]
    )

    pre_call = ws_context.ws_projects[a]
    outcome = await prs.ensure_projects_resolved([a, b, nf], ws_context)

    assert resolver.calls == [[a, b]]
    assert set(outcome.resolved) == {a, b}
    assert outcome.resolved[a] is not pre_call
    # The fresh object carries the preset-contributed action the raw one lacks.
    assert any(act.name == "root_only_action" for act in outcome.resolved[a].actions)
    assert nf in outcome.failed
    assert "NO_FINECODE" in outcome.failed[nf]


async def test_explicit_invalid_target_fails_and_require_names_missing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named CONFIG_INVALID project is a named failure, never a silent skip;
    ``require`` on a path in neither map raises naming that path."""
    bad = tmp_path / "bad"
    ws_context = _make_context([_make_plain(bad, domain.ProjectStatus.CONFIG_INVALID)])
    resolver = SwappingResolver(ws_context, monkeypatch)

    outcome = await prs.ensure_projects_resolved([bad], ws_context)

    assert resolver.calls == []
    assert bad in outcome.failed
    assert "CONFIG_INVALID" in outcome.failed[bad]
    unknown = tmp_path / "unknown"
    with pytest.raises(prs.ProjectResolutionFailed) as excinfo:
        outcome.require([bad, unknown])
    assert str(unknown) in str(excinfo.value)


async def test_concurrent_calls_resolve_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two concurrent requests for one project make one resolution and both get
    the same fresh object (AC12)."""
    a = tmp_path / "a"
    ws_context = _make_context([_make_collected(a)])
    block = asyncio.Event()
    resolver = SwappingResolver(ws_context, monkeypatch, block=block)

    first = asyncio.create_task(prs.ensure_projects_resolved([a], ws_context))
    await _pump_until(lambda: len(resolver.calls) == 1)
    second = asyncio.create_task(prs.ensure_projects_resolved([a], ws_context))
    await asyncio.sleep(0)
    block.set()
    outcome1 = await first
    outcome2 = await second

    assert resolver.calls == [[a]]
    assert outcome1.resolved[a] is outcome2.resolved[a]
    assert isinstance(outcome1.resolved[a], domain.ResolvedProject)


async def test_call_waits_even_after_the_project_is_stored(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second request must not read a project whose resolution has stored the
    ResolvedProject but has not finished configuring its runner: it waits for
    the whole in-flight resolution (AC12)."""
    a = tmp_path / "a"
    ws_context = _make_context([_make_collected(a)])
    block = asyncio.Event()
    resolver = SwappingResolver(ws_context, monkeypatch, block=block)

    first = asyncio.create_task(prs.ensure_projects_resolved([a], ws_context))
    # The fake stores the new object first, then blocks before returning.
    await _pump_until(
        lambda: isinstance(ws_context.ws_projects[a], domain.ResolvedProject)
    )

    second = asyncio.create_task(prs.ensure_projects_resolved([a], ws_context))
    await asyncio.sleep(0)
    assert not second.done()

    block.set()
    await first
    await second


async def test_attributable_failure_is_remembered_until_reload(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure pinned on one project of a batch is served to the next request
    without a new resolution, with the reload hint; its siblings resolved
    (AC11)."""
    a, b, x = tmp_path / "a", tmp_path / "b", tmp_path / "x"
    ws_context = _make_context(
        [_make_collected(a), _make_collected(b), _make_collected(x)]
    )
    resolver = SwappingResolver(
        ws_context, monkeypatch, fail_paths={x}, fail_message="venv missing"
    )

    outcome = await prs.ensure_projects_resolved([a, b, x], ws_context)

    assert x in outcome.failed
    assert outcome.failed[x] == "venv missing"
    assert set(outcome.resolved) == {a, b}

    second = await prs.ensure_projects_resolved([x], ws_context)
    assert resolver.calls == [[a, b, x]]
    assert second.failed[x].endswith(prs._RETRY_HINT)
    assert second.resolved == {}


async def test_unattributable_failure_is_not_remembered(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch failure that cannot be pinned on any project is reported to this
    call but retried by the next (AC11)."""
    a, b, x = tmp_path / "a", tmp_path / "b", tmp_path / "x"
    ws_context = _make_context(
        [_make_collected(a), _make_collected(b), _make_collected(x)]
    )
    resolver = SwappingResolver(
        ws_context,
        monkeypatch,
        fail_paths={a, b, x},
        raise_bare=True,
        fail_message="transport broke",
    )

    outcome = await prs.ensure_projects_resolved([a, b, x], ws_context)

    assert set(outcome.failed) == {a, b, x}
    assert "batch including this project failed" in outcome.failed[a]

    second = await prs.ensure_projects_resolved([a, b, x], ws_context)
    assert len(resolver.calls) == 2


async def test_one_path_batch_failure_is_remembered(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single-project resolution that fails attributes the failure to that
    project and remembers it (AC11)."""
    a = tmp_path / "a"
    ws_context = _make_context([_make_collected(a)])
    resolver = SwappingResolver(
        ws_context, monkeypatch, fail_paths={a}, fail_message="venv missing"
    )

    outcome = await prs.ensure_projects_resolved([a], ws_context)

    assert a in outcome.failed
    assert outcome.failed[a] == "venv missing"
    assert ws_context.project_resolution_failures[a].endswith(prs._RETRY_HINT)


async def test_cancelling_one_waiter_does_not_cancel_shared_resolution(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling a caller (a disconnected client) does not cancel the
    resolution its peers are waiting on: the other caller still gets the
    result."""
    a = tmp_path / "a"
    ws_context = _make_context([_make_collected(a)])
    block = asyncio.Event()
    resolver = SwappingResolver(ws_context, monkeypatch, block=block)

    first = asyncio.create_task(prs.ensure_projects_resolved([a], ws_context))
    await _pump_until(lambda: len(resolver.calls) == 1)
    second = asyncio.create_task(prs.ensure_projects_resolved([a], ws_context))
    await asyncio.sleep(0)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    block.set()
    outcome = await second
    assert isinstance(outcome.resolved[a], domain.ResolvedProject)
    assert resolver.calls == [[a]]


def test_projects_failed_message_is_str() -> None:
    """``str(exc)`` carries the message — a generic handler that formats
    ``str(exc)`` must report the named failure."""
    msg = "project '/x' has status CONFIG_INVALID"
    exc = prs.ProjectResolutionFailed(msg)
    assert str(exc) == msg
    assert exc.message == msg
