"""Pure resolution of matrix-env interpreter subset selection for prepare-envs.

Given a project's (post-expansion) ``tool.finecode.env`` table plus the
``--env`` / ``--interpreter`` CLI selectors and the config-declared
``default_interpreters`` policy, decide which concrete envs are actually
"selected" for this run (PRD-0003 AC8).

No I/O, no config-file reading, no venv creation — this module only
consumes an already-loaded env table dict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from finecode.wm_server.config.interpreter_matrix import (
    Interpreter,
    InvalidInterpreterError,
    parse_interpreter,
)

__all__ = [
    "EnvSelection",
    "EnvSelectionError",
    "resolve_env_selection",
    "resolve_selected_interpreters",
    "compute_create_set",
    "compute_install_set",
    "env_selector_known_in",
    "interpreter_selector_known_in",
]


class EnvSelectionError(ValueError):
    """A ``default_interpreters`` policy or explicit selector is invalid."""


@dataclass
class EnvSelection:
    active: bool
    """True iff `selected_env_names` is a proper subset of all env names."""
    selected_env_names: set[str]
    matrix_child_names: set[str]
    """Every matrix child env in this project, selected or not."""


def _version_sort_key(version: str) -> tuple:
    """Order versions by numeric parts where possible, falling back to string
    comparison for non-numeric parts (e.g. pre-release suffixes)."""
    key: list[tuple[int, Any]] = []
    for part in version.split("."):
        if part.isdigit():
            key.append((0, int(part)))
        else:
            key.append((1, part))
    return tuple(key)


def _resolve_policy(
    base: str, key: str, policy: Any, axis: set[Interpreter]
) -> set[Interpreter]:
    if policy == "all":
        return set(axis)
    if policy == "newest" or policy == "oldest":
        keyed = [(_version_sort_key(interp.version), interp) for interp in axis]
        target_key = max(k for k, _ in keyed) if policy == "newest" else min(
            k for k, _ in keyed
        )
        return {interp for k, interp in keyed if k == target_key}
    if isinstance(policy, list):
        result: set[Interpreter] = set()
        for value in policy:
            try:
                interp = parse_interpreter(value)
            except InvalidInterpreterError as exc:
                raise EnvSelectionError(
                    f"default_interpreters for env '{base}' (key '{key}') has an"
                    f" invalid interpreter string {value!r}: {exc}"
                ) from exc
            if interp not in axis:
                raise EnvSelectionError(
                    f"default_interpreters for env '{base}' (key '{key}') names"
                    f" interpreter '{value}' which is not in its declared"
                    " interpreter axis"
                )
            result.add(interp)
        return result
    raise EnvSelectionError(
        f"default_interpreters for env '{base}' (key '{key}') has an invalid"
        f" policy value: {policy!r}"
    )


def _lookup_policy(policy_dict: dict[str, Any], dev_env: str) -> Any:
    if dev_env in policy_dict:
        return policy_dict[dev_env]
    bucket = "ci" if dev_env == "ci" else "local"
    if bucket in policy_dict:
        return policy_dict[bucket]
    return "all"


def resolve_env_selection(
    env_table: dict[str, dict],
    env_selectors: list[str],
    interpreter_selectors: list[str],
    dev_env: str,
) -> EnvSelection:
    """Resolve which envs in `env_table` are selected for this prepare-envs run.

    Raises:
        EnvSelectionError: A config default or explicit selector names an
            interpreter not in its base's declared axis.
        InvalidInterpreterError: An `--interpreter` selector string is malformed.
    """
    all_env_names = set(env_table.keys())

    matrix_children: dict[str, Interpreter] = {}
    bases: dict[str, list[str]] = {}
    for name, entry in env_table.items():
        if isinstance(entry, dict) and "interpreter" in entry:
            interp = parse_interpreter(entry["interpreter"])
            matrix_children[name] = interp
            base = name.split("@", 1)[0]
            bases.setdefault(base, []).append(name)

    non_matrix_names = all_env_names - set(matrix_children.keys())

    axis_by_base: dict[str, set[Interpreter]] = {
        base: {matrix_children[child] for child in children}
        for base, children in bases.items()
    }

    default_interpreters_by_base: dict[str, dict[str, Any]] = {}
    for base, children in bases.items():
        first_child = env_table[children[0]]
        default_interpreters_by_base[base] = first_child.get("default_interpreters", {})

    # Eagerly validate every declared policy (not just the one active for the
    # current dev_env) so a config bug surfaces regardless of which run
    # triggers it.
    for base, policy_dict in default_interpreters_by_base.items():
        for key, policy in policy_dict.items():
            _resolve_policy(base, key, policy, axis_by_base[base])

    parsed_interpreter_selectors: set[Interpreter] | None = None
    if interpreter_selectors:
        parsed_interpreter_selectors = {
            parse_interpreter(value) for value in interpreter_selectors
        }

    interp_effective_set_by_base: dict[str, set[Interpreter]] = {}
    for base in bases:
        if parsed_interpreter_selectors is not None:
            interp_effective_set_by_base[base] = (
                axis_by_base[base] & parsed_interpreter_selectors
            )
        else:
            policy = _lookup_policy(default_interpreters_by_base.get(base, {}), dev_env)
            interp_effective_set_by_base[base] = _resolve_policy(
                base, dev_env, policy, axis_by_base[base]
            )

    named_bases: set[str] = set()
    named_children_by_base: dict[str, set[str]] = {}
    named_non_matrix: set[str] = set()
    for selector in env_selectors:
        if selector in bases:
            named_bases.add(selector)
        elif selector in matrix_children:
            base = selector.split("@", 1)[0]
            named_children_by_base.setdefault(base, set()).add(selector)
        elif selector in non_matrix_names:
            named_non_matrix.add(selector)
        # else: unknown-in-this-project -> selects nothing here; cross-project
        # validation happens at the service layer.

    selected: set[str] = set()
    for base, children in bases.items():
        if base in named_bases:
            if parsed_interpreter_selectors is not None:
                chosen = {
                    child
                    for child in children
                    if matrix_children[child] in interp_effective_set_by_base[base]
                }
            else:
                chosen = set(children)
        elif base in named_children_by_base:
            named = named_children_by_base[base]
            if parsed_interpreter_selectors is not None:
                chosen = {
                    child
                    for child in named
                    if matrix_children[child] in interp_effective_set_by_base[base]
                }
            else:
                chosen = set(named)
        else:
            chosen = {
                child
                for child in children
                if matrix_children[child] in interp_effective_set_by_base[base]
            }
        selected |= chosen

    if env_selectors:
        selected |= named_non_matrix
    else:
        selected |= non_matrix_names

    active = selected != all_env_names

    return EnvSelection(
        active=active,
        selected_env_names=selected,
        matrix_child_names=set(matrix_children.keys()),
    )


def resolve_selected_interpreters(
    env_table: dict[str, dict],
    env_selectors: list[str],
    interpreter_selectors: list[str],
    dev_env: str,
) -> set[str] | None:
    """Resolve `--env`/`--interpreter` selectors (+ config default) into a set
    of selected interpreter canonicals (``"<impl>@<version>"``), for use by
    the run fan-out sites (PRD-0003 AC8).

    Returns ``None`` when nothing narrows the axis (mirrors
    ``EnvSelection.active`` being ``False``) — callers then run the full
    declared axis, unchanged.

    Raises:
        EnvSelectionError: A config default or explicit selector names an
            interpreter not in its base's declared axis.
        InvalidInterpreterError: An `--interpreter` selector string is malformed.
    """
    selection = resolve_env_selection(env_table, env_selectors, interpreter_selectors, dev_env)
    if not selection.active:
        return None
    return {
        env_table[name]["interpreter"]
        for name in selection.selected_env_names
        if name in selection.matrix_child_names
    }


def compute_create_set(selection: EnvSelection, all_env_names: set[str]) -> set[str]:
    """Envs to run `create_envs` for: non-matrix envs are always created;
    unselected matrix children are skipped (PRD-0003 AC8)."""
    if not selection.active:
        return set(all_env_names)
    return all_env_names - (selection.matrix_child_names - selection.selected_env_names)


def compute_install_set(selection: EnvSelection, all_env_names: set[str]) -> set[str]:
    """Envs to run `install_envs` for."""
    if not selection.active:
        return set(all_env_names)
    return set(selection.selected_env_names)


def env_selector_known_in(selector: str, env_table: dict[str, Any]) -> bool:
    """Whether `--env` value `selector` matches an env name or matrix base
    name in `env_table` (a project's merged env-name -> config-entry map)."""
    if selector in env_table:
        return True
    for name, entry in env_table.items():
        if (
            isinstance(entry, dict)
            and "interpreter" in entry
            and name.split("@", 1)[0] == selector
        ):
            return True
    return False


def interpreter_selector_known_in(selector: str, env_table: dict[str, Any]) -> bool:
    """Whether `--interpreter` value `selector` matches an interpreter declared
    by some matrix child in `env_table`."""
    try:
        parsed = parse_interpreter(selector)
    except InvalidInterpreterError:
        return False

    for entry in env_table.values():
        if isinstance(entry, dict) and "interpreter" in entry:
            try:
                child_interp = parse_interpreter(entry["interpreter"])
            except InvalidInterpreterError:
                continue
            if child_interp == parsed:
                return True
    return False
