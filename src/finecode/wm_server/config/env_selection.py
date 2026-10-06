"""Pure resolution of matrix-env selection, one resolver per command.

Given a
project's (post-expansion) ``tool.finecode.env`` table plus the relevant
selectors from WM cleint and the config-declared ``default_interpreters``
policy, decide which concrete envs are actually "selected" for this run
(PRD-0003 AC8, ADR-0103).

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
    "ALL_INTERPRETERS",
    "EnvSelection",
    "EnvSelectionError",
    "compute_prepare_set",
    "env_selector_known_in",
    "interpreter_selector_known_in",
    "resolve_env_selection",
    "resolve_run_selection",
]


ALL_INTERPRETERS = "all"


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
        target_key = (
            max(k for k, _ in keyed) if policy == "newest" else min(k for k, _ in keyed)
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


@dataclass
class _MatrixBases:
    all_env_names: set[str]
    matrix_children: dict[str, Interpreter]
    bases: dict[str, list[str]]
    axis_by_base: dict[str, set[Interpreter]]
    default_interpreters_by_base: dict[str, dict[str, Any]]
    non_matrix_names: set[str]


def _matrix_bases(env_table: dict[str, dict]) -> _MatrixBases:
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
    for base, policy_dict in default_interpreters_by_base.items():
        for key, policy in policy_dict.items():
            _resolve_policy(base, key, policy, axis_by_base[base])
    return _MatrixBases(
        all_env_names=all_env_names,
        matrix_children=matrix_children,
        bases=bases,
        axis_by_base=axis_by_base,
        default_interpreters_by_base=default_interpreters_by_base,
        non_matrix_names=non_matrix_names,
    )


def _children_with(mb: _MatrixBases, base: str, interps: set[Interpreter]) -> set[str]:
    return {child for child in mb.bases[base] if mb.matrix_children[child] in interps}


def _default_interpreters(
    mb: _MatrixBases, base: str, dev_env: str
) -> set[Interpreter]:
    policy = _lookup_policy(mb.default_interpreters_by_base.get(base, {}), dev_env)
    return _resolve_policy(base, dev_env, policy, mb.axis_by_base[base])


def resolve_env_selection(
    env_table: dict[str, dict],
    env_selectors: list[str],
    dev_env: str,
) -> EnvSelection:
    """Resolve which envs in `env_table` are selected."""
    mb = _matrix_bases(env_table)
    if not env_selectors:
        selected: set[str] = set(mb.non_matrix_names)
        for base in mb.bases:
            selected |= _children_with(
                mb, base, _default_interpreters(mb, base, dev_env)
            )
        active = selected != mb.all_env_names
        return EnvSelection(
            active=active,
            selected_env_names=selected,
            matrix_child_names=set(mb.matrix_children.keys()),
        )
    selected = set()
    for selector in env_selectors:
        if selector in mb.bases:
            selected |= _children_with(
                mb, selector, _default_interpreters(mb, selector, dev_env)
            )
        elif (
            selector.endswith("@" + ALL_INTERPRETERS)
            and selector[: -len("@" + ALL_INTERPRETERS)] in mb.bases
        ):
            base = selector[: -len("@" + ALL_INTERPRETERS)]
            selected |= set(mb.bases[base])
        elif selector in mb.matrix_children or selector in mb.non_matrix_names:
            selected.add(selector)
        # else: unknown-in-this-project -> selects nothing here; cross-project
        # validation happens at the service layer.
    active = selected != mb.all_env_names
    return EnvSelection(
        active=active,
        selected_env_names=selected,
        matrix_child_names=set(mb.matrix_children.keys()),
    )


def resolve_run_selection(
    env_table: dict[str, dict],
    interpreter_selectors: list[str],
    dev_env: str,
) -> set[str] | None:
    """Resolve `--interpreter` selectors (+ config default) into selected concrete
    matrix env names, for use by the run fan-out sites (ADR-0103).

    Returns ``None`` when every base's effective set equals its full axis
    (including when there are no bases) — callers then run the full declared
    axis, unchanged.
    """
    mb = _matrix_bases(env_table)
    parsed = {
        parse_interpreter(v) for v in interpreter_selectors if v != ALL_INTERPRETERS
    }
    effective_by_base: dict[str, set[Interpreter]] = {}
    if ALL_INTERPRETERS in interpreter_selectors:
        for base in mb.bases:
            effective_by_base[base] = set(mb.axis_by_base[base])
    elif interpreter_selectors:
        for base in mb.bases:
            effective_by_base[base] = mb.axis_by_base[base] & parsed
    else:
        for base in mb.bases:
            effective_by_base[base] = _default_interpreters(mb, base, dev_env)
    if all(effective_by_base[base] == mb.axis_by_base[base] for base in mb.bases):
        return None
    selected: set[str] = set()
    for base in mb.bases:
        selected |= _children_with(mb, base, effective_by_base[base])
    return selected


def compute_prepare_set(selection: EnvSelection, all_env_names: set[str]) -> set[str]:
    """Envs to run both `create_envs` and `install_envs` for: every env when
    the selection is inactive, otherwise exactly the selected envs. A
    non-matrix env outside an `--env` filter is neither created nor installed,
    the same as an unselected matrix child (PRD-0003 AC8)."""
    if not selection.active:
        return set(all_env_names)
    return set(selection.selected_env_names)


def env_selector_known_in(selector: str, env_table: dict[str, Any]) -> bool:
    """Whether `--env` value `selector` matches an env name, a matrix base
    name, or a `<base>@all` form in `env_table`."""
    if selector in env_table:
        return True
    if selector.endswith("@" + ALL_INTERPRETERS):
        prefix = selector[: -len("@" + ALL_INTERPRETERS)]
        for name, entry in env_table.items():
            if (
                isinstance(entry, dict)
                and "interpreter" in entry
                and name.split("@", 1)[0] == prefix
            ):
                return True
        return False
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
    by some matrix child in `env_table`, or is the `all` keyword."""
    if selector == ALL_INTERPRETERS:
        return True
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
