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


def _structure_strict_bool(val, _):
    if not isinstance(val, bool):
        raise ValueError(f"expected a boolean, got {type(val).__name__}")
    return val


def _structure_strict_int(val, _):
    # `bool` is an `int` subclass; JSON `true`/`false` must not satisfy an int
    # field and an integer must not satisfy a bool field.
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError(f"expected an integer, got {type(val).__name__}")
    return val


def _structure_strict_float(val, _):
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"expected a number, got {type(val).__name__}")
    return float(val)


def _structure_strict_str(val, _):
    if not isinstance(val, str):
        raise ValueError(f"expected a string, got {type(val).__name__}")
    return val


def _is_list_type(t):
    return t is list or typing.get_origin(t) is list


def _structure_strict_list_factory(cls, conv):
    item_type = cls.__args__[0] if cls.__args__ else typing.Any

    def structure(val, _):
        # Default cattrs iterates a string into its characters; a scalar meant
        # for a list field must fail instead of becoming ['f', 'i', 'l', 'e'].
        if isinstance(val, (str, bytes)) or not isinstance(
            val, collections.abc.Sequence
        ):
            raise ValueError(f"expected a list, got {type(val).__name__}")
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
