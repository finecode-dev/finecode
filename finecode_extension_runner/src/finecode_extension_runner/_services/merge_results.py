import dataclasses

from loguru import logger

from finecode_extension_api import code_action
from finecode_extension_runner import context, run_utils
from finecode_extension_runner._converter import converter as _converter


async def merge_results(
    action_name: str,
    results: list[dict],
    runner_context: context.RunnerContext,
) -> dict:
    """Merge multiple serialized action results into one using the action's result type.

    Each entry in ``results`` must be a dict produced by ``dataclasses.asdict()``
    of the action's ``RESULT_TYPE``.  Merging is delegated to
    ``RunActionResult.update()``, the same mechanism the runner uses when
    combining results from multiple handlers within a single run.
    """
    # Prefer cached result_type to avoid re-importing the action module.
    action_cache = runner_context.action_cache_by_name.get(action_name)
    if action_cache is not None and action_cache.exec_info is not None:
        result_type = action_cache.exec_info.result_type
    else:
        # Cold cache: action hasn't been run yet in this runner; import the type.
        try:
            action = runner_context.project.actions[action_name]
        except KeyError:
            raise ValueError(f"Action '{action_name}' not found")
        action_type = run_utils.import_module_member_by_source_str(action.source)
        result_type = action_type.RESULT_TYPE

    non_empty = [r for r in results if r]
    if result_type is None or not non_empty:
        return {}

    merged: code_action.RunActionResult | None = None
    for result_dict in non_empty:
        typed = _converter.structure(result_dict, result_type)
        if merged is None:
            merged = typed
        else:
            merged.update(typed)

    if merged is None:
        return {}

    logger.trace(f"merge_results: merged {len(non_empty)} results for action '{action_name}'")
    return dataclasses.asdict(merged)
