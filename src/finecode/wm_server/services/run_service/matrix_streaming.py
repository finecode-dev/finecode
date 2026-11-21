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

Runs the FULL declared interpreter axis, unless ``selected_envs`` is
given (ADR-0103) — a set of selected concrete env names
to restrict the fan-out to.
"""

from __future__ import annotations

import asyncio
import typing

from loguru import logger

from finecode.wm_server import context, domain
from finecode.wm_server.config.interpreter_matrix import Interpreter
from finecode.wm_server.runner import (
    _internal_client_types,
    elicitation_bridge,
    runner_client,
)
from finecode.wm_server.runner.runner_client import RunActionResponse
from finecode.wm_server.utils import stream_completion

from . import matrix_runner, proxy_utils
from .merge_helpers import merge_slot_results, strip_unrequested_formats

__all__ = ["run_matrix_with_partial_results"]


OnPartial = typing.Callable[[str, dict], typing.Awaitable[None]]
OnPrefix = typing.Callable[[stream_completion.PrefixFence], typing.Awaitable[None]]


def _pipeline_fault_of(
    error: BaseException,
) -> stream_completion.PipelineFailure | None:
    """Find a pipeline fault hidden in an outcome chain, if any."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, stream_completion.PipelineFailure):
            return current
        current = current.__cause__ or current.__context__
    return None


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
    on_prefix: OnPrefix,
    origin: elicitation_bridge.RunDispatchOrigin | None,
) -> RunActionResponse:
    """Run one interpreter variant end-to-end and return its serialized response.

    Forwards every partial received for this variant to *on_partial*, tagged
    with ``interpreter.canonical``, as it arrives — this is what gives the
    CLI live, per-interpreter-labeled output — and every prefix fence to
    *on_prefix*. The fence is owned by the caller from that point: a
    service caller forwards it into the output stream, an API caller
    acknowledges it after the writer handoff. The returned
    ``RunActionResponse`` is only this variant's contribution to the final
    variant-keyed result; combination across variants happens in the caller.
    """
    partial_count = 0
    entries: list[_internal_client_types.ResultByFormat] = []
    requested_formats = (
        result_formats
        if result_formats is not None
        else [runner_client.RunResultFormat.JSON]
    )
    result_formats_for_run = list(requested_formats)
    if (
        merge_results
        and runner_client.RunResultFormat.STRING in requested_formats
        and runner_client.RunResultFormat.JSON not in requested_formats
    ):
        result_formats_for_run.append(runner_client.RunResultFormat.JSON)
    final_result_by_format: _internal_client_types.ResultByFormat = {}
    callback_fault: BaseException | None = None
    runner_error: BaseException | None = None
    # Bound only when the producer context delivered its prefix boundary;
    # every error path above raises before the response below is read.
    finished: proxy_utils.RunWithPartialResultsContext | None = None
    try:
        async with proxy_utils.run_with_partial_results(
            action_name=action_name,
            params=params,
            partial_result_token=partial_result_token,
            project_dir_path=project.dir_path,
            run_trigger=run_trigger,
            dev_env=dev_env,
            ws_context=ws_context,
            initialize_all_handlers=True,
            result_formats=result_formats_for_run,
            interpreter=interpreter,
            origin=origin,
        ) as ctx:
            async for event in ctx.partials.events():
                if isinstance(event, stream_completion.PrefixFence):
                    if callback_fault is None:
                        try:
                            await on_prefix(event)
                        except Exception as exc:
                            callback_fault = exc
                    if callback_fault is not None and not event.completion.done():
                        # Settle the fence with the pipeline fault so the
                        # runner drain cannot strand on an ack that never
                        # comes; keep draining so later fences settle too.
                        event.completion.set_result(
                            stream_completion.PipelineFailure(callback_fault)
                        )
                    continue
                if callback_fault is not None:
                    # Owned forwarding is aborted: drain without delivering.
                    continue
                value = event.value
                partial_count += 1
                if isinstance(value, dict):
                    result_by_format = typing.cast(
                        _internal_client_types.ResultByFormat, value
                    )
                    final_result_by_format = result_by_format
                    if merge_results:
                        entries.append(result_by_format)
                try:
                    await on_partial(
                        interpreter.canonical,
                        dict(
                            strip_unrequested_formats(
                                typing.cast(
                                    _internal_client_types.ResultByFormat, value
                                ),
                                requested_formats,
                            )
                        ),
                    )
                except Exception as exc:
                    callback_fault = exc
            # The loop only ends at the shared END marker; record the
            # delivered context for the response below.
            finished = ctx
    except Exception as exc:
        runner_error = exc
    if callback_fault is not None and runner_error is not None:
        # Dual fault: the prefix handoff failed and the runner errored. The
        # runner-only outcome passes through the adapter below; the stream
        # fault is recorded here and the variant's forwarding already
        # stopped above.
        logger.warning(
            f"matrix variant '{interpreter.canonical}': prefix handoff failed"
            f" ({callback_fault}); preserving runner outcome ({runner_error})"
        )
        pipeline_cause = _pipeline_fault_of(runner_error)
        if pipeline_cause is not None:
            raise pipeline_cause
        raise runner_error
    if callback_fault is not None:
        raise stream_completion.PipelineFailure(callback_fault)
    if runner_error is not None:
        pipeline_cause = _pipeline_fault_of(runner_error)
        if pipeline_cause is not None:
            # A stream-only fault hidden in the runner outcome must not be
            # synthesized into an ordinary failed variant below.
            raise pipeline_cause
        raise runner_error

    assert finished is not None
    return_code = 0
    for resp in finished.responses:
        return_code |= resp.return_code

    # Direct result mode: the ER sent no $/progress notifications for this
    # variant, so the complete result sits in ctx.responses. Forward it as a
    # partial too, mirroring `_stream_action`'s direct-mode handling, so the
    # CLI still sees this variant's output.
    if partial_count == 0:
        for resp in finished.responses:
            if resp.result_by_format:
                result_by_format = typing.cast(
                    _internal_client_types.ResultByFormat, resp.result_by_format
                )
                final_result_by_format.update(result_by_format)
                await on_partial(
                    interpreter.canonical,
                    dict(
                        strip_unrequested_formats(result_by_format, requested_formats)
                    ),
                )
        if merge_results and final_result_by_format:
            entries.append(final_result_by_format)

    if merge_results:
        merged_result = await merge_slot_results(
            project_path=project.dir_path,
            action_name=action_name,
            entries=entries,
            requested_formats=requested_formats,
            ws_context=ws_context,
        )
        variant_result_by_format = merged_result or {}
    else:
        variant_result_by_format = strip_unrequested_formats(
            final_result_by_format, requested_formats
        )

    return RunActionResponse(
        result_by_format=typing.cast(
            dict[str, runner_client.RunActionRawResult],
            dict(variant_result_by_format),
        ),
        return_code=return_code,
        status="success",
    )


async def _run_variant_safe(
    *,
    interpreter: Interpreter,
    on_partial: OnPartial,
    on_prefix: OnPrefix,
    result_formats: list[runner_client.RunResultFormat] | None,
    **kwargs: typing.Any,
) -> RunActionResponse:
    """Run one interpreter variant, converting any exception into a synthetic
    failed response so one variant's failure never aborts the others (R5)."""
    try:
        return await _run_variant(
            interpreter=interpreter,
            on_partial=on_partial,
            on_prefix=on_prefix,
            result_formats=result_formats,
            **kwargs,
        )
    except stream_completion.PipelineFailure:
        # A stream-only fault is never a synthetic completed variant; the
        # coordinator aborts owned forwarding and joins every child.
        raise
    except Exception as exc:
        # A variant that fails before streaming anything would otherwise leave
        # only the synthetic response below, which a merged caller never prints.
        # Forward the failure as a partial for string-format callers so it
        # reaches their output; JSON-only callers get no new partial shape.
        if (
            result_formats is not None
            and runner_client.RunResultFormat.STRING in result_formats
        ):
            try:
                await on_partial(interpreter.canonical, {"string": f"error: {exc}"})
            except Exception:  # noqa: BLE001 - a failed notifier must not replace the variant's own error
                logger.warning(
                    f"Could not forward failure of interpreter "
                    f"'{interpreter.canonical}' to the partial-result consumer"
                )
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
    on_prefix: OnPrefix,
    selected_envs: set[str] | None = None,
    origin: elicitation_bridge.RunDispatchOrigin | None,
) -> tuple[dict, int]:
    """Fan a matrixed action out per interpreter over the streaming path.

    Runs one variant per interpreter concurrently, each a scoped call to
    ``proxy_utils.run_with_partial_results``; a variant that raises never
    aborts the others. Returns the variant-keyed ``result_by_format`` (built
    by the existing ``matrix_runner._combine_variant_responses``) and the
    OR'd overall return code.

    Runs the FULL declared interpreter axis, unless *selected_envs* is
    given (ADR-0103) — a set of selected concrete env names to restrict the
    fan-out to.
    """
    requested_formats = (
        result_formats
        if result_formats is not None
        else [runner_client.RunResultFormat.JSON]
    )
    selected = matrix_runner.selected_variants(action, selected_envs)

    interpreters: list[Interpreter] = list(selected.keys())

    tasks = [
        asyncio.ensure_future(
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
                on_prefix=on_prefix,
                origin=origin,
            )
        )
        for interpreter in interpreters
    ]
    try:
        responses = await asyncio.gather(*tasks)
    except BaseException:
        # A pipeline fault (or owner abort) stops owned forwarding: cancel
        # every sibling and join them all before propagating. Ordinary
        # variant failures never reach here — they are synthetic responses.
        for task in tasks:
            if not task.done():
                _ = task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    variants: dict[Interpreter, RunActionResponse] = dict(
        zip(interpreters, responses, strict=False)
    )
    combined = matrix_runner._combine_variant_responses(variants)
    combined_result = typing.cast(
        _internal_client_types.ResultByFormat, combined.result_by_format
    )
    if (
        merge_results
        and runner_client.RunResultFormat.JSON not in requested_formats
        and not any(response.status == "error" for response in responses)
    ):
        combined_result = strip_unrequested_formats(combined_result, requested_formats)
    return dict(combined_result), combined.return_code
