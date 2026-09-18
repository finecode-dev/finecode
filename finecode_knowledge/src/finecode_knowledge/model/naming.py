"""How a schema name is spelled, and where its qualifier comes from (ADR-0017 D3/D4).

Every name in the registry -- entity type, field, relationship kind, predicate,
rule, template, provider -- is ``<package>.<local name>``, where *package* is
the top-level Python package of the module that declared it. The author writes
only the local name; the qualifier is derived here, which is what makes it
unforgeable: ``my_ext`` cannot claim ``fine_knowledge.`` because it never types
the prefix.

There is deliberately **no unqualified lookup** (D4) -- no uniqueness-based
fallback, no resolution order, no ambiguity error, because there is no
unqualified form to be ambiguous. A name resolves or it does not.

A leaf module: it imports only ``errors``, so every layer can spell a name.
"""

from __future__ import annotations

from finecode_knowledge.model.errors import SchemaError

__all__ = [
    "declaring_package",
    "is_qualified",
    "local_in_package",
    "qualify",
    "split_qualified",
]


def declaring_package(obj: object) -> str:
    """The top-level package of *obj*'s defining module -- D3's derived qualifier.

    *obj* is a class or function: ``my_ext.schema.Rel`` attributes to ``my_ext``.

    Raises:
        SchemaError: *obj* has no ``__module__`` to attribute it to.
    """
    module = getattr(obj, "__module__", None)
    if not module:
        raise SchemaError(
            f"Cannot attribute {obj!r} to a declaring package: it has no __module__. "
            "Pass `package=` explicitly to register it."
        )
    return module.partition(".")[0]


def is_qualified(name: str) -> bool:
    return "." in name


def qualify(package: str, local_name: str) -> str:
    """Spell *local_name* as declared by *package*; already-qualified names pass through.

    Pass-through is what lets one package name another's entity: a field
    declared in ``my_ext`` on ``fine_knowledge.Package`` spells that entity
    qualified, and it is not re-prefixed.
    """
    if is_qualified(local_name):
        return local_name
    return f"{package}.{local_name}"


def local_in_package(qualified_name: str, package: str) -> str:
    """Spell *qualified_name* for a reader who already knows they are in *package*.

    ADR-0017 D7's grouping rule, and the whole of it: a name declared by the
    surrounding package drops the qualifier, because the section heading (or
    the entity being rendered) already states it; a name from anywhere else
    keeps it. This is *not* "abbreviate when unambiguous" -- the rule D7
    rejects -- because it never depends on what else happens to be registered.
    ``my_ext.score`` and ``sec_ext.score`` on one entity stay two visibly
    different rows under every possible set of installed extensions.
    """
    declaring, local = split_qualified(qualified_name)
    return local if declaring == package else qualified_name


def split_qualified(name: str) -> tuple[str, str]:
    """Split ``"fine_knowledge.Package"`` into ``("fine_knowledge", "Package")``.

    Raises:
        SchemaError: *name* carries no qualifier. There is no unqualified
            lookup to fall back to (D4), so this is the end of the road rather
            than the start of a resolution search.
    """
    package, sep, local = name.partition(".")
    if not sep:
        raise SchemaError(
            f"Unqualified schema name: {name!r}. Every name is spelled "
            f"'<package>.<local name>' -- there is no unqualified lookup (ADR-0017 D4). "
            f"If {name!r} is declared by this package, spell it '<your package>.{name}'."
        )
    return package, local
