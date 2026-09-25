"""Rendering the resolved config dump as TOML.

The dump target is a ``pyproject.toml``, so this is the one place in the
``dump_config`` pipeline that knows the serialization format; the format and
save handlers only pass the rendered text on.
"""

import tomlkit
from packaging.utils import canonicalize_name

from fine_envs.dependency_config_utils import get_dependency_name


def attribution_comment(
    active_selection: dict[str, list[str]], raw_config: dict
) -> str:
    """Return a TOML comment block naming each dependency spec the workspace
    selection rewrote, or an empty string when nothing was rewritten.

    The selection is derivable at dump time from ``finecode-workspace-user.toml``
    alone, so attribution needs no provenance threaded through config merging.
    """
    if not active_selection:
        return ""
    lines: list[str] = []
    for specs in raw_config.get("dependency-groups", {}).values():
        for spec in specs:
            if not isinstance(spec, str):
                continue
            extras = active_selection.get(canonicalize_name(get_dependency_name(spec)))
            if extras:
                lines.append(
                    f"# {spec} (selected by extra(s) {', '.join(extras)} "
                    f"in finecode-workspace-user.toml)"
                )
    if not lines:
        return ""
    return (
        "# Dependency specs rewritten by finecode-workspace-user.toml:\n"
        + "\n".join(lines)
        + "\n"
    )


def render_config_dump(
    raw_config_dump: dict, active_selection: dict[str, list[str]]
) -> str:
    """Render the dump as TOML, prefixed by the attribution comment when present."""
    content = tomlkit.dumps(raw_config_dump)
    attribution = attribution_comment(active_selection, raw_config_dump)
    if attribution:
        content = attribution + content
    return content
