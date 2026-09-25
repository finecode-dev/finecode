from __future__ import annotations

import pathlib
import tempfile
import tomllib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from finecode_extension_api import code_action
from finecode_extension_api.interfaces import ifileeditor

from finecode_extension_runner import context, domain, schemas
from finecode_extension_runner import services as services_module
from finecode_extension_runner._services import run_action as run_action_service
from finecode_extension_runner.di import bootstrap as di_bootstrap
from finecode_extension_runner.di.registry import Registry
from finecode_extension_runner.testing._file_editor import InMemoryFileEditor
from finecode_extension_runner.testing._senders import (
    CollectingPartialResultSender,
    CollectingProgressSender,
)
from finecode_extension_runner.testing._stubs import (
    InMemoryWalWriter,
    NullWalWriter,
    WalEvent,
)


def _build_actions(
    actions_dict: dict[str, dict],
) -> dict[str, domain.ActionDeclaration]:
    result: dict[str, domain.ActionDeclaration] = {}
    for name, action_data in actions_dict.items():
        handlers = [
            domain.ActionHandlerDeclaration(
                name=h["name"],
                source=h["source"],
                config=h.get("config") or {},
                env=h.get("env"),
            )
            for h in action_data.get("handlers", [])
        ]
        result[name] = domain.ActionDeclaration(
            name=name,
            config=action_data.get("config") or {},
            handlers=handlers,
            source=action_data.get("source", ""),
        )
    return result


async def _default_raw_config_getter(project_def_path: str) -> dict:
    """Reads the project's own file from disk.

    With no presets and no interpreter matrix in play, a project's resolved config is
    its own file -- this default keeps ``IProjectInfoProvider.get_project_raw_config``
    truthful for tests that declare an env directly in the project's ``pyproject.toml``
    (the common case), without every such test needing its own ``IProjectInfoProvider``
    override.

    It is **not** faithful beyond that, and the gap is easy to miss: in production the
    WM serves a *resolved* config, with presets merged and interpreter matrices already
    expanded into ``<base>@<impl>-<version>`` children (ADR-0047, see
    ``IProjectInfoProvider.get_project_raw_config``). This default reproduces neither.
    A test whose project declares ``interpreters`` therefore gets a config shape that
    never reaches a real handler. Override ``IProjectInfoProvider`` and supply the
    expanded shape for those, as the ``sync_python_interpreters`` tests do.
    """
    path = pathlib.Path(project_def_path)
    if not path.exists():
        return {}
    with path.open("rb") as file:
        return tomllib.load(file)


class Session:
    """Bound test session.  Wraps a ``RunnerContext`` and exposes helpers for
    running actions and asserting on results without going through IPC.
    """

    def __init__(
        self,
        runner_context: context.RunnerContext,
        service_overrides: dict[type, Any] | None = None,
    ) -> None:
        self._runner_context = runner_context
        self._service_overrides = service_overrides or {}
        self.partial_results = CollectingPartialResultSender()
        self.progress = CollectingProgressSender()

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def file_editor(self) -> InMemoryFileEditor | None:
        return self._service_overrides.get(ifileeditor.IFileEditor)  # type: ignore[return-value]

    @property
    def wal_events(self) -> list[WalEvent]:
        wal = self._runner_context.wal_writer
        if isinstance(wal, InMemoryWalWriter):
            return wal.events
        return []

    async def service(self, type_: type) -> Any:
        return await self._runner_context.di_registry.get_instance(type_)

    # ------------------------------------------------------------------
    # Action runners
    # ------------------------------------------------------------------

    async def run_action(
        self,
        action_name: str,
        payload: code_action.RunActionPayload | None = None,
        *,
        caller_kwargs: code_action.CallerRunContextKwargs | None = None,
        partial_result_token: int | str | None = None,
    ) -> code_action.RunActionResult | None:
        try:
            action_def = self._runner_context.project.actions[action_name]
        except KeyError:
            raise ValueError(f"Action '{action_name}' not found in test session")

        meta = code_action.RunActionMeta(
            trigger=code_action.RunActionTrigger.SYSTEM,
            dev_env=code_action.DevEnv.CI,
            wal_run_id=str(uuid.uuid4()),
        )
        return await run_action_service.run_action(
            action_def=action_def,
            payload=payload,
            meta=meta,
            runner_context=self._runner_context,
            partial_result_token=partial_result_token,
            caller_kwargs=caller_kwargs,
            progress_sender=self.progress,
            partial_result_observer=self.partial_results,
        )

    async def run_handlers(
        self,
        action_name: str,
        handler_names: list[str],
        payload: dict | None = None,
        *,
        previous_result: dict | None = None,
        previous_context: dict | None = None,
    ) -> schemas.RunHandlersResponse:
        wal_run_id = str(uuid.uuid4())
        request = schemas.RunHandlersRequest(
            action_name=action_name,
            handler_names=handler_names,
            params=payload or {},
            previous_result=previous_result,
            previous_context=previous_context,
        )
        options = schemas.RunActionOptions(
            run_id=wal_run_id,
            meta=code_action.RunActionMeta(
                trigger=code_action.RunActionTrigger.SYSTEM,
                dev_env=code_action.DevEnv.CI,
                wal_run_id=wal_run_id,
            ),
        )
        return await run_action_service.run_handlers_raw(
            request=request,
            options=options,
            runner_context=self._runner_context,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _shutdown(self) -> None:
        services_module.shutdown_all_action_handlers(self._runner_context)


@asynccontextmanager
async def handler_test_session(
    *,
    project_dir: pathlib.Path,
    actions: dict[str, dict],
    handler_configs: dict[str, dict] | None = None,
    service_overrides: dict[type, Any] | None = None,
    service_declarations: list[schemas.ServiceDeclaration] | None = None,
    service_config_overrides: dict[str, dict[str, Any]] | None = None,
    project_name: str = "test_project",
    wal: Literal["null", "memory"] = "null",
) -> AsyncIterator[Session]:
    """Async context manager that boots a real ER in the test process.

    Example::

        async with handler_test_session(
            project_dir=tmp_path,
            actions={"fmt": {"source": "...", "handlers": [...]}},
            service_overrides={IFileEditor: InMemoryFileEditor()},
        ) as session:
            result = await session.run_action("fmt", payload)
            assert result.changed
    """
    domain_actions = _build_actions(actions)
    project = domain.Project(
        name=project_name,
        dir_path=project_dir,
        def_path=project_dir / "pyproject.toml",
        actions=domain_actions,
        action_handler_configs=handler_configs or {},
    )

    wal_writer: NullWalWriter | InMemoryWalWriter = (
        NullWalWriter() if wal == "null" else InMemoryWalWriter()
    )
    registry = Registry()
    runner_context = context.RunnerContext(
        project=project,
        di_registry=registry,
        wal_writer=wal_writer,
    )

    handler_packages = {
        h["source"].split(".")[0]
        for action_data in actions.values()
        for h in action_data.get("handlers", [])
        if h.get("source")
    }
    cache_dir = project_dir / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    di_bootstrap.bootstrap(
        registry=registry,
        runner_context=runner_context,
        project_def_path_getter=lambda: project_dir / "pyproject.toml",
        project_raw_config_getter=_default_raw_config_getter,
        current_project_raw_config_version_getter=lambda: 0,
        cache_dir_path_getter=lambda: cache_dir,
        actions_getter=lambda: runner_context.project.actions,
        current_env_name_getter=lambda: "test",
        handler_packages=handler_packages,
        service_declarations=service_declarations or [],
        service_config_overrides=service_config_overrides or {},
        send_request_to_wm=None,
    )

    for type_, instance in (service_overrides or {}).items():
        registry.register_instance(type_, instance, override=True)

    session = Session(
        runner_context=runner_context, service_overrides=service_overrides
    )
    try:
        yield session
    finally:
        session._shutdown()


async def run_handler(
    handler_cls: type,
    payload: code_action.RunActionPayload | None = None,
    *,
    action_cls: type,
    project_dir: pathlib.Path | None = None,
    service_overrides: dict[type, Any] | None = None,
    handler_config: dict | None = None,
) -> code_action.RunActionResult | None:
    """One-shot helper for testing a single handler without managing a session.

    Builds a minimal ``handler_test_session`` with a single action that has
    a single handler, runs it, and tears down.

    Example::

        result = await run_handler(
            RuffFormatFileHandler,
            FormatFileRunPayload(file_path=path, save=False),
            action_cls=FormatPythonFileAction,
            service_overrides={IFileEditor: editor},
        )
    """

    action_source = f"{action_cls.__module__}.{action_cls.__qualname__}"
    handler_source = f"{handler_cls.__module__}.{handler_cls.__qualname__}"
    action_name = action_cls.__name__

    actions = {
        action_name: {
            "source": action_source,
            "handlers": [
                {
                    "name": handler_cls.__name__,
                    "source": handler_source,
                    "config": handler_config or {},
                }
            ],
        }
    }

    if project_dir is not None:
        async with handler_test_session(
            project_dir=project_dir,
            actions=actions,
            service_overrides=service_overrides,
        ) as session:
            return await session.run_action(action_name, payload)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            async with handler_test_session(
                project_dir=pathlib.Path(tmp),
                actions=actions,
                service_overrides=service_overrides,
            ) as session:
                return await session.run_action(action_name, payload)
