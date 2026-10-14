import collections.abc
import types as _types
import typing

import cattrs
from cattrs.gen import make_dict_structure_fn, override

try:
    from typing import Literal
except ImportError:
    from typing import Literal

from finecode_extension_api.code_action import RunActionMeta

from finecode_extension_runner.schemas import RunActionOptions


def _result_format_union_structure(val, _):
    # Accept only 'json' or 'string' as valid values
    if val in ("json", "string"):
        return val
    raise ValueError(f"Invalid result format: {val}")


def _new_union_structure_fn(cls, conv):
    """Handle Python 3.10+ ``X | Y`` union syntax (types.UnionType).

    cattrs' default Converter only handles typing.Union, not types.UnionType.
    """
    args = cls.__args__
    none_type = type(None)

    def structure(val, _):
        if val is None and none_type in args:
            return None
        for arg in args:
            if arg is none_type:
                continue
            try:
                return conv.structure(val, arg)
            except Exception:
                continue
        return val

    return structure


converter = cattrs.Converter()

converter.register_structure_hook_factory(
    lambda t: isinstance(t, _types.UnionType), _new_union_structure_fn
)

_result_format_union = Literal["json", "string"]
# Registered by predicate rather than by type: `register_structure_hook` routes
# types through `functools.singledispatch.register`, which rejects anything that
# is not a class or a union — `Literal` included — since Python 3.14.
converter.register_structure_hook_func(
    lambda t: t is _result_format_union, _result_format_union_structure
)

# Camel-case structuring for protocol options (wire uses camelCase, Python fields use snake_case)
converter.register_structure_hook(
    RunActionMeta,
    make_dict_structure_fn(
        RunActionMeta,
        converter,
        dev_env=override(rename="devEnv"),
        orchestration_depth=override(rename="orchestrationDepth"),
    ),
)

converter.register_structure_hook(
    RunActionOptions,
    make_dict_structure_fn(
        RunActionOptions,
        converter,
        run_id=override(rename="runId"),
        partial_result_token=override(rename="partialResultToken"),
        progress_token=override(rename="progressToken"),
        result_formats=override(rename="resultFormats"),
        caller_kwargs=override(rename="callerKwargs"),
    ),
)


# These hooks reject a value for being the wrong *type*, so they raise
# `TypeError` (ruff `TRY004`). cattrs wraps whatever a structure hook raises
# into a `BaseValidationError`, which is what `_structure_payload` catches, so
# the exception class chosen here does not change what a caller sees.
#
# cattrs resolves these hooks through `str`/`int`/`bool`/`float`'s registration
# in its *singledispatch* table, which matches by issubclass — so a payload
# field typed as a `StrEnum` or `IntEnum` (e.g. `InspectCodeTarget`,
# `DiagnosticSeverity`) is routed here too, not to cattrs' default enum
# factory. `cl` is that concrete field type, not necessarily plain
# `str`/`int`/`bool`/`float` — it must be used to build the result (`cl(val)`,
# which is a no-op for the plain types and reconstructs the enum member
# otherwise), or every enum-typed payload field silently degrades to its raw
# primitive instead of a real enum instance.
def _structure_strict_bool(val, cl):
    if not isinstance(val, bool):
        raise TypeError(f"expected a boolean, got {type(val).__name__}")
    return cl(val)


def _structure_strict_int(val, cl):
    # `bool` is an `int` subclass; JSON `true`/`false` must not satisfy an int
    # field and an integer must not satisfy a bool field.
    if isinstance(val, bool) or not isinstance(val, int):
        raise TypeError(f"expected an integer, got {type(val).__name__}")
    return cl(val)


def _structure_strict_float(val, cl):
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise TypeError(f"expected a number, got {type(val).__name__}")
    return cl(float(val))


def _structure_strict_str(val, cl):
    if not isinstance(val, str):
        raise TypeError(f"expected a string, got {type(val).__name__}")
    return cl(val)


def _is_list_type(t):
    return t is list or typing.get_origin(t) is list


def _structure_strict_list_factory(cls, conv):
    # `typing.get_args`, not `cls.__args__`: a bare `list` annotation has no
    # `__args__` at all, so reading it directly raises AttributeError before the
    # fallback below can be reached. `get_args` returns `()` there instead.
    args = typing.get_args(cls)
    item_type = args[0] if args else typing.Any

    def structure(val, _):
        # Default cattrs iterates a string into its characters; a scalar meant
        # for a list field must fail instead of becoming ['f', 'i', 'l', 'e'].
        if isinstance(val, (str, bytes)) or not isinstance(
            val, collections.abc.Sequence
        ):
            raise TypeError(f"expected a list, got {type(val).__name__}")
        return [conv.structure(item, item_type) for item in val]

    return structure


def _new_strict_union_structure_fn(cls, conv):
    """Structure ``X | Y`` strictly, raising when no member matches.

    Mirrors :func:`_new_union_structure_fn` for the one case that matters here:
    the shared converter passes an unmatched value through unchanged, which a
    payload converter must not do — it would silently accept ``"abc"`` for
    ``int | None``.
    """
    args = cls.__args__
    none_type = type(None)

    def structure(val, _):
        if val is None and none_type in args:
            return None
        for arg in args:
            if arg is none_type:
                continue
            try:
                return conv.structure(val, arg)
            except Exception:
                continue
        raise ValueError(f"invalid value for type, expected {cls}")

    return structure


# A second converter used only for action payloads. `converter` also governs
# results, run state, handler config and service config — none of which asked
# for strict typing — so it is left alone.
#
# `forbid_extra_keys` is deliberately absent here: cross-env subaction dispatch
# ships the parent payload's dict to a subaction whose concrete PAYLOAD_TYPE is
# not importable in this env (ActionRef.action_type is None — see
# iprojectactionrunner.ActionRef), and the receiving env structures it into its
# own, possibly wider, payload type. Tolerating extra keys is the mechanism that
# dispatch relies on, not an edge case.
payload_converter = converter.copy()
payload_converter.register_structure_hook(bool, _structure_strict_bool)
payload_converter.register_structure_hook(int, _structure_strict_int)
payload_converter.register_structure_hook(float, _structure_strict_float)
payload_converter.register_structure_hook(str, _structure_strict_str)
payload_converter.register_structure_hook_factory(
    _is_list_type, _structure_strict_list_factory
)
payload_converter.register_structure_hook_factory(
    lambda t: isinstance(t, _types.UnionType), _new_strict_union_structure_fn
)
