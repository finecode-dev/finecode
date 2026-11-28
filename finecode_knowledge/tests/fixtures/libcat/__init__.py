"""``libcat`` -- a synthetic schema the engine's own test suite is written against.

Deliberately *unlike* FineCode's: a lending library, with no ``Project``, no
``Preset`` and no ``Package`` anywhere in it. That is the whole point. The
engine's claim is that it is schema-agnostic (R20) and that a third party
participates on identical terms (R18/R19), and a suite exercised only through
``fine_knowledge.schema`` cannot tell the difference between "the engine is
generic" and "the engine happens to know core's vocabulary". A leaked assumption
about ``Project`` fails loudly here instead of coincidentally passing.

Importing this package declares the schema and nominates it as the process
default, exactly as a real schema package does -- see ``libcat/schema.py``.
"""

from __future__ import annotations

# Importing the *package* is what fully populates the registry -- `schema.py`
# cannot import `predicates.py` back (see that module's docstring), so a consumer
# reaching for `LIBCAT_SCHEMA` through `libcat.schema` alone would find the
# predicate namespace half-empty.
from libcat import predicates as predicates
from libcat.schema import (
    LIBCAT_SCHEMA,
    Author,
    AuthorFields,
    Book,
    BookFields,
    Copy,
    CopyFields,
    Rel,
    Shelf,
    ShelfFields,
)

__all__ = [
    "LIBCAT_SCHEMA",
    "Author",
    "AuthorFields",
    "Book",
    "BookFields",
    "Copy",
    "CopyFields",
    "Rel",
    "Shelf",
    "ShelfFields",
    "predicates",
]
