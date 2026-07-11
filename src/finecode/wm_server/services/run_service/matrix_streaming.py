"""WM-side variant-keyed fan-out for matrixed actions on the streaming path
(PRD-0003).

Mirrors ``matrix_runner`` (the non-streaming fan-out) one layer up: each
interpreter variant is a scoped call to
:func:`proxy_utils.run_with_partial_results` (passing ``interpreter=`` to
restrict dispatch to that variant's handlers — see that function's docstring)
rather than a fresh in-process ``run_variant`` invocation. There is no
separate "single env streaming run" primitive to reuse the way
``matrix_runner`` reuses ``proxy_utils._execute_action`` — the per-env
streaming primitive already IS ``run_with_partial_results``.

Within a variant, the ER's own ``actions/mergeResults`` still combines that
variant's handler partials into one typed result when the caller opts into
merging (typed merge stays ER-side — the WM only ever sees serialized dicts).
Across variants, this module reuses ``matrix_runner._combine_variant_responses``
to key the final result by interpreter, exactly like the non-streaming path.

Runs the FULL declared interpreter axis, unless ``selected_interpreters`` is
given (PRD-0003 AC8) — a set of interpreter canonicals
(``"<impl>@<version>"``) to restrict the fan-out to.
"""

from __future__ import annotations

import asyncio
import typing

from finecode.wm_server import context, domain
from finecode.wm_server.config import interpreter_matrix
from finecode.wm_server.config.interpreter_matrix import Interpreter
from finecode.wm_server.runner import runner_client
from finecode.wm_server.runner.runner_client import RunActionResponse

from . import matrix_runner, proxy_utils
from .exceptions import ActionRunFailed
from .merge_helpers import merge_partial_results_for_action

__all__ = ["run_matrix_with_partial_results"]


OnPartial = typing.Callable[[str, dict], typing.Awaitable[None]]


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


async def _run_variant(
    *,
    interpreter: Interpreter,
    project: domain.CollectedProject,
    action_name: str,
    params: dict[str, typing.Any],
    result_formats: list[runner_client.RunResultFormat] | None,
    partial_result_token: int | str,
    run_trigger: runner_client.RunActionTrigger,
    dev_env: runner_client.DevEnv,
    ws_context: context.WorkspaceContext,
    merge_results: bool,
    on_partial: OnPartial,
) -> RunActionResponse:
    """Run one interpreter variant end-to-end and return its serialized response.

    Forwards every partial received for this variant to *on_partial*, tagged
    with ``interpreter.canonical``, as it arrives — this is what gives the
    CLI live, per-interpreter-labeled output. The returned
    ``RunActionResponse`` is only this variant's contribution to the final
    variant-keyed result; combination across variants happens in the caller.
    """
    partial_count = 0
    json_payloads: list[dict] = []
    final_result_by_format: dict[str, typing.Any] = {}

    async with proxy_utils.run_with_partial_results(
        action_name=action_name,
        params=params,
        partial_result_token=partial_result_token,
        project_dir_path=project.dir_path,
        run_trigger=run_trigger,
        dev_env=dev_env,
        ws_context=ws_context,
        initialize_all_handlers=True,
        result_formats=result_formats,
        interpreter=interpreter,
    ) as ctx:
        async for value in ctx:
            partial_count += 1
            if isinstance(value, dict):
                final_result_by_format = value
                if merge_results:
                    json_payloads.append(value.get("json"))
            await on_partial(interpreter.canonical, value)

    return_code = 0
    for resp in ctx.responses:
        return_code |= resp.return_code

    # Direct result mode: the ER sent no $/progress notifications for this
    # variant, so the complete result sits in ctx.responses. Forward it as a
    # partial too, mirroring `_stream_action`'s direct-mode handling, so the
    # CLI still sees this variant's output.
    if partial_count == 0:
        for resp in ctx.responses:
            if resp.result_by_format:
                final_result_by_format = resp.result_by_format
                if merge_results:
                    json_payloads.append(resp.result_by_format.get("json"))
                await on_partial(interpreter.canonical, resp.result_by_format)

    if merge_results:
        merged_json = await merge_partial_results_for_action(
            project_path=project.dir_path,
            action_name=action_name,
            json_payloads=json_payloads,
            ws_context=ws_context,
        )
        variant_result_by_format: dict[str, typing.Any] = {"json": merged_json}
        # Merging only applies to the `json` format — preserve a text form when
        # the (single, or last-seen) streamed/direct result carried one.
        for fmt in ("string", "styled_text_json"):
            if fmt in final_result_by_format:
                variant_result_by_format[fmt] = final_result_by_format[fmt]
    else:
        variant_result_by_format = final_result_by_format

    return RunActionResponse(
        result_by_format=variant_result_by_format,
        return_code=return_code,
        status="success",
    )


async def _run_variant_safe(
    *,
    interpreter: Interpreter,
    **kwargs: typing.Any,
) -> RunActionResponse:
    """Run one interpreter variant, converting any exception into a synthetic
    failed response so one variant's failure never aborts the others (R5)."""
    try:
        return await _run_variant(interpreter=interpreter, **kwargs)
    except Exception as exc:
        return RunActionResponse(
            result_by_format={"string": f"error: {exc}", "json": {"error": str(exc)}},
            return_code=1,
            status="error",
        )


async def run_matrix_with_partial_results(
    *,
    project: domain.CollectedProject,
    action: domain.Action,
    action_name: str,
    params: dict[str, typing.Any],
    result_formats: list[runner_client.RunResultFormat] | None,
    partial_result_token: int | str,
    run_trigger: runner_client.RunActionTrigger,
    dev_env: runner_client.DevEnv,
    ws_context: context.WorkspaceContext,
    merge_results: bool,
    on_partial: OnPartial,
    selected_interpreters: set[str] | None = None,
) -> tuple[dict, int]:
    """Fan a matrixed action out per interpreter over the streaming path.

    Runs one variant per interpreter concurrently, each a scoped call to
    ``proxy_utils.run_with_partial_results``; a variant that raises never
    aborts the others. Returns the variant-keyed ``result_by_format`` (built
    by the existing ``matrix_runner._combine_variant_responses``) and the
    OR'd overall return code.

    Runs the FULL declared interpreter axis, unless *selected_interpreters* is
    given (PRD-0003 AC8) — a set of interpreter canonicals to restrict the
    fan-out to.

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

    interpreters: list[Interpreter] = list(selected.keys())

    tasks = [
        _run_variant_safe(
            interpreter=interpreter,
            project=project,
            action_name=action_name,
            params=params,
            result_formats=result_formats,
            partial_result_token=partial_result_token,
            run_trigger=run_trigger,
            dev_env=dev_env,
            ws_context=ws_context,
            merge_results=merge_results,
            on_partial=on_partial,
        )
        for interpreter in interpreters
    ]
    responses = await asyncio.gather(*tasks)

    variants: dict[Interpreter, RunActionResponse] = dict(zip(interpreters, responses))
    combined = matrix_runner._combine_variant_responses(variants)
    return combined.result_by_format, combined.return_code
