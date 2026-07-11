"""WM-side variant-keyed fan-out for matrixed actions (PRD-0003 Gap #3, Approach A).

An action is *matrixed* when its handlers bind to concrete per-interpreter
envs produced by interpreter-matrix config expansion (ADR-0047) — i.e. at
least one of ``action.handlers`` carries a non-``None`` ``interpreter``.
Config-time validation (see ``interpreter_matrix.validate``) already
guarantees that for a given action either ALL handlers are interpreter-bound
or NONE are, so ``run_matrix_action`` only needs to group the handlers that
are bound and run one variant per group.

Each interpreter variant is run in its own Extension Runner process — the WM
cannot import extension-defined ``RunActionResult`` types, so it only ever
sees serialized ``RunActionResponse`` objects (``result_by_format`` dicts).
Combination therefore happens here, WM-side, over those serialized
responses — mirroring the existing multi-project combiner rather than reconstructing
typed ``RunActionResult`` objects.

This module must stay free of upper-layer imports so it
can be imported from ``proxy_utils`` without a cycle: it takes the actual
per-handler-set executor (``proxy_utils._execute_action``) as the
``run_variant`` callback rather than importing ``proxy_utils`` itself.
"""

from __future__ import annotations

import asyncio
import copy
import typing

from finecode.wm_server import domain
from finecode.wm_server.config import interpreter_matrix
from finecode.wm_server.config.interpreter_matrix import Interpreter
from finecode.wm_server.runner import runner_client
from finecode.wm_server.runner.runner_client import RunActionResponse
from finecode.wm_server.services.run_service.exceptions import ActionRunFailed

__all__ = ["run_matrix_action", "is_matrixed"]


RunVariant = typing.Callable[..., typing.Awaitable[RunActionResponse]]


def is_matrixed(action: domain.Action) -> bool:
    """True when *action* is matrixed (Gap #3): at least one of its handlers
    binds to a concrete per-interpreter env produced by interpreter-matrix
    expansion (ADR-0047), i.e. carries a non-``None`` ``interpreter``.

    Config-time validation (``interpreter_matrix.validate``) guarantees that
    for a given action either ALL handlers are interpreter-bound or NONE are,
    so checking any single handler is sufficient.
    """
    return any(handler.interpreter for handler in action.handlers)


def _group_handlers_by_interpreter(
    action: domain.Action,
) -> dict[Interpreter, list[domain.ActionHandler]]:
    """Group *action*'s handlers by interpreter, preserving declaration order.

    Handlers without an ``interpreter`` (should not occur for a matrixed
    action per the config-time no-mixing invariant, but guarded here anyway)
    are skipped.
    """
    groups: dict[Interpreter, list[domain.ActionHandler]] = {}
    for handler in action.handlers:
        if handler.interpreter is None:
            continue
        interpreter = interpreter_matrix.parse_interpreter(handler.interpreter)
        groups.setdefault(interpreter, []).append(handler)
    return groups


async def _run_variant_safe(
    interpreter: Interpreter,
    variant_action: domain.Action,
    run_variant: RunVariant,
    **kwargs: typing.Any,
) -> RunActionResponse:
    """Run one interpreter variant, converting any exception into a synthetic
    failed response so one variant's failure never aborts the others."""
    try:
        return await run_variant(action=variant_action, **kwargs)
    except Exception as exc:
        return RunActionResponse(
            result_by_format={"string": f"error: {exc}", "json": {"error": str(exc)}},
            return_code=1,
            status="error",
        )


def _flatten_styled_text_json(value: typing.Any) -> str:
    """Best-effort plain-text flatten of a ``styled_text_json`` payload.

    ``styled_text_json`` is an extension-defined styled-text serialization
    this module cannot import a proper renderer for (it must stay free of
    upper-layer / extension-api imports). Concatenating any string values
    found while walking the raw JSON structure is good enough for the
    combined ``"string"`` format's MVP purposes.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "".join(_flatten_styled_text_json(v) for v in value.values())
    if isinstance(value, list):
        return "".join(_flatten_styled_text_json(v) for v in value)
    return ""


def _variant_text(response: RunActionResponse) -> str:
    string_value = response.result_by_format.get("string")
    if isinstance(string_value, str):
        return string_value

    styled_text_json = response.result_by_format.get("styled_text_json")
    if styled_text_json is not None:
        return _flatten_styled_text_json(styled_text_json)

    return ""


def _combine_variant_responses(
    variants: dict[Interpreter, RunActionResponse],
) -> RunActionResponse:
    """Combine per-interpreter ``RunActionResponse``s into ONE variant-keyed response."""
    return_code = 0
    for response in variants.values():
        return_code |= response.return_code

    result_by_format: dict[str, typing.Any] = {}

    has_json = any(
        "json" in response.result_by_format for response in variants.values()
    )
    if has_json:
        result_by_format["json"] = {
            interpreter.canonical: response.result_by_format.get("json")
            for interpreter, response in variants.items()
        }

    has_text = any(
        "string" in response.result_by_format
        or "styled_text_json" in response.result_by_format
        for response in variants.values()
    )
    if has_text:
        result_by_format["string"] = "\n".join(
            f"=== {interpreter.canonical} ===\n{_variant_text(response)}"
            for interpreter, response in variants.items()
        )

    return RunActionResponse(
        result_by_format=result_by_format,
        return_code=return_code,
        status="success",
    )


async def run_matrix_action(
    *,
    action: domain.Action,
    action_name: str,
    payload: dict[str, typing.Any],
    project_def: domain.Project,
    ws_context: typing.Any,
    run_trigger: runner_client.RunActionTrigger,
    dev_env: runner_client.DevEnv,
    result_formats: list[runner_client.RunResultFormat],
    initialize_all_handlers: bool,
    progress_token: int | str | None,
    wal_run_id: str,
    traceparent: str | None,
    orchestration_depth: int,
    caller_kwargs: dict | None,
    run_variant: RunVariant,
    selected_interpreters: set[str] | None = None,
) -> RunActionResponse:
    """Fan a matrixed action out to one run per interpreter and combine the results.

    Runs the FULL declared interpreter axis, unless *selected_interpreters* is
    given (PRD-0003 AC8) — a set of interpreter canonicals (``"<impl>@<version>"``)
    to restrict the fan-out to. Each interpreter's handler subset is run via
    *run_variant* (``proxy_utils._execute_action``, passed in to avoid a
    circular import) in its own ER process, concurrently; a variant that
    raises never aborts the others (see ``_run_variant_safe``).

    Raises:
        ActionRunFailed: *selected_interpreters* names an interpreter not in
            the action's declared axis.
    """
    groups = _group_handlers_by_interpreter(action)

    if selected_interpreters is None:
        selected = groups
    else:
        unknown = selected_interpreters - {it.canonical for it in groups}
        if unknown:
            raise ActionRunFailed(
                f"selected interpreters not in axis: {sorted(unknown)}"
            )
        selected = {
            it: hs for it, hs in groups.items() if it.canonical in selected_interpreters
        }

    tasks = []
    interpreters: list[Interpreter] = []
    for interpreter, handlers in selected.items():
        variant_action = copy.copy(action)
        variant_action.handlers = handlers
        interpreters.append(interpreter)
        tasks.append(
            _run_variant_safe(
                interpreter,
                variant_action,
                run_variant,
                action_name=action_name,
                payload=payload,
                project_def=project_def,
                ws_context=ws_context,
                run_trigger=run_trigger,
                dev_env=dev_env,
                result_formats=result_formats,
                initialize_all_handlers=initialize_all_handlers,
                # Progress aggregation across variants is deferred (MVP).
                progress_token=None,
                wal_run_id=wal_run_id,
                traceparent=traceparent,
                orchestration_depth=orchestration_depth,
                caller_kwargs=caller_kwargs,
                allow_no_handlers=False,
            )
        )

    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    variants: dict[Interpreter, RunActionResponse] = {}
    for interpreter, result in zip(interpreters, raw_results):
        if isinstance(result, BaseException):
            variants[interpreter] = RunActionResponse(
                result_by_format={
                    "string": f"error: {result}",
                    "json": {"error": str(result)},
                },
                return_code=1,
                status="error",
            )
        else:
            variants[interpreter] = result

    return _combine_variant_responses(variants)
