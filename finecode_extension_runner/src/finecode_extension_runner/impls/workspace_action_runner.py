from __future__ import annotations

import dataclasses
import pathlib
import typing
from collections.abc import Awaitable, Callable
from typing import Any

import cattrs.errors
from finecode_extension_api import code_action
from finecode_extension_api.interfaces import (
    iprojectactionrunner,
    iworkspaceactionrunner,
)

from finecode_extension_runner import (
    coverage_sink,
    er_telemetry,
    run_context,
)
from finecode_extension_runner._converter import converter as _converter

PayloadT = typing.TypeVar("PayloadT", bound=code_action.RunActionPayload)
ResultT = typing.TypeVar("ResultT", bound=code_action.RunActionResult)


class WorkspaceActionRunnerImpl(iworkspaceactionrunner.IWorkspaceActionRunner):
    """Calls the WM back-channel finecode/runActionInWorkspace."""

    def __init__(
        self, send_request_to_wm: Callable[[str, dict], Awaitable[Any]]
    ) -> None:
        self._send = send_request_to_wm

    async def run_action_in_projects(
        self,
        action_type: type[code_action.Action[PayloadT, typing.Any, ResultT]],
        payload: PayloadT,
        meta: code_action.RunActionMeta,
        project_paths: list[pathlib.Path] | None = None,
        concurrently: bool = True,
    ) -> dict[pathlib.Path, ResultT]:
        action_source = f"{action_type.__module__}.{action_type.__qualname__}"
        traceparent = er_telemetry.get_current_traceparent()
        try:
            raw = await self._send(
                "finecode/runActionInWorkspace",
                {
                    "actionSource": action_source,
                    "payload": dataclasses.asdict(payload),
                    "meta": {
                        "trigger": meta.trigger.value,
                        "devEnv": meta.dev_env.value,
                        "orchestrationDepth": meta.orchestration_depth,
                    },
                    "projectPaths": [p.as_posix() for p in project_paths]
                    if project_paths is not None
                    else None,
                    "concurrently": concurrently,
                    "traceparent": traceparent,
                    # Names the run this fan-out belongs to, so a question asked
                    # by any project it reaches is still addressed to the client
                    # that started the whole thing (ADR-0082 rule 1).
                    "runId": run_context.current_run_id(),
                },
            )
        except Exception as e:
            project_str = (
                ", ".join(str(p) for p in project_paths)
                if project_paths is not None
                else "all workspace projects"
            )
            raise iprojectactionrunner.ActionRunFailed(
                f"Running '{action_type.__name__}' in [{project_str}] failed: {e}"
            ) from e
        results = self._decode_results(action_type, raw)
        # Workspace fan-out return point: every per-project result
        # arrived with its coverage serialized inside it; carry it into the
        # calling run's sink.
        for result in results.values():
            coverage_sink.deposit_from(result)
        return results

    async def run_action_per_project(
        self,
        action_type: type[code_action.Action[PayloadT, typing.Any, ResultT]],
        payload_by_project: dict[pathlib.Path, PayloadT],
        meta: code_action.RunActionMeta,
        concurrently: bool = True,
    ) -> dict[pathlib.Path, ResultT]:
        action_source = f"{action_type.__module__}.{action_type.__qualname__}"
        traceparent = er_telemetry.get_current_traceparent()
        try:
            raw = await self._send(
                "finecode/runActionInWorkspace",
                {
                    "actionSource": action_source,
                    # An empty base payload plus complete per-project overrides
                    # is exactly a per-project payload: the WM shallow-merges
                    # `{**payload, **overrides[project]}`.
                    "payload": {},
                    "meta": {
                        "trigger": meta.trigger.value,
                        "devEnv": meta.dev_env.value,
                        "orchestrationDepth": meta.orchestration_depth,
                    },
                    "projectPaths": [p.as_posix() for p in payload_by_project],
                    "payloadOverridesByProject": {
                        p.as_posix(): dataclasses.asdict(payload)
                        for p, payload in payload_by_project.items()
                    },
                    "concurrently": concurrently,
                    "traceparent": traceparent,
                    "runId": run_context.current_run_id(),
                },
            )
        except Exception as e:
            project_str = ", ".join(str(p) for p in payload_by_project)
            raise iprojectactionrunner.ActionRunFailed(
                f"Running '{action_type.__name__}' in [{project_str}] failed: {e}"
            ) from e
        results = self._decode_results(action_type, raw)
        # Workspace per-project return point: same pick-up as
        # run_action_in_projects — coverage rode inside the serialized results.
        for result in results.values():
            coverage_sink.deposit_from(result)
        return results

    def _decode_results(
        self,
        action_type: type[code_action.Action[PayloadT, typing.Any, ResultT]],
        raw: dict,
    ) -> dict[pathlib.Path, ResultT]:
        results_by_project: dict = raw["resultsByProject"]
        results: dict[pathlib.Path, ResultT] = {}
        for k, v in results_by_project.items():
            raw_entry = next(iter(v.values()), None)
            if raw_entry is None or raw_entry.get("status") == "no_handlers":
                continue
            raw_result = raw_entry.get("result")
            if raw_result is None:
                raise iprojectactionrunner.ActionRunFailed(
                    f"'{action_type.__name__}' handler returned no result for project '{k}' "
                    f"(status={raw_entry.get('status')}). The handler must always send a result."
                )
            try:
                results[pathlib.Path(k)] = _converter.structure(
                    raw_result, action_type.RESULT_TYPE
                )
            except cattrs.errors.ClassValidationError as e:
                details = "; ".join(cattrs.transform_error(e))
                raise iprojectactionrunner.ActionRunFailed(
                    f"Failed to parse result of '{action_type.__name__}' for project '{k}': {details}"
                ) from e
        return results
