import dataclasses

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


_is_dc = lambda t: dataclasses.is_dataclass(t) and isinstance(t, type)

converter = cattrs.Converter()
converter.register_structure_hook_factory(_is_dc, _camel_structure_fn)
converter.register_unstructure_hook_factory(_is_dc, _camel_unstructure_fn)
