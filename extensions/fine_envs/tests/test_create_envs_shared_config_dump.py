import asyncio
import dataclasses
import pathlib
import shutil
import tempfile
from typing import Any, cast

import pytest
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    ilogger,
    iprojectactionrunner,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import (
    ResourceUri,
    path_to_resource_uri,
    resource_uri_to_path,
)
from finecode_extension_runner.testing import handler_test_session

from fine_envs import (
    create_env_action,
    create_envs_action,
    dump_config_action,
)
from fine_envs.create_envs_discover_envs_handler import (
    CreateEnvsDiscoverEnvsHandler,
)
from fine_envs.create_envs_dispatch_handler import (
    CreateEnvsDispatchHandler,
    CreateEnvsDispatchHandlerConfig,
)
from fine_envs.dump_config_handler import DumpConfigHandler
from fine_envs.dump_config_save_handler import DumpConfigSaveHandler


@dataclasses.dataclass
class _ProbeCall:
    uri: str | None
    pyproject_exists: bool


class _Probe:
    """Per-test behavior switch for the recording `create_env` handler."""

    def __init__(self) -> None:
        self.mode: str = "record"
        self.calls: list[Any] = []
        self.created: bool = True
        self.error_text: str = "probe-error"
        self.raise_names: set[str] = set()
        self.block_names: set[str] = set()
        self.gates: dict[str, asyncio.Event] = {}


_PROBE = _Probe()


@pytest.fixture(autouse=True)
def probe() -> _Probe:
    global _PROBE
    _PROBE = _Probe()
    return _PROBE


class _ProbeCreateEnvHandler(
    code_action.ActionHandler[
        create_env_action.CreateEnvAction, code_action.ActionHandlerConfig
    ]
):
    """Recording `create_env` handler; behavior is driven by the `_PROBE` switch."""

    async def run(
        self,
        payload: create_env_action.CreateEnvRunPayload,
        run_context: create_env_action.CreateEnvRunContext,
    ) -> create_envs_action.CreateEnvsRunResult:
        probe = _PROBE
        uri = payload.resolved_project_def_path
        exists = resource_uri_to_path(uri).is_file() if uri is not None else False
        if payload.env.name in probe.raise_names:
            probe.calls.append(_ProbeCall(uri=_uri_str(uri), pyproject_exists=exists))
            raise RuntimeError(probe.error_text)
        if payload.env.name in probe.block_names:
            probe.calls.append(_ProbeCall(uri=_uri_str(uri), pyproject_exists=exists))
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.3)
                raise
        if probe.mode == "cancel-probe":
            probe.calls.append(_ProbeCall(uri=_uri_str(uri), pyproject_exists=exists))
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)
                assert uri is not None
                probe.calls.append(
                    (
                        "cancel-saw-exists",
                        resource_uri_to_path(uri).parent.parent.exists(),
                    )
                )
                raise
        if probe.mode == "gate":
            assert uri is not None
            probe.calls.append(_ProbeCall(uri=_uri_str(uri), pyproject_exists=exists))
            await probe.gates.setdefault(str(uri), asyncio.Event()).wait()
            return create_envs_action.CreateEnvsRunResult(errors=[], created=True)
        if probe.mode == "errors":
            probe.calls.append(_ProbeCall(uri=_uri_str(uri), pyproject_exists=exists))
            return create_envs_action.CreateEnvsRunResult(
                errors=[probe.error_text], created=False
            )
        probe.calls.append(_ProbeCall(uri=_uri_str(uri), pyproject_exists=exists))
        return create_envs_action.CreateEnvsRunResult(errors=[], created=probe.created)


@dataclasses.dataclass
class _RaisingDumpConfigHandlerConfig(code_action.ActionHandlerConfig): ...


class _RaisingDumpConfigHandler(
    code_action.ActionHandler[
        dump_config_action.DumpConfigAction, _RaisingDumpConfigHandlerConfig
    ]
):
    async def run(
        self,
        payload: dump_config_action.DumpConfigRunPayload,
        run_context: dump_config_action.DumpConfigRunContext,
    ) -> dump_config_action.DumpConfigRunResult:
        raise RuntimeError("dump-boom")


_RECORDED_DUMP_TARGETS: list[ResourceUri] = []
_ORDER: list[str] = []


@dataclasses.dataclass
class _OrderMarkerHandlerConfig(code_action.ActionHandlerConfig): ...


class _OrderMarkerHandler(
    code_action.ActionHandler[
        dump_config_action.DumpConfigAction, _OrderMarkerHandlerConfig
    ]
):
    """Module-level marker handler for the multi-project test."""

    async def run(
        self,
        payload: dump_config_action.DumpConfigRunPayload,
        run_context: dump_config_action.DumpConfigRunContext,
    ) -> dump_config_action.DumpConfigRunResult:
        _ORDER.append(f"dispatch:{payload.target_file_path}")
        assert run_context.config_dump_content is not None
        run_context.config_dump_content += "\n[tool-ac6-handler]\npresent = true\n"
        return dump_config_action.DumpConfigRunResult(
            config_dump=run_context.raw_config_dump
        )


@dataclasses.dataclass
class _TargetRecordingDumpHandlerConfig(code_action.ActionHandlerConfig): ...


class _TargetRecordingDumpHandler(
    code_action.ActionHandler[
        dump_config_action.DumpConfigAction, _TargetRecordingDumpHandlerConfig
    ]
):
    """Records the dispatched ``target_file_path`` without changing the
    pipeline: content set by ``DumpConfigHandler`` is left untouched for the
    save handler."""

    async def run(
        self,
        payload: dump_config_action.DumpConfigRunPayload,
        run_context: dump_config_action.DumpConfigRunContext,
    ) -> dump_config_action.DumpConfigRunResult:
        _RECORDED_DUMP_TARGETS.append(payload.target_file_path)
        return dump_config_action.DumpConfigRunResult(
            config_dump=run_context.raw_config_dump
        )


class _CountingProjectInfoProvider:
    def __init__(self, raw: dict[str, Any], def_path: pathlib.Path) -> None:
        self.raw = raw
        self.def_path = def_path
        self.raw_calls: list[pathlib.Path] = []
        self.current_raw_calls = 0
        self.selection_calls = 0
        self.raw_error: Exception | None = None
        self.selection_error: Exception | None = None
        self.block_fetch = False

    def get_current_project_dir_path(self) -> pathlib.Path:
        return self.def_path.parent

    def get_current_project_def_path(self) -> pathlib.Path:
        return self.def_path

    async def get_current_project_package_name(self) -> str:
        raise NotImplementedError

    async def get_project_raw_config(
        self, project_def_path: pathlib.Path
    ) -> dict[str, Any]:
        self.raw_calls.append(project_def_path)
        if self.block_fetch:
            await asyncio.Event().wait()
        if self.raw_error is not None:
            raise self.raw_error
        return self.raw

    async def get_current_project_raw_config(self) -> dict[str, Any]:
        self.current_raw_calls += 1
        return self.raw

    def get_current_project_raw_config_version(self) -> int:
        return 0

    async def get_workspace_packages(self) -> dict[str, Any]:
        raise NotImplementedError

    async def get_workspace_extra_selection(self) -> dict[str, list[str]]:
        self.selection_calls += 1
        if self.selection_error is not None:
            raise self.selection_error
        return {}


class _RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def exception(self, exception: Exception) -> None: ...

    def trace(self, message: str) -> None: ...

    def info(self, message: str) -> None: ...

    def debug(self, message: str) -> None: ...

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def error(self, message: str) -> None: ...

    def disable(self, package: str) -> None: ...

    def enable(self, package: str) -> None: ...


def _source(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def _uri_str(uri: ResourceUri | None) -> str | None:
    return None if uri is None else str(uri)


def _session_actions(
    *,
    create_handler_cls: type = _ProbeCreateEnvHandler,
    dump_handler_classes: list[type] | None = None,
    with_discovery: bool = False,
) -> dict[str, Any]:
    create_envs_handlers = []
    if with_discovery:
        create_envs_handlers.append(
            {
                "name": "discover",
                "source": _source(CreateEnvsDiscoverEnvsHandler),
                "env": "test",
            }
        )
    create_envs_handlers.append(
        {
            "name": "dispatch",
            "source": _source(CreateEnvsDispatchHandler),
            "env": "test",
        }
    )
    dump_handlers = [
        {"name": cls.__name__, "source": _source(cls), "env": "test"}
        for cls in (dump_handler_classes or [DumpConfigHandler, DumpConfigSaveHandler])
    ]
    return {
        create_envs_action.CreateEnvsAction.__name__: {
            "source": _source(create_envs_action.CreateEnvsAction),
            "handlers": create_envs_handlers,
        },
        create_env_action.CreateEnvAction.__name__: {
            "source": _source(create_env_action.CreateEnvAction),
            "handlers": [
                {"name": "probe", "source": _source(create_handler_cls), "env": "test"},
            ],
        },
        dump_config_action.DumpConfigAction.__name__: {
            "source": _source(dump_config_action.DumpConfigAction),
            "handlers": dump_handlers,
        },
    }


def _env_info(
    def_path: pathlib.Path, tmp_path: pathlib.Path, name: str
) -> create_envs_action.EnvInfo:
    return create_envs_action.EnvInfo(
        name=name,
        venv_dir_path=path_to_resource_uri(tmp_path / "venvs" / name),
        project_def_path=path_to_resource_uri(def_path),
        interpreter=None,
    )


def _shared_dump_dirs() -> set[str]:
    return {
        p.name
        for p in pathlib.Path(tempfile.gettempdir()).iterdir()
        if p.name.startswith("finecode_config_dump_")
    }


async def _run_create_envs(
    tmp_path: pathlib.Path,
    envs: list[create_envs_action.EnvInfo] | None,
    *,
    provider: _CountingProjectInfoProvider,
    logger: _RecordingLogger,
    handler_configs: dict[str, Any] | None = None,
    **session_kwargs: Any,
) -> create_envs_action.CreateEnvsRunResult | None:
    async with handler_test_session(
        project_dir=tmp_path,
        actions=_session_actions(**session_kwargs),
        service_overrides={
            iprojectinfoprovider.IProjectInfoProvider: provider,
            ilogger.ILogger: logger,
        },
        handler_configs=handler_configs,
    ) as session:
        result = await session.run_action(
            create_envs_action.CreateEnvsAction.__name__,
            create_envs_action.CreateEnvsRunPayload(envs=envs),
        )
        return cast("create_envs_action.CreateEnvsRunResult | None", result)


def _provider(
    tmp_path: pathlib.Path, raw: dict[str, Any] | None = None
) -> _CountingProjectInfoProvider:
    def_path = tmp_path / "pyproject.toml"
    if not def_path.exists():
        _ = def_path.write_text("[project]\n", encoding="utf-8")
    return _CountingProjectInfoProvider(raw or {}, def_path)


async def test_explicit_batch_shares_one_dump_across_children(
    tmp_path: pathlib.Path, probe: _Probe
) -> None:
    """Three envs of one project are prepared once and every child borrows the
    same live directory, so per-env config fetches collapse to one."""
    provider = _provider(tmp_path)
    logger = _RecordingLogger()
    envs = [_env_info(provider.def_path, tmp_path, f"env-{i}") for i in range(3)]
    _RECORDED_DUMP_TARGETS.clear()

    result = await _run_create_envs(
        tmp_path,
        envs,
        provider=provider,
        logger=logger,
        dump_handler_classes=[
            DumpConfigHandler,
            _TargetRecordingDumpHandler,
            DumpConfigSaveHandler,
        ],
    )

    assert result is not None
    assert result.errors == []
    assert provider.raw_calls == [provider.def_path]
    assert provider.selection_calls == 1
    assert logger.warnings == []
    assert len(probe.calls) == 3
    uris = {call.uri for call in probe.calls}
    assert len(uris) == 1
    only = next(iter(uris))
    assert only is not None and only.startswith("file://")
    lent = resource_uri_to_path(ResourceUri(only))
    assert lent.name == "pyproject.toml"
    assert lent.parent.name == tmp_path.name
    assert lent.parent.parent.name.startswith("finecode_config_dump_")
    assert all(call.pyproject_exists for call in probe.calls)
    assert len(_RECORDED_DUMP_TARGETS) == 1
    assert _RECORDED_DUMP_TARGETS[0] == ResourceUri(only)


async def test_discovered_batch_counts_discovery_and_preparation_once(
    tmp_path: pathlib.Path, probe: _Probe
) -> None:
    """A discovered batch fetches the raw config once for discovery and once
    for the shared preparation, and every child still shares one dump."""
    raw: dict[str, Any] = {"dependency-groups": {"env-a": [], "env-b": []}}
    provider = _provider(tmp_path, raw)
    logger = _RecordingLogger()

    async with handler_test_session(
        project_dir=tmp_path,
        actions=_session_actions(with_discovery=True),
        service_overrides={
            iprojectinfoprovider.IProjectInfoProvider: provider,
            ilogger.ILogger: logger,
        },
    ) as session:
        result = await session.run_action(
            create_envs_action.CreateEnvsAction.__name__,
            create_envs_action.CreateEnvsRunPayload(envs=None),
        )

    assert result is not None
    assert cast(Any, result).errors == []
    assert provider.current_raw_calls == 1
    assert provider.raw_calls == [provider.def_path]
    assert len(probe.calls) == 2
    assert len({call.uri for call in probe.calls}) == 1


async def test_warm_batch_and_empty_batch(
    tmp_path: pathlib.Path, probe: _Probe
) -> None:
    """Warm envs share one preparation and the directory is gone afterwards;
    an explicit empty batch prepares nothing and creates no directory."""
    provider = _provider(tmp_path)
    logger = _RecordingLogger()
    probe.created = False
    envs = [_env_info(provider.def_path, tmp_path, f"env-{i}") for i in range(2)]
    before = _shared_dump_dirs()

    result = await _run_create_envs(tmp_path, envs, provider=provider, logger=logger)

    assert result is not None
    assert result.errors == []
    assert provider.raw_calls == [provider.def_path]
    assert provider.selection_calls == 1
    assert logger.warnings == []
    assert len(probe.calls) == 2
    assert _shared_dump_dirs() == before

    before = _shared_dump_dirs()
    probe.calls.clear()
    empty_result = await _run_create_envs(
        tmp_path, [], provider=provider, logger=logger
    )

    assert empty_result is not None
    assert empty_result.errors == []
    assert len(provider.raw_calls) == 1
    assert provider.selection_calls == 1
    assert probe.calls == []
    assert _shared_dump_dirs() == before


async def test_mixed_project_batch_prepares_one_dump_per_project(
    tmp_path: pathlib.Path, probe: _Probe
) -> None:
    """One explicit batch holds envs of three projects: each project is fetched
    and dispatched once, each child sees its own project's file, and the run
    directory holds one subdirectory per project."""
    _ORDER.clear()
    current_def = tmp_path / "pyproject.toml"
    _ = current_def.write_text("[project]\n", encoding="utf-8")
    proj_b = tmp_path / "x" / "proj" / "pyproject.toml"
    proj_b.parent.mkdir(parents=True)
    _ = proj_b.write_text("[project]\n", encoding="utf-8")
    proj_c = tmp_path / "y" / "proj" / "pyproject.toml"
    proj_c.parent.mkdir(parents=True)
    _ = proj_c.write_text("[project]\n", encoding="utf-8")

    raw_by_path = {
        current_def: {"project": {"name": "a"}, "tool": {"ac6": "a"}},
        proj_b: {"project": {"name": "b"}, "tool": {"ac6": "b"}},
        proj_c: {"project": {"name": "c"}, "tool": {"ac6": "c"}},
    }

    class _PerProjectProvider(_CountingProjectInfoProvider):
        async def get_project_raw_config(
            self, project_def_path: pathlib.Path
        ) -> dict[str, Any]:
            self.raw_calls.append(project_def_path)
            _ORDER.append(f"fetch:{project_def_path}")
            return raw_by_path[project_def_path]

        async def get_workspace_extra_selection(self) -> dict[str, list[str]]:
            self.selection_calls += 1
            return {}

    orig_run = _ProbeCreateEnvHandler.run
    seen_contents: list[str] = []
    seen_entries: list[list[str]] = []

    async def _recording_run(self: Any, payload: Any, run_context: Any) -> Any:
        _ORDER.append(f"child:{payload.env.name}")
        lent = resource_uri_to_path(payload.resolved_project_def_path)  # type: ignore[arg-type]
        seen_contents.append(lent.read_text(encoding="utf-8"))
        seen_entries.append(sorted(x.name for x in lent.parent.iterdir()))
        return await orig_run(self, payload, run_context)

    provider = _PerProjectProvider({"project": {"name": "a"}}, current_def)
    logger = _RecordingLogger()
    envs = [
        _env_info(current_def, tmp_path, "a-0"),
        _env_info(current_def, tmp_path, "a-1"),
        _env_info(proj_b, tmp_path, "b-0"),
        _env_info(proj_c, tmp_path, "c-0"),
    ]
    _RECORDED_DUMP_TARGETS.clear()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(_ProbeCreateEnvHandler, "run", _recording_run)
    try:
        result = await _run_create_envs(
            tmp_path,
            envs,
            provider=provider,  # type: ignore[arg-type]
            logger=logger,
            dump_handler_classes=[
                DumpConfigHandler,
                _OrderMarkerHandler,
                _TargetRecordingDumpHandler,
                DumpConfigSaveHandler,
            ],
        )
    finally:
        monkeypatch.undo()

    assert result is not None
    assert result.errors == []
    assert sorted(provider.raw_calls) == sorted([current_def, proj_b, proj_c])
    assert len(_RECORDED_DUMP_TARGETS) == 3
    assert len(set(_RECORDED_DUMP_TARGETS)) == 3
    assert len(probe.calls) == 4
    by_env = {c.uri for c in probe.calls}
    assert len(by_env) == 3
    uris_by_project: dict[str, str] = {}
    assert len(seen_contents) == 4
    for text in seen_contents:
        assert "tool-ac6-handler" in text
    assert any('"a"' in c or "a" in c for c in seen_contents)
    for entries in seen_entries:
        assert entries == ["pyproject.toml"]
    for call in probe.calls:
        assert call.uri is not None
        lent = resource_uri_to_path(ResourceUri(call.uri))
        assert lent.name == "pyproject.toml"
        assert call.pyproject_exists
    # B and C have different parents though the directory name repeats
    all_parents = {
        resource_uri_to_path(ResourceUri(c.uri)).parent for c in probe.calls if c.uri
    }  # type: ignore[arg-type]
    assert len(all_parents) == 3
    grands = {
        resource_uri_to_path(ResourceUri(c.uri)).parent.parent
        for c in probe.calls
        if c.uri
    }  # type: ignore[arg-type]
    assert len(grands) == 1
    grand = next(iter(grands))
    assert grand.name.startswith("finecode_config_dump_")
    # every dispatch ended before the first child started
    first_child = next(i for i, e in enumerate(_ORDER) if e.startswith("child:"))
    dispatches = [i for i, e in enumerate(_ORDER) if e.startswith("dispatch:")]
    assert len(dispatches) == 3
    assert all(d < first_child for d in dispatches)
    assert _shared_dump_dirs() == set()


async def test_percent_encoded_and_plain_spellings_share_one_dump(
    tmp_path: pathlib.Path, probe: _Probe
) -> None:
    """The percent-encoded and plain `file://` spellings of one file group to
    one project: one fetch, one dispatch, one identical lent URI."""
    spaced = tmp_path / "dir with space" / "pyproject.toml"
    spaced.parent.mkdir(parents=True)
    _ = spaced.write_text("[project]\n", encoding="utf-8")
    provider = _provider(tmp_path, {})
    provider.def_path = spaced
    logger = _RecordingLogger()
    encoded = path_to_resource_uri(spaced)
    plain = ResourceUri("file://" + str(spaced))
    envs = [
        create_envs_action.EnvInfo(
            name="e-encoded",
            venv_dir_path=path_to_resource_uri(tmp_path / "venvs" / "e-encoded"),
            project_def_path=encoded,
            interpreter=None,
        ),
        create_envs_action.EnvInfo(
            name="e-plain",
            venv_dir_path=path_to_resource_uri(tmp_path / "venvs" / "e-plain"),
            project_def_path=plain,
            interpreter=None,
        ),
    ]

    result = await _run_create_envs(tmp_path, envs, provider=provider, logger=logger)

    assert result is not None
    assert result.errors == []
    assert provider.raw_calls == [spaced]
    assert provider.selection_calls == 1
    assert len(probe.calls) == 2
    assert probe.calls[0].uri == probe.calls[1].uri
    assert probe.calls[0].uri is not None


async def test_prepare_failure_names_project_and_removes_run_dir(
    tmp_path: pathlib.Path, probe: _Probe
) -> None:
    """When one project's fetch fails, the run fails naming that project, no
    child starts, and the run directory is gone."""
    current_def = tmp_path / "pyproject.toml"
    _ = current_def.write_text("[project]\n", encoding="utf-8")
    proj_b = tmp_path / "x" / "proj" / "pyproject.toml"
    proj_b.parent.mkdir(parents=True)
    _ = proj_b.write_text("[project]\n", encoding="utf-8")
    proj_c = tmp_path / "y" / "proj" / "pyproject.toml"
    proj_c.parent.mkdir(parents=True)
    _ = proj_c.write_text("[project]\n", encoding="utf-8")

    class _FailBProvider(_CountingProjectInfoProvider):
        async def get_project_raw_config(
            self, project_def_path: pathlib.Path
        ) -> dict[str, Any]:
            self.raw_calls.append(project_def_path)
            if project_def_path == proj_b:
                raise RuntimeError("b-boom")
            return {}

    provider = _FailBProvider({}, current_def)
    logger = _RecordingLogger()
    envs = [
        _env_info(current_def, tmp_path, "a-0"),
        _env_info(proj_b, tmp_path, "b-0"),
        _env_info(proj_c, tmp_path, "c-0"),
    ]
    before = _shared_dump_dirs()

    with pytest.raises(
        iprojectactionrunner.ActionRunFailed,
        match="Failed to prepare the config dump of",
    ) as exc_info:
        await _run_create_envs(tmp_path, envs, provider=provider, logger=logger)

    assert str(proj_b) in exc_info.value.message
    assert "b-boom" in exc_info.value.message
    assert probe.calls == []
    assert _shared_dump_dirs() == before
    assert provider.raw_calls.count(proj_b) == 1


async def test_concurrent_preparation_overlaps_fetches(
    tmp_path: pathlib.Path, probe: _Probe
) -> None:
    """Fetches for two projects overlap: each waits until both started."""
    def_a = tmp_path / "pyproject.toml"
    _ = def_a.write_text("[project]\n", encoding="utf-8")
    def_b = tmp_path / "other" / "pyproject.toml"
    def_b.parent.mkdir(parents=True)
    _ = def_b.write_text("[project]\n", encoding="utf-8")

    started: set[str] = set()
    started_event = asyncio.Event()

    class _BarrierProvider(_CountingProjectInfoProvider):
        async def get_project_raw_config(
            self, project_def_path: pathlib.Path
        ) -> dict[str, Any]:
            self.raw_calls.append(project_def_path)
            started.add(str(project_def_path))
            if len(started) >= 2:
                started_event.set()
            await asyncio.wait_for(started_event.wait(), timeout=10)
            return {}

    provider = _BarrierProvider({}, def_a)
    logger = _RecordingLogger()
    envs = [
        _env_info(def_a, tmp_path, "a-0"),
        _env_info(def_b, tmp_path, "b-0"),
    ]

    result = await asyncio.wait_for(
        _run_create_envs(tmp_path, envs, provider=provider, logger=logger), timeout=10
    )

    assert result is not None
    assert result.errors == []
    assert len(probe.calls) == 2


async def test_setting_is_ignored_and_still_prepares(
    tmp_path: pathlib.Path, probe: _Probe
) -> None:
    """A stale opt-out key is ignored, not\n    rejected: the run still prepares and lends a value."""
    provider = _provider(tmp_path)
    logger = _RecordingLogger()
    envs = [_env_info(provider.def_path, tmp_path, f"env-{i}") for i in range(1)]

    result = await _run_create_envs(
        tmp_path,
        envs,
        provider=provider,
        logger=logger,
        handler_configs={
            _source(CreateEnvsDispatchHandler): {
                "prepare_" + "project_config_dump": False
            }
        },
    )

    assert result is not None
    assert result.errors == []
    assert provider.raw_calls == [provider.def_path]
    assert len(probe.calls) == 1
    assert probe.calls[0].uri is not None


async def test_child_errors_are_aggregated_and_dump_removed(
    tmp_path: pathlib.Path, probe: _Probe
) -> None:
    """Children that report errors fail the batch without failing the run's
    cleanup: the shared directory is still removed after the last child."""
    provider = _provider(tmp_path)
    logger = _RecordingLogger()
    probe.mode = "errors"
    probe.error_text = "child-nope"
    envs = [_env_info(provider.def_path, tmp_path, f"env-{i}") for i in range(2)]

    result = await _run_create_envs(tmp_path, envs, provider=provider, logger=logger)

    assert result is not None
    assert result.errors == ["child-nope", "child-nope"]
    assert _shared_dump_dirs() == set()


async def test_cancellation_during_child_unwind_keeps_dump_until_last_child(
    tmp_path: pathlib.Path, probe: _Probe
) -> None:
    """A child still unwinding after cancellation must see the dump: removal
    happens only after every started child has ended."""
    provider = _provider(tmp_path)
    logger = _RecordingLogger()
    probe.mode = "cancel-probe"
    envs = [_env_info(provider.def_path, tmp_path, "env-0")]

    async with handler_test_session(
        project_dir=tmp_path,
        actions=_session_actions(),
        service_overrides={
            iprojectinfoprovider.IProjectInfoProvider: provider,
            ilogger.ILogger: logger,
        },
    ) as session:
        task = asyncio.create_task(
            session.run_action(
                create_envs_action.CreateEnvsAction.__name__,
                create_envs_action.CreateEnvsRunPayload(envs=envs),
            )
        )
        for _ in range(100):
            if probe.calls:
                break
            await asyncio.sleep(0.01)
        assert probe.calls
        _ = task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert ("cancel-saw-exists", True) in probe.calls
    assert _shared_dump_dirs() == set()


async def test_sibling_failure_plus_double_cancel_still_raises_child_error(
    tmp_path: pathlib.Path, probe: _Probe
) -> None:
    """A second cancellation landing during sibling-failure teardown must not
    strand the directory nor swallow the child's message."""
    provider = _provider(tmp_path)
    logger = _RecordingLogger()
    probe.error_text = "boom-double-cancel"
    probe.raise_names = {"env-a"}
    probe.block_names = {"env-b"}
    envs = [
        _env_info(provider.def_path, tmp_path, "env-a"),
        _env_info(provider.def_path, tmp_path, "env-b"),
    ]
    before = _shared_dump_dirs()

    async with handler_test_session(
        project_dir=tmp_path,
        actions=_session_actions(),
        service_overrides={
            iprojectinfoprovider.IProjectInfoProvider: provider,
            ilogger.ILogger: logger,
        },
    ) as session:
        task = asyncio.create_task(
            session.run_action(
                create_envs_action.CreateEnvsAction.__name__,
                create_envs_action.CreateEnvsRunPayload(envs=envs),
            )
        )
        for _ in range(200):
            if sum(isinstance(c, _ProbeCall) for c in probe.calls) >= 2:
                break
            await asyncio.sleep(0.01)
        _ = task.cancel()
        _ = task.cancel()
        with pytest.raises(
            iprojectactionrunner.ActionRunFailed, match="boom-double-cancel"
        ):
            await task

    assert _shared_dump_dirs() == before


async def test_fetch_failure_fails_run_without_starting_children(
    tmp_path: pathlib.Path, probe: _Probe
) -> None:
    """A raw-config fetch failure fails the run with a preparation error after
    exactly one attempt, and no child or directory is left behind."""
    provider = _provider(tmp_path)
    provider.raw_error = RuntimeError("nope")
    logger = _RecordingLogger()
    envs = [_env_info(provider.def_path, tmp_path, "env-0")]
    before = _shared_dump_dirs()

    with pytest.raises(
        iprojectactionrunner.ActionRunFailed,
        match="Failed to prepare the config dump of",
    ):
        await _run_create_envs(tmp_path, envs, provider=provider, logger=logger)

    assert len(provider.raw_calls) == 1
    assert probe.calls == []
    assert _shared_dump_dirs() == before


async def test_dump_dispatch_failure_fails_run_without_starting_children(
    tmp_path: pathlib.Path, probe: _Probe
) -> None:
    """A failing `dump_config` dispatch fails the run the same way: one fetch,
    no child runs, no directory left."""
    provider = _provider(tmp_path)
    logger = _RecordingLogger()
    envs = [_env_info(provider.def_path, tmp_path, "env-0")]
    before = _shared_dump_dirs()

    with pytest.raises(
        iprojectactionrunner.ActionRunFailed,
        match="Failed to prepare the config dump of",
    ):
        await _run_create_envs(
            tmp_path,
            envs,
            provider=provider,
            logger=logger,
            dump_handler_classes=[_RaisingDumpConfigHandler],
        )

    assert len(provider.raw_calls) == 1
    assert probe.calls == []
    assert _shared_dump_dirs() == before


async def test_cancellation_during_preparation_propagates_cancel(
    tmp_path: pathlib.Path, probe: _Probe
) -> None:
    """Cancelling the fetch ends the run as cancelled: no child starts and no
    directory remains."""
    provider = _provider(tmp_path)
    provider.block_fetch = True
    logger = _RecordingLogger()
    envs = [_env_info(provider.def_path, tmp_path, "env-0")]
    before = _shared_dump_dirs()

    async with handler_test_session(
        project_dir=tmp_path,
        actions=_session_actions(),
        service_overrides={
            iprojectinfoprovider.IProjectInfoProvider: provider,
            ilogger.ILogger: logger,
        },
    ) as session:
        task = asyncio.create_task(
            session.run_action(
                create_envs_action.CreateEnvsAction.__name__,
                create_envs_action.CreateEnvsRunPayload(envs=envs),
            )
        )
        for _ in range(100):
            if provider.raw_calls:
                break
            await asyncio.sleep(0.01)
        assert len(provider.raw_calls) == 1
        _ = task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert probe.calls == []
    assert _shared_dump_dirs() == before


async def test_removal_failure_warns_and_keeps_result(
    tmp_path: pathlib.Path,
    probe: _Probe,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An undeletable dump directory is a warning, not a new failure: the run
    keeps the result it would have had."""
    provider = _provider(tmp_path)
    logger = _RecordingLogger()
    envs = [_env_info(provider.def_path, tmp_path, "env-0")]

    def _boom(_path: pathlib.Path) -> None:
        raise OSError("device or resource busy")

    real_rmtree = shutil.rmtree
    monkeypatch.setattr(shutil, "rmtree", _boom)

    result = await _run_create_envs(tmp_path, envs, provider=provider, logger=logger)

    assert result is not None
    assert result.errors == []
    assert len(probe.calls) == 1
    assert probe.calls[0].uri is not None
    assert len(logger.warnings) == 1
    lent_grand = resource_uri_to_path(ResourceUri(probe.calls[0].uri)).parent.parent
    assert str(lent_grand) in logger.warnings[0]
    assert "Failed to remove the shared config dump directory" in logger.warnings[0]

    dump_uri = probe.calls[0].uri
    assert dump_uri is not None
    monkeypatch.undo()
    real_rmtree(resource_uri_to_path(ResourceUri(dump_uri)).parent.parent)


async def test_overlapping_runs_keep_separate_dumps(
    tmp_path: pathlib.Path, probe: _Probe
) -> None:
    """Two concurrent runs lend different directories and ending one run never
    removes the other's dump before its children end."""
    provider = _provider(tmp_path)
    logger = _RecordingLogger()
    probe.mode = "gate"
    envs_a = [_env_info(provider.def_path, tmp_path, "a-0")]
    envs_b = [_env_info(provider.def_path, tmp_path, "b-0")]
    before = _shared_dump_dirs()

    async with handler_test_session(
        project_dir=tmp_path,
        actions=_session_actions(),
        service_overrides={
            iprojectinfoprovider.IProjectInfoProvider: provider,
            ilogger.ILogger: logger,
        },
    ) as session:
        task_a = asyncio.create_task(
            session.run_action(
                create_envs_action.CreateEnvsAction.__name__,
                create_envs_action.CreateEnvsRunPayload(envs=envs_a),
            )
        )
        task_b = asyncio.create_task(
            session.run_action(
                create_envs_action.CreateEnvsAction.__name__,
                create_envs_action.CreateEnvsRunPayload(envs=envs_b),
            )
        )
        for _ in range(200):
            if len({c.uri for c in probe.calls if isinstance(c, _ProbeCall)}) >= 2:
                break
            await asyncio.sleep(0.01)
        maybe_uris = [c.uri for c in probe.calls if isinstance(c, _ProbeCall)]
        uris = [u for u in maybe_uris if u is not None]
        assert len(uris) == 2 and uris[0] != uris[1]
        dirs = [resource_uri_to_path(ResourceUri(u)).parent.parent for u in uris]
        assert all(d.exists() for d in dirs)

        _ = probe.gates[uris[0]].set()
        done, pending = await asyncio.wait(
            {task_a, task_b}, return_when=asyncio.FIRST_COMPLETED
        )
        assert len(done) == 1
        assert len(pending) == 1
        gone = [d for d in dirs if not d.exists()]
        kept = [d for d in dirs if d.exists()]
        assert len(gone) == 1 and len(kept) == 1

        _ = probe.gates[uris[1]].set()
        for task in pending:
            _ = await asyncio.wait_for(task, timeout=10)
        for task in done:
            _ = await task

    assert _shared_dump_dirs() == before


async def test_hand_built_context_warns_once_and_removes(
    tmp_path: pathlib.Path,
) -> None:
    """A run whose delegated-calls read is unconfirmed still cleans up, and
    logs exactly one warning naming the project directory."""
    def_path = tmp_path / "pyproject.toml"
    _ = def_path.write_text("[project]\n", encoding="utf-8")
    raw: dict[str, Any] = {"tool": {"finecode": {}}}
    recorded: list[str | None] = []

    class _Runner:
        async def run_action(
            self,
            *,
            action_type: Any,
            payload: Any,
            meta: Any,
            caller_kwargs: Any = None,
        ) -> Any:
            if isinstance(payload, dump_config_action.DumpConfigRunPayload):
                target = resource_uri_to_path(payload.target_file_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                _ = target.write_text("[project]\n", encoding="utf-8")
                return dump_config_action.DumpConfigRunResult(config_dump=raw)
            if isinstance(payload, create_env_action.CreateEnvRunPayload):
                recorded.append(_uri_str(payload.resolved_project_def_path))
                return create_envs_action.CreateEnvsRunResult(errors=[], created=True)
            recorded.append(None)
            return create_envs_action.CreateEnvsRunResult(errors=[], created=True)

    provider = _CountingProjectInfoProvider(raw, def_path)
    logger = _RecordingLogger()
    handler = CreateEnvsDispatchHandler(
        config=CreateEnvsDispatchHandlerConfig(),
        action_runner=cast(Any, _Runner()),
        logger=cast(Any, logger),
        project_info_provider=cast(Any, provider),
    )
    envs = [_env_info(def_path, tmp_path, "env-0")]
    payload = create_envs_action.CreateEnvsRunPayload(envs=envs)
    run_context = create_envs_action.CreateEnvsRunContext(
        run_id=1,
        initial_payload=payload,
        meta=code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.SYSTEM,
            dev_env=code_action.DevEnv.CI,
        ),
        info_provider=cast(Any, None),
    )
    await run_context.init()
    before = _shared_dump_dirs()

    result = await handler.run(payload, run_context)

    assert result.errors == []
    assert recorded and len(set(recorded)) == 1 and recorded[0] is not None
    assert logger.warnings == [
        (
            f"{def_path.parent}: a delegated call of this create_envs run may still be "
            "running, possibly a create_env handler still using the removed config dump"
        )
    ]
    assert _shared_dump_dirs() == before


async def test_session_run_logs_no_warning(
    tmp_path: pathlib.Path,
) -> None:
    """The same batch through a real ER setup ends stopped, so no unconfirmed
    warning is logged."""
    provider = _provider(tmp_path)
    logger = _RecordingLogger()
    envs = [_env_info(provider.def_path, tmp_path, "env-0")]

    result = await _run_create_envs(tmp_path, envs, provider=provider, logger=logger)

    assert result is not None
    assert result.errors == []
    assert logger.warnings == []


async def test_hand_built_context_two_projects_warns_with_both_dirs(
    tmp_path: pathlib.Path,
) -> None:
    """A hand-built two-project batch warns once naming every project dir in
    batch order, and removes the run directory."""
    def_a = tmp_path / "pyproject.toml"
    _ = def_a.write_text("[project]\n", encoding="utf-8")
    def_b = tmp_path / "x" / "proj" / "pyproject.toml"
    def_b.parent.mkdir(parents=True)
    _ = def_b.write_text("[project]\n", encoding="utf-8")
    raw: dict[str, Any] = {"tool": {"finecode": {}}}

    class _Runner:
        async def run_action(
            self,
            *,
            action_type: Any,
            payload: Any,
            meta: Any,
            caller_kwargs: Any = None,
        ) -> Any:
            if isinstance(payload, dump_config_action.DumpConfigRunPayload):
                target = resource_uri_to_path(payload.target_file_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                _ = target.write_text("[project]\n", encoding="utf-8")
                return dump_config_action.DumpConfigRunResult(config_dump=raw)
            return create_envs_action.CreateEnvsRunResult(errors=[], created=True)

    class _TwoProvider(_CountingProjectInfoProvider):
        async def get_project_raw_config(
            self, project_def_path: pathlib.Path
        ) -> dict[str, Any]:
            self.raw_calls.append(project_def_path)
            return {}

    provider = _TwoProvider({}, def_a)
    logger = _RecordingLogger()
    handler = CreateEnvsDispatchHandler(
        config=CreateEnvsDispatchHandlerConfig(),
        action_runner=cast(Any, _Runner()),
        logger=cast(Any, logger),
        project_info_provider=cast(Any, provider),
    )
    envs = [_env_info(def_a, tmp_path, "a-0"), _env_info(def_b, tmp_path, "b-0")]
    payload = create_envs_action.CreateEnvsRunPayload(envs=envs)
    run_context = create_envs_action.CreateEnvsRunContext(
        run_id=1,
        initial_payload=payload,
        meta=code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.SYSTEM,
            dev_env=code_action.DevEnv.CI,
        ),
        info_provider=cast(Any, None),
    )
    await run_context.init()
    before = _shared_dump_dirs()

    result = await handler.run(payload, run_context)

    assert result.errors == []
    assert logger.warnings == [
        f"{def_a.parent}, {def_b.parent}: a delegated call of this create_envs run may still be "
        "running, possibly a create_env handler still using the removed config dump"
    ]
    assert _shared_dump_dirs() == before


async def test_hand_built_preparation_failure_logs_no_warning(
    tmp_path: pathlib.Path,
) -> None:
    """A preparation failure through a hand-built (unconfirmed) context removes
    the run directory without the unconfirmed warning."""
    def_path = tmp_path / "pyproject.toml"
    _ = def_path.write_text("[project]\n", encoding="utf-8")

    class _Runner:
        async def run_action(
            self,
            *,
            action_type: Any,
            payload: Any,
            meta: Any,
            caller_kwargs: Any = None,
        ) -> Any:
            if isinstance(payload, dump_config_action.DumpConfigRunPayload):
                raise RuntimeError("dump-boom")
            raise AssertionError("no child must start")

    class _FailProvider(_CountingProjectInfoProvider):
        async def get_project_raw_config(
            self, project_def_path: pathlib.Path
        ) -> dict[str, Any]:
            self.raw_calls.append(project_def_path)
            raise RuntimeError("fetch-boom")

    provider = _FailProvider({}, def_path)
    logger = _RecordingLogger()
    handler = CreateEnvsDispatchHandler(
        config=CreateEnvsDispatchHandlerConfig(),
        action_runner=cast(Any, _Runner()),
        logger=cast(Any, logger),
        project_info_provider=cast(Any, provider),
    )
    envs = [_env_info(def_path, tmp_path, "env-0")]
    payload = create_envs_action.CreateEnvsRunPayload(envs=envs)
    run_context = create_envs_action.CreateEnvsRunContext(
        run_id=1,
        initial_payload=payload,
        meta=code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.SYSTEM,
            dev_env=code_action.DevEnv.CI,
        ),
        info_provider=cast(Any, None),
    )
    await run_context.init()
    before = _shared_dump_dirs()

    with pytest.raises(code_action.ActionFailedException):
        await handler.run(payload, run_context)

    assert _shared_dump_dirs() == before
    assert logger.warnings == []
