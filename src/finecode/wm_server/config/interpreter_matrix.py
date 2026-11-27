"""Config-time resolution of interpreter matrices.

Pure value objects and functions for parsing interpreter identities,
expanding matrix environments and their handlers into concrete
per-interpreter envs, and validating matrix configurations. No I/O, no
config-file reading, no venv creation.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = [
    "MATRIX_ENV_KEY",
    "ConcreteEnv",
    "ConcreteEnvNameError",
    "EnvSpec",
    "ExpansionResult",
    "HandlerRef",
    "Interpreter",
    "InvalidInterpreterError",
    "MatrixSetMismatchError",
    "MixedMatrixError",
    "check_concrete_env_names",
    "concrete_env_name",
    "expand",
    "matrix_child_interpreter",
    "parse_interpreter",
    "validate",
]


MATRIX_ENV_KEY = "matrix_env"
"""Env-table key that expansion writes on each concrete child, naming the
matrix environment it came from."""


class InvalidInterpreterError(ValueError):
    """An interpreter string is malformed."""


class MixedMatrixError(ValueError):
    """An action mixes matrix and single-interpreter handlers."""


class MatrixSetMismatchError(ValueError):
    """A matrixed action's matrix environments do not all declare the exact
    same interpreter set."""


@dataclass(frozen=True)
class Interpreter:
    implementation: str
    version: str

    @property
    def canonical(self) -> str:
        return f"{self.implementation}@{self.version}"

    @property
    def env_suffix(self) -> str:
        return f"{self.implementation}-{self.version}"


@dataclass
class EnvSpec:
    name: str
    interpreters: list[Interpreter] | None = None


@dataclass
class HandlerRef:
    action: str
    name: str
    env: str


class ConcreteEnvNameError(ValueError):
    """A generated matrix child name is invalid or collides."""


@dataclass
class ConcreteEnv:
    name: str
    interpreter: Interpreter | None
    matrix_env: str | None


@dataclass
class ExpansionResult:
    concrete_envs: list[ConcreteEnv]
    handlers: list[HandlerRef]
    matrix_environments: dict[str, list[Interpreter]]


def matrix_child_interpreter(env_entry: object) -> str | None:
    """The `interpreter` of an env-table entry that expansion produced, or
    `None` for any other entry, including one with only an `interpreter` pin."""
    if isinstance(env_entry, dict) and MATRIX_ENV_KEY in env_entry:
        return env_entry.get("interpreter")
    return None


def parse_interpreter(value: str) -> Interpreter:
    """Parse an interpreter string into an `Interpreter` value object.

    Raises:
        InvalidInterpreterError: If `value` is empty, whitespace-only, or
            does not match the `"<impl>@<version>"` / `"<version>"` shapes.
    """
    stripped = value.strip()
    if not stripped:
        raise InvalidInterpreterError(f"empty interpreter string: {value!r}")

    parts = stripped.split("@")
    if len(parts) == 1:
        return Interpreter("cpython", parts[0])
    if len(parts) != 2:
        raise InvalidInterpreterError(f"malformed interpreter string: {value!r}")

    implementation, version = parts
    implementation = implementation.strip()
    version = version.strip()
    if not implementation or not version:
        raise InvalidInterpreterError(f"malformed interpreter string: {value!r}")

    return Interpreter(implementation.lower(), version)


def concrete_env_name(base: str, interpreter: Interpreter) -> str:
    """Concrete env name for one matrix child."""
    return f"{base}-{interpreter.env_suffix}"


_NAME_PATTERN = "^([A-Z0-9]|[A-Z0-9][A-Z0-9._-]*[A-Z0-9])$"
_NAME_RE = re.compile(_NAME_PATTERN, re.IGNORECASE)


def _normalize_concrete_name(value: str) -> str:
    lowered = value.lower()
    out: list[str] = []
    prev_dash = False
    for ch in lowered:
        if ch == "-" or ch == "_" or ch == ".":
            if not prev_dash:
                out.append("-")
            prev_dash = True
        else:
            out.append(ch)
            prev_dash = False
    return "".join(out)


def check_concrete_env_names(
    expansion: ExpansionResult, declared_names: Iterable[str]
) -> None:
    """Validate generated child names.

    Raises:
        ConcreteEnvNameError: If a generated name is invalid under PEP 735 /
            PEP 508, or collides after normalization with a declared name or
            another generated name. Reports the first failing child in
            `concrete_envs` order.
    """
    declared_list = list(declared_names)
    seen: dict[str, str] = {}
    for concrete_env in expansion.concrete_envs:
        if concrete_env.matrix_env is None:
            continue
        name = concrete_env.name
        base = concrete_env.matrix_env
        canonical = (
            concrete_env.interpreter.canonical
            if concrete_env.interpreter is not None
            else ""
        )
        if _NAME_RE.match(name) is None:
            raise ConcreteEnvNameError(
                f"matrix env {base!r} interpreter {canonical!r} "
                f"produces invalid dependency-group name {name!r}"
            )
        normalized = _normalize_concrete_name(name)
        for declared in declared_list:
            if _normalize_concrete_name(declared) == normalized:
                raise ConcreteEnvNameError(
                    f"matrix env {base!r} interpreter {canonical!r} "
                    f"produces name {name!r} conflicting with "
                    f"declared name {declared!r}"
                )
        if normalized in seen:
            raise ConcreteEnvNameError(
                f"matrix env {base!r} interpreter {canonical!r} "
                f"produces name {name!r} conflicting with generated "
                f"name {seen[normalized]!r}"
            )
        seen[normalized] = name


def expand(envs: list[EnvSpec], handlers: list[HandlerRef]) -> ExpansionResult:
    """Expand matrix environments into concrete per-interpreter envs and
    rewrite handler references accordingly."""
    concrete_envs: list[ConcreteEnv] = []
    matrix_environments: dict[str, list[Interpreter]] = {}
    env_names_by_base: dict[str, list[str]] = {}

    for env in envs:
        if env.interpreters is None:
            concrete_envs.append(ConcreteEnv(env.name, None, None))
            env_names_by_base[env.name] = [env.name]
        else:
            matrix_environments[env.name] = env.interpreters
            concrete_names: list[str] = []
            for interpreter in env.interpreters:
                concrete_name = concrete_env_name(env.name, interpreter)
                concrete_envs.append(ConcreteEnv(concrete_name, interpreter, env.name))
                concrete_names.append(concrete_name)
            env_names_by_base[env.name] = concrete_names

    rewritten_handlers: list[HandlerRef] = []
    for handler in handlers:
        for concrete_name in env_names_by_base[handler.env]:
            rewritten_handlers.append(
                HandlerRef(handler.action, handler.name, concrete_name)
            )

    return ExpansionResult(
        concrete_envs=concrete_envs,
        handlers=rewritten_handlers,
        matrix_environments=matrix_environments,
    )


def validate(envs: list[EnvSpec], handlers: list[HandlerRef]) -> dict[str, str]:
    """Classify each action referenced by `handlers` as `"single"` or
    `"matrixed"`.

    Raises:
        MixedMatrixError: If an action mixes handlers referencing matrix
            environments with handlers referencing single-interpreter envs.
        MatrixSetMismatchError: If a matrixed action's matrix environments do
            not all declare the exact same interpreter set.
    """
    interpreters_by_matrix_env: dict[str, list[Interpreter]] = {
        env.name: env.interpreters for env in envs if env.interpreters is not None
    }
    single_env_names = {env.name for env in envs if env.interpreters is None}

    action_matrix_envs: dict[str, list[str]] = {}
    action_single_envs: dict[str, list[str]] = {}
    action_order: list[str] = []

    for handler in handlers:
        if handler.action not in action_order:
            action_order.append(handler.action)
        if handler.env in interpreters_by_matrix_env:
            action_matrix_envs.setdefault(handler.action, []).append(handler.env)
        elif handler.env in single_env_names:
            action_single_envs.setdefault(handler.action, []).append(handler.env)

    result: dict[str, str] = {}
    for action in action_order:
        matrix_envs = action_matrix_envs.get(action, [])
        single_envs = action_single_envs.get(action, [])

        if matrix_envs and single_envs:
            raise MixedMatrixError(
                f"action {action!r} mixes matrix and single-interpreter envs"
            )

        if matrix_envs:
            interpreter_sets = [
                set(interpreters_by_matrix_env[env]) for env in matrix_envs
            ]
            first_set = interpreter_sets[0]
            for other_set in interpreter_sets[1:]:
                if other_set != first_set:
                    raise MatrixSetMismatchError(
                        f"action {action!r} spans matrix environments with "
                        "mismatched interpreter sets"
                    )
            result[action] = "matrixed"
        else:
            result[action] = "single"

    return result
