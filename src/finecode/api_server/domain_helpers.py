"""
Domain helper functions that operate on domain models but don't belong
directly in the domain module.
"""

from finecode.api_server import domain


def collect_all_handlers_to_initialize(
    project: domain.Project,
    env_name: str,
) -> dict[str, list[str]]:
    """Collect all handler names per action for the given env."""
    assert project.actions is not None
    result: dict[str, list[str]] = {}
    for action in project.actions:
        handler_names = [h.name for h in action.handlers if h.env == env_name]
        if handler_names:
            result[action.name] = handler_names
    return result


def collect_handlers_to_initialize_for_actions(
    project: domain.Project,
    env_name: str,
    action_names: list[str],
) -> dict[str, list[str]]:
    """Collect handler names per action for the given env, filtered by action names."""
    assert project.actions is not None
    result: dict[str, list[str]] = {}
    action_names_set = set(action_names)
    for action in project.actions:
        if action.name not in action_names_set:
            continue
        handler_names = [h.name for h in action.handlers if h.env == env_name]
        if handler_names:
            result[action.name] = handler_names
    return result
