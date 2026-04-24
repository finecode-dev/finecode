import types as _types

import cattrs

import typing
try:
    from typing import Literal, Union, get_args
except ImportError:
    from typing_extensions import Literal, get_args

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

_result_format_union = typing.Union[Literal["json"], Literal["string"]]
converter.register_structure_hook(_result_format_union, _result_format_union_structure)