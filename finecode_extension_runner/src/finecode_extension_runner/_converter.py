import types as _types

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
