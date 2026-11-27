"""Probe handlers for #993 shared-config-dump cross-ER tests.

Test-local packaging: copied into a temporary fixture-module directory and
made importable in copied ER venvs via a ``.pth`` file. Imports the standard
library, ``finecode_extension_api`` and ``fine_envs`` (installed editable in
every copied venv).

Coordination is a JSON file next to the project (``probe-config.json``),
written by the test before the run: ``{"log_path": ..., "mode": ...}``.
``EnvInfo`` is fixed by the action contract, so per-test behavior travels
through that file, which the handlers locate through
``IProjectInfoProvider.get_current_project_dir_path()``. Every handler
appends one JSON line per event to ``log_path``. Timestamps are
``time.time()`` (same host as the probe).

``create_env`` modes (``mode`` key, or ``raise_envs``/``stubborn_envs`` lists
matched by env name):

- ``normal``: record the lent URI and whether its ``pyproject.toml`` carries
  the shared-dump marker, then succeed.
- ``raise``/``raise_envs``: fail at once with ``probe-child-boom``.
- ``cooperative``: block until the release file appears; on cancellation
  record it, clean up briefly and re-raise.
- ``stubborn``: on cancellation record it, ignore the cancel for
  ``stubborn_seconds`` (default ``B + 3``) and then succeed.
- ``nested``: delegate once to the stubborn leaf in the third env, cancel
  that nested await shortly after, swallow the cancellation and succeed, so
  the reply carries ``subtreeStopped: false``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import time

from fine_envs import create_env_action, dump_config_action
from fine_envs.create_envs_action import CreateEnvsRunResult
from finecode_extension_api.interfaces import (
    iprojectactionrunner,
    iprojectinfoprovider,
)
from finecode_extension_api.resource_uri import resource_uri_to_path

from finecode_extension_api import code_action

B = 5.0
CONFIG_NAME = "probe-config.json"
MARKER = "tool-shared-dump-marker"


def _event(log_path: str, who: str, what: str, **extra: object) -> None:
    if not log_path:
        return
    line = {"t": time.time(), "who": who, "what": what, "pid": os.getpid(), **extra}
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


def _read_config(project_dir: str) -> dict:
    with open(os.path.join(project_dir, CONFIG_NAME), encoding="utf-8") as f:
        return json.load(f)


def _log_has(log_path: str, who: str, what: str) -> bool:
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("who") == who and event.get("what") == what:
                    return True
    except OSError:
        return False
    return False


@dataclasses.dataclass
class ProbeCreateEnvHandlerConfig(code_action.ActionHandlerConfig): ...


class ProbeCreateEnvHandler(
    code_action.ActionHandler[
        create_env_action.CreateEnvAction, ProbeCreateEnvHandlerConfig
    ]
):
    """Recording `create_env` handler; behavior comes from the side-channel."""

    def __init__(
        self,
        project_info_provider: iprojectinfoprovider.IProjectInfoProvider,
        action_runner: iprojectactionrunner.IProjectActionRunner,
    ) -> None:
        self._info = project_info_provider
        self._runner = action_runner

    def _config(self) -> tuple[str, dict, str]:
        project_dir = str(self._info.get_current_project_dir_path())
        config = _read_config(project_dir)
        return project_dir, config, str(config.get("log_path", ""))

    async def _cooperative(
        self, log: str, name: str, release: str, uri: str | None
    ) -> CreateEnvsRunResult:
        _event(log, name, "started", uri=uri)
        try:
            while not os.path.exists(release):
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            _event(log, name, "cancel_seen", uri=uri)
            await asyncio.sleep(0.3)
            _event(log, name, "cleanup_end", uri=uri)
            raise
        _event(log, name, "released", uri=uri)
        return CreateEnvsRunResult(errors=[], created=True)

    async def _stubborn(
        self, log: str, name: str, uri: str | None, seconds: float
    ) -> CreateEnvsRunResult:
        _event(log, name, "started", uri=uri)
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            _event(log, name, "cancel_seen", uri=uri)
            await asyncio.sleep(seconds)
            _event(log, name, "stubborn_end", uri=uri)
            return CreateEnvsRunResult(errors=[], created=True)
        _event(log, name, "end", uri=uri)
        return CreateEnvsRunResult(errors=[], created=True)

    async def _nested(
        self, log: str, name: str, uri: str | None, config: dict
    ) -> CreateEnvsRunResult:
        _event(log, name, "started", uri=uri)
        ref = iprojectactionrunner.ActionRef(
            source="shared_config_dump_probe.ProbeStubbornLeafAction",
            result_type=ProbeLeafResult,
            action_type=None,
        )
        task = asyncio.ensure_future(
            self._runner.run_action(
                ref,
                ProbeLeafPayload(
                    log_path=log,
                    sleep_seconds=float(config.get("nested_sleep", B + 3.0)),
                ),
                code_action.RunActionMeta(
                    trigger=code_action.RunActionTrigger.SYSTEM,
                    dev_env=code_action.DevEnv.CI,
                ),
            )
        )
        deadline = time.monotonic() + 55.0
        while not _log_has(log, "leaf", "started"):
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        _event(log, name, "nested_cancelled", uri=uri)
        return CreateEnvsRunResult(errors=[], created=True)

    async def run(
        self,
        payload: create_env_action.CreateEnvRunPayload,
        run_context: create_env_action.CreateEnvRunContext,
    ) -> CreateEnvsRunResult:
        _, config, log = self._config()
        name = payload.env.name
        uri_obj = payload.resolved_project_def_path
        uri = str(uri_obj) if uri_obj is not None else None
        if name in config.get("raise_envs", []):
            _event(log, name, "started", uri=uri)
            raise RuntimeError("probe-child-boom")
        mode = config.get("mode", "normal")
        if mode == "cooperative":
            return await self._cooperative(
                log, name, str(config.get("release", "")), uri
            )
        if mode == "stubborn":
            return await self._stubborn(
                log, name, uri, float(config.get("stubborn_seconds", B + 3.0))
            )
        if mode == "nested":
            return await self._nested(log, name, uri, config)
        _event(log, name, "saw-uri", uri=uri)
        marker = False
        is_file = False
        file_name = None
        if uri_obj is not None:
            dump_path = resource_uri_to_path(uri_obj)
            is_file = dump_path.is_file()
            file_name = dump_path.name
            text = dump_path.read_text(encoding="utf-8")
            marker = MARKER in text
        _event(
            log,
            name,
            "marker",
            uri=uri,
            marker=marker,
            is_file=is_file,
            file_name=file_name,
        )
        return CreateEnvsRunResult(errors=[], created=True)


class ProbeStubbornCreateEnvHandler(
    code_action.ActionHandler[
        create_env_action.CreateEnvAction, ProbeCreateEnvHandlerConfig
    ]
):
    """Always-stubborn `create_env` segment for the mixed-env test."""

    def __init__(
        self, project_info_provider: iprojectinfoprovider.IProjectInfoProvider
    ) -> None:
        self._info = project_info_provider

    async def run(
        self,
        payload: create_env_action.CreateEnvRunPayload,
        run_context: create_env_action.CreateEnvRunContext,
    ) -> CreateEnvsRunResult:
        project_dir = str(self._info.get_current_project_dir_path())
        log = str(_read_config(project_dir).get("log_path", ""))
        name = payload.env.name
        _event(log, name, "started")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            _event(log, name, "cancel_seen")
            await asyncio.sleep(B + 3.0)
            _event(log, name, "stubborn_end")
            return CreateEnvsRunResult(errors=[], created=True)
        return CreateEnvsRunResult(errors=[], created=True)


class ProbeCooperativeCreateEnvHandler(
    code_action.ActionHandler[
        create_env_action.CreateEnvAction, ProbeCreateEnvHandlerConfig
    ]
):
    """Always-cooperative `create_env` segment for the mixed-env test."""

    def __init__(
        self, project_info_provider: iprojectinfoprovider.IProjectInfoProvider
    ) -> None:
        self._info = project_info_provider

    async def run(
        self,
        payload: create_env_action.CreateEnvRunPayload,
        run_context: create_env_action.CreateEnvRunContext,
    ) -> CreateEnvsRunResult:
        project_dir = str(self._info.get_current_project_dir_path())
        log = str(_read_config(project_dir).get("log_path", ""))
        name = payload.env.name
        _event(log, name, "started")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            _event(log, name, "cancel_seen")
            await asyncio.sleep(0.3)
            _event(log, name, "cleanup_end")
            raise
        return CreateEnvsRunResult(errors=[], created=True)


@dataclasses.dataclass
class ProbeLeafPayload(code_action.RunActionPayload):
    log_path: str = ""
    sleep_seconds: float = B + 3.0


@dataclasses.dataclass
class ProbeLeafResult(code_action.RunActionResult):
    pass


class ProbeLeafContext(code_action.RunActionContext[ProbeLeafPayload]): ...


class ProbeStubbornLeafAction(
    code_action.Action[ProbeLeafPayload, ProbeLeafContext, ProbeLeafResult]
):
    PAYLOAD_TYPE = ProbeLeafPayload
    RUN_CONTEXT_TYPE = ProbeLeafContext
    RESULT_TYPE = ProbeLeafResult
    DESCRIPTION = "#993 stubborn leaf for the nested-unconfirmed test."


class ProbeStubbornLeafHandler(
    code_action.ActionHandler[ProbeStubbornLeafAction, code_action.ActionHandlerConfig]
):
    async def run(
        self, payload: ProbeLeafPayload, run_context: ProbeLeafContext
    ) -> ProbeLeafResult:
        _event(payload.log_path, "leaf", "started")
        try:
            await asyncio.sleep(payload.sleep_seconds)
        except asyncio.CancelledError:
            _event(payload.log_path, "leaf", "cancel_seen")
            await asyncio.sleep(payload.sleep_seconds)
            _event(payload.log_path, "leaf", "stubborn_end")
            return ProbeLeafResult()
        _event(payload.log_path, "leaf", "end")
        return ProbeLeafResult()


@dataclasses.dataclass
class ProbeDumpMarkerHandlerConfig(code_action.ActionHandlerConfig): ...


class ProbeDumpMarkerHandler(
    code_action.ActionHandler[
        dump_config_action.DumpConfigAction, ProbeDumpMarkerHandlerConfig
    ]
):
    """Test-only `dump_config` tail handler: appends a marker table to the
    saved file and records the dump directory, so tests can prove every
    consumer ran on it.

    It runs in another env after the save handler (cross-env sequential
    segments do not share run-context content, so it edits the file the
    save handler wrote instead of touching the context).
    """

    def __init__(
        self, project_info_provider: iprojectinfoprovider.IProjectInfoProvider
    ) -> None:
        self._info = project_info_provider

    async def run(
        self,
        payload: dump_config_action.DumpConfigRunPayload,
        run_context: dump_config_action.DumpConfigRunContext,
    ) -> dump_config_action.DumpConfigRunResult:
        project_dir = str(self._info.get_current_project_dir_path())
        log = str(_read_config(project_dir).get("log_path", ""))
        target = resource_uri_to_path(payload.target_file_path)
        text = target.read_text(encoding="utf-8")
        if MARKER not in text:
            text += "\n[tool-shared-dump-marker]\npresent = true\n"
            target.write_text(text, encoding="utf-8")
        _event(log, "dump-marker", "dumped", dir=str(target.parent))
        return dump_config_action.DumpConfigRunResult(
            config_dump=run_context.raw_config_dump
        )
