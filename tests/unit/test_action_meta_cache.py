from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

from finecode.wm_server import context, domain
from finecode.wm_server.runner import action_meta_dump
from finecode.wm_server.services import action_meta_cache

ENV = "dev_no_runtime"
SOURCE_A = "test.actions.ActionA"
SOURCE_B = "test.actions.ActionB"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _fingerprint(path: Path) -> dict:
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    try:
        ctime_ns = stat.st_ctime_ns
    except AttributeError:
        ctime_ns = stat.st_mtime_ns
    return {
        "path": str(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": ctime_ns,
        "sha256": digest.hexdigest(),
    }


def _make_venv(project_dir: Path, env_name: str, *, editable: bool = False) -> Path:
    venv = project_dir / ".venvs" / env_name
    if sys.platform == "win32":
        _write(venv / "Scripts" / "python.exe", "")
        site_packages = venv / "Lib" / "site-packages"
    else:
        _write(venv / "bin" / "python", "")
        site_packages = venv / "lib" / "python3.14" / "site-packages"
    dist_info = site_packages / "finecode_extension_runner-0.1.dist-info"
    _write(dist_info / "RECORD", "record-content\n")
    if editable:
        _write(
            dist_info / "direct_url.json", json.dumps({"dir_info": {"editable": True}})
        )
    else:
        _write(
            dist_info / "direct_url.json",
            json.dumps({"url": "https://example/wheel.whl"}),
        )
    return venv


def _make_project(project_dir: Path, sources: list[str]) -> domain.CollectedProject:
    actions = [
        domain.Action(
            name=f"a{i}",
            source=source,
            handlers=[
                domain.ActionHandler(
                    name="h", source="test.H", config={}, env=ENV, dependencies=[]
                )
            ],
            config={},
        )
        for i, source in enumerate(sources)
    ]
    return domain.CollectedProject(
        name="p",
        dir_path=project_dir,
        def_path=project_dir / "pyproject.toml",
        status=domain.ProjectStatus.CONFIG_VALID,
        env_configs={},
        actions=actions,
        services=[],
        action_handler_configs={},
    )


def _meta(source: str) -> dict:
    return {
        "canonical_source": source.replace("test.actions", "test.impl"),
        "runs_concurrently": False,
        "scope": "project",
        "parentActionSource": None,
        "language": "python",
        "fileLoc": "test/impl.py:1",
    }


def _document(project_dir: Path, venv: Path, sources: list[str], stamped: Path) -> dict:
    site_packages = action_meta_cache._site_packages_dir(venv)
    assert site_packages is not None
    pth_fps = []
    try:
        for pth in sorted(site_packages.glob("*.pth")):
            if pth.is_file():
                pth_fps.append(_fingerprint(pth))
    except OSError:
        pth_fps = []
    return {
        "format": 1,
        "startedNs": time.time_ns() + 10_000_000_000,
        "actionMetaFile": _fingerprint(stamped),
        "header": {
            "sysPath": list(sys.path),
            "pythonpath": os.environ.get("PYTHONPATH"),
            "sitePackages": {
                "path": str(site_packages),
                "dir_mtime_ns": site_packages.stat().st_mtime_ns,
                "pth": pth_fps,
            },
            "headerStable": True,
        },
        "entries": {
            source: {
                "meta": _meta(source),
                "files": [_fingerprint(stamped)],
                "dirs": [],
            }
            for source in sources
        },
        "failures": {},
    }


@pytest.fixture()
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    stamped = project_dir / "action.py"
    _write(stamped, "hello\n")
    venv = _make_venv(project_dir, ENV)
    ws_context = context.WorkspaceContext(ws_dirs_paths=[project_dir])
    calls: list = []

    async def _fake_run_dump(
        python_cmd: str, proj_dir: Path, sources: list[str], *, attempt_timeout: float
    ):
        calls.append(list(sources))
        return action_meta_dump.DumpOutcome(
            kind="ok", document=_document(project_dir, venv, sources, stamped)
        )

    monkeypatch.setattr(action_meta_dump, "run_dump", _fake_run_dump)
    return project_dir, stamped, venv, ws_context, calls


async def test_hit_applies_without_spawning(
    setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid cache file answers without spawning, so warm runs cost only stats."""
    project_dir, stamped, venv, ws_context, calls = setup
    project = _make_project(project_dir, [SOURCE_A])
    ws_context.ws_action_schemas[project_dir] = {"a": None}

    first = await action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
    assert SOURCE_A in first.metas
    assert calls, "expected the first lookup to dump"

    async def _fail(*args, **kwargs):
        raise AssertionError("must not dump on a hit")

    monkeypatch.setattr(action_meta_dump, "run_dump", _fail)
    project2 = _make_project(project_dir, [SOURCE_A])
    ws_context.ws_action_schemas[project_dir] = {"a": None}
    result = await action_meta_cache.resolve_unresolved(project2, ws_context)

    assert result == {}
    action = project2.actions[0]
    assert action.canonical_source == _meta(SOURCE_A)["canonical_source"]
    assert action.meta_from_cache is True
    assert project_dir not in ws_context.ws_action_schemas


async def test_invalidation_misses_then_dumps_once(setup) -> None:
    """Any stamp the file cannot confirm refills from the env, exactly once per cause."""
    project_dir, stamped, venv, ws_context, calls = setup
    project = _make_project(project_dir, [SOURCE_A])
    await action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
    assert len(calls) == 1

    _write(stamped, "HELLO\n")
    os.utime(
        stamped, ns=(stamped.stat().st_atime_ns, stamped.stat().st_mtime_ns + 1_000_000)
    )
    project = _make_project(project_dir, [SOURCE_A])
    result = await action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
    assert SOURCE_A in result.metas
    assert len(calls) == 2


async def test_pythonpath_change_misses(setup, monkeypatch: pytest.MonkeyPatch) -> None:
    """A different interpreter search path cannot reuse the file, so it dumps again."""
    project_dir, stamped, venv, ws_context, calls = setup
    project = _make_project(project_dir, [SOURCE_A])
    await action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
    assert len(calls) == 1

    monkeypatch.setenv("PYTHONPATH", "/tmp/definitely-not-the-same")
    project = _make_project(project_dir, [SOURCE_A])
    result = await action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
    assert SOURCE_A in result.metas
    assert len(calls) == 2


async def test_pth_rewrite_and_addition_miss(setup) -> None:
    """Edited or added .pth files change imports, so the file is refilled."""
    project_dir, stamped, venv, ws_context, calls = setup
    site_packages = action_meta_cache._site_packages_dir(venv)
    assert site_packages is not None
    _write(site_packages / "extra.pth", "import os\n")
    project = _make_project(project_dir, [SOURCE_A])
    await action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
    assert len(calls) == 1

    _write(site_packages / "extra.pth", "import sys\n")
    project = _make_project(project_dir, [SOURCE_A])
    await action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
    assert len(calls) == 2

    _write(site_packages / "added.pth", "import os\n")
    project = _make_project(project_dir, [SOURCE_A])
    await action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
    assert len(calls) == 3


async def test_identity_changes_miss(setup) -> None:
    """An upgraded or reinstalled runner cannot reuse the previous env's answers."""
    project_dir, stamped, venv, ws_context, calls = setup
    project = _make_project(project_dir, [SOURCE_A])
    await action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
    assert len(calls) == 1

    site_packages = action_meta_cache._site_packages_dir(venv)
    assert site_packages is not None
    old = site_packages / "finecode_extension_runner-0.1.dist-info"
    new = site_packages / "finecode_extension_runner-0.2.dist-info"
    old.rename(new)
    project = _make_project(project_dir, [SOURCE_A])
    await action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
    assert len(calls) == 2

    record = new / "RECORD"
    _write(record, "changed-record\n")
    project = _make_project(project_dir, [SOURCE_A])
    await action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
    assert len(calls) == 3


async def test_mtime_only_change_stays_a_hit(setup) -> None:
    """Touching a file without changing it keeps the hit, so checkouts do not all redump."""
    project_dir, stamped, venv, ws_context, calls = setup
    project = _make_project(project_dir, [SOURCE_A])
    await action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
    assert len(calls) == 1

    stat = stamped.stat()
    os.utime(stamped, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    project = _make_project(project_dir, [SOURCE_A])
    result = await action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
    assert SOURCE_A in result.metas
    assert len(calls) == 1


async def test_write_merges_disjoint_sources_and_leaves_no_tmp(
    setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two dumps sharing an identity key accumulate, and a failed rename never litters."""
    project_dir, stamped, venv, ws_context, calls = setup
    project = _make_project(project_dir, [SOURCE_A, SOURCE_B])
    await action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
    await action_meta_cache.lookup(project, ENV, [SOURCE_B], ws_context)
    stored = json.loads(action_meta_cache._cache_file(venv).read_text(encoding="utf-8"))
    assert SOURCE_A in stored["entries"]
    assert SOURCE_B in stored["entries"]
    assert list(action_meta_cache._cache_file(venv).parent.glob("*.tmp")) == []

    real_replace = os.replace

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _boom)
    project = _make_project(project_dir, [SOURCE_A])
    await action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
    assert list(action_meta_cache._cache_file(venv).parent.glob("*.tmp")) == []
    monkeypatch.setattr(os, "replace", real_replace)


async def test_racy_entry_applies_but_is_not_written(setup) -> None:
    """A file replaced mid-dump still answers this run, but nothing stale is stored."""
    project_dir, stamped, venv, ws_context, calls = setup
    from finecode.wm_server.runner import action_meta_dump as dump_mod

    async def _racy_dump(
        python_cmd: str, proj_dir: Path, sources: list[str], *, attempt_timeout: float
    ):
        doc = _document(project_dir, venv, sources, stamped)
        for entry in doc["entries"].values():
            for item in entry["files"]:
                item["mtime_ns"] = doc["startedNs"]
                item["ctime_ns"] = doc["startedNs"]
        return dump_mod.DumpOutcome(kind="ok", document=doc)

    import finecode.wm_server.services.action_meta_cache as cache_mod

    calls.clear()
    old = cache_mod.action_meta_dump.run_dump
    cache_mod.action_meta_dump.run_dump = _racy_dump
    try:
        project = _make_project(project_dir, [SOURCE_A])
        result = await action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
    finally:
        cache_mod.action_meta_dump.run_dump = old
    assert SOURCE_A in result.metas
    stored = action_meta_cache._read_file(action_meta_cache._cache_file(venv))
    assert stored is None or SOURCE_A not in stored.get("entries", {})


async def test_import_failure_memoized_until_traceback_changes(
    setup, caplog=None
) -> None:
    """A broken import hides once with one log line, then reappears only after its files move."""
    project_dir, stamped, venv, ws_context, calls = setup
    from finecode.wm_server.runner import action_meta_dump as dump_mod

    async def _failing_dump(
        python_cmd: str, proj_dir: Path, sources: list[str], *, attempt_timeout: float
    ):
        calls.append(list(sources))
        return dump_mod.DumpOutcome(
            kind="ok",
            document={
                "format": 1,
                "startedNs": time.time_ns() + 10_000_000_000,
                "actionMetaFile": _fingerprint(stamped),
                "header": {
                    "sysPath": list(sys.path),
                    "pythonpath": os.environ.get("PYTHONPATH"),
                    "sitePackages": {
                        "path": str(action_meta_cache._site_packages_dir(venv)),
                        "dir_mtime_ns": action_meta_cache._site_packages_dir(venv)
                        .stat()
                        .st_mtime_ns,
                        "pth": [],
                    },
                    "headerStable": True,
                },
                "entries": {},
                "failures": {
                    SOURCE_A: {
                        "error": "ImportError: boom",
                        "files": [_fingerprint(stamped)],
                    }
                },
            },
        )

    import finecode.wm_server.services.action_meta_cache as cache_mod

    cache_mod.action_meta_dump.run_dump = _failing_dump
    try:
        project = _make_project(project_dir, [SOURCE_A])
        first = await action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
        assert first.failures[SOURCE_A] is not None
        assert first.failures[SOURCE_A].kind == "import_failed"
        n_calls = len(calls)
        project = _make_project(project_dir, [SOURCE_A])
        second = await action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
        assert second.failures[SOURCE_A] is not None
        assert len(calls) == n_calls

        _write(stamped, "changed!\n")
        project = _make_project(project_dir, [SOURCE_A])
        third = await action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
        assert third.failures[SOURCE_A] is not None
        assert len(calls) == n_calls + 1
    finally:
        pass


async def test_missing_venv_gives_env_unusable_without_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An env with no interpreter is remembered without spawning anything."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    ws_context = context.WorkspaceContext(ws_dirs_paths=[project_dir])
    project = _make_project(project_dir, [SOURCE_A])

    async def _fail(*args, **kwargs):
        raise AssertionError("must not spawn without a venv")

    monkeypatch.setattr(action_meta_dump, "run_dump", _fail)
    result = await action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
    assert result.failures[SOURCE_A] is not None
    assert result.failures[SOURCE_A].kind == "env_unusable"


async def test_skew_persists_for_wheel_but_not_editable(tmp_path: Path) -> None:
    """Wheel skew survives restarts per runner identity, while editable skew never outlives a branch switch."""
    from finecode.wm_server.runner import action_meta_dump as dump_mod

    for editable in (False, True):
        project_dir = tmp_path / f"p_{int(editable)}"
        project_dir.mkdir()
        venv = _make_venv(project_dir, ENV, editable=editable)
        ws_context = context.WorkspaceContext(ws_dirs_paths=[project_dir])
        project = _make_project(project_dir, [SOURCE_A])

        async def _skew_dump(
            python_cmd: str,
            proj_dir: Path,
            sources: list[str],
            *,
            attempt_timeout: float,
        ):
            return dump_mod.DumpOutcome(
                kind="skew", document=None, reason="No such command"
            )

        old = dump_mod.run_dump
        dump_mod.run_dump = _skew_dump
        try:
            result = await action_meta_cache.lookup(
                project, ENV, [SOURCE_A], ws_context
            )
        finally:
            dump_mod.run_dump = old
        assert result.failures[SOURCE_A] is not None
        assert result.failures[SOURCE_A].kind == "skew"
        marker = action_meta_cache._read_file(action_meta_cache._cache_file(venv))
        if editable:
            assert marker is None
        else:
            assert marker is not None
            assert "skew" in marker
            fresh = context.WorkspaceContext(ws_dirs_paths=[project_dir])
            before = fresh.action_meta_dump_stats.skew_from_cache
            project2 = _make_project(project_dir, [SOURCE_A])
            again = await action_meta_cache.lookup(project2, ENV, [SOURCE_A], fresh)
            assert again.failures[SOURCE_A] is not None
            assert fresh.action_meta_dump_stats.skew_from_cache == before + 1


async def test_dedup_shares_one_dump_between_callers(setup) -> None:
    """Concurrent lookups share one task, and disjoint subsets both resolve from the full dump."""
    project_dir, stamped, venv, ws_context, calls = setup
    project = _make_project(project_dir, [SOURCE_A, SOURCE_B])
    left, right = await asyncio.gather(
        action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context),
        action_meta_cache.lookup(project, ENV, [SOURCE_B], ws_context),
    )
    assert SOURCE_A in left.metas
    assert SOURCE_B in right.metas
    assert len(calls) == 1
    assert sorted(calls[0]) == sorted([SOURCE_A, SOURCE_B])


async def test_cancel_dumps_and_shutdown_gate(setup) -> None:
    """Shutdown cancels in-flight dumps, and no new task starts once it begins."""
    project_dir, stamped, venv, ws_context, calls = setup
    from finecode.wm_server.runner import action_meta_dump as dump_mod

    started = asyncio.Event()

    async def _slow_dump(*args, **kwargs):
        started.set()
        await asyncio.sleep(30.0)
        return dump_mod.DumpOutcome(kind="ok", document={})

    old = dump_mod.run_dump
    dump_mod.run_dump = _slow_dump
    try:
        project = _make_project(project_dir, [SOURCE_A])
        task = asyncio.ensure_future(
            action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
        )
        await started.wait()
        cancelled = action_meta_cache.cancel_dumps(ws_context)
        assert cancelled
        await asyncio.gather(*cancelled, return_exceptions=True)
        await task
    finally:
        dump_mod.run_dump = old

    ws_context.shutting_down = True
    assert action_meta_cache._ensure_dump_task(venv, project, ENV, ws_context) is None


async def test_budget_counts_and_peak(setup) -> None:
    """Dumps hold the startup semaphore and surface in the waiting gauges and peak."""
    project_dir, stamped, venv, ws_context, calls = setup
    from finecode.wm_server.runner import action_meta_dump as dump_mod

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _holding_dump(*args, **kwargs):
        entered.set()
        await release.wait()
        return dump_mod.DumpOutcome(
            kind="ok",
            document=_document(
                project_dir, venv, kwargs.get("sources", [SOURCE_A]), stamped
            )
            if isinstance(kwargs.get("sources"), list)
            else _document(project_dir, venv, [SOURCE_A], stamped),
        )

    async def _holding_dump2(
        python_cmd: str, proj_dir: Path, sources: list[str], *, attempt_timeout: float
    ):
        entered.set()
        await release.wait()
        return dump_mod.DumpOutcome(
            kind="ok", document=_document(project_dir, venv, sources, stamped)
        )

    old = dump_mod.run_dump
    dump_mod.run_dump = _holding_dump2
    try:
        project = _make_project(project_dir, [SOURCE_A])
        first = asyncio.ensure_future(
            action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
        )
        await entered.wait()
        await asyncio.sleep(0.1)
        assert ws_context.action_meta_dump_stats.running == 1
        second = asyncio.ensure_future(
            action_meta_cache.lookup(project, ENV, [SOURCE_A], ws_context)
        )
        await asyncio.sleep(0.5)
        release.set()
        await asyncio.gather(first, second)
    finally:
        dump_mod.run_dump = old
    assert ws_context.action_meta_dump_stats.spawned >= 1
    assert ws_context.action_meta_dump_stats.ok >= 1
