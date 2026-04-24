import dataclasses
import types as _types

import cattrs
from cattrs.gen import make_dict_structure_fn, make_dict_unstructure_fn, override


def _to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def _camel_structure_fn(cls, conv):
    overrides = {
        f.name: override(rename=_to_camel(f.name))
        for f in dataclasses.fields(cls)
        if _to_camel(f.name) != f.name
    }
    return make_dict_structure_fn(cls, conv, **overrides)


def _camel_unstructure_fn(cls, conv):
    overrides = {
        f.name: override(rename=_to_camel(f.name))
        for f in dataclasses.fields(cls)
        if _to_camel(f.name) != f.name
    }
    return make_dict_unstructure_fn(cls, conv, **overrides)


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


_is_dc = lambda t: dataclasses.is_dataclass(t) and isinstance(t, type)

converter = cattrs.Converter()
converter.register_structure_hook_factory(
    lambda t: isinstance(t, _types.UnionType), _new_union_structure_fn
)
converter.register_structure_hook_factory(_is_dc, _camel_structure_fn)
converter.register_unstructure_hook_factory(_is_dc, _camel_unstructure_fn)
