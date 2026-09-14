"""The ``libcat`` vocabulary: authors, books, shelves and physical copies.

Written the way a real schema package writes one -- entity types first, then one
``register_namespace`` call per declaring class, then the providers -- so that
what the engine's tests exercise is the registration path an adopter actually
takes (ADR-0017 D2), not a test-only shortcut into the registry.

**Shaped to cover what the engine needs, not to be a plausible library.** Four
properties are here on purpose because engine behaviour turns on them:

- ``Copy`` has a **composite** ``KEY`` (``isbn`` + ``branch``). A single-field
  key never exercises the ``KEY`` literal's ordering rule or its partial-key
  refusal (ADR-0019 D2/D6).
- ``label`` is declared on **two** entity types. A field is identified by the
  *pair* (ADR-0013 D3), and a suite where every field name is unique cannot see
  a lookup that dropped the entity half.
- ``cites`` is ``Book -> Book``, a **self-relation**, so expansion depth and the
  recursion guard have something to recurse through.
- Each entity type's fields are split across **more than one provider**, which
  is what makes attribution (``query/attribution.py``) discriminate rather than
  return every provider for every key.
"""

from __future__ import annotations

from finecode_knowledge.model.bands import MANY, Band
from finecode_knowledge.model.entity_type import EntityType
from finecode_knowledge.model.fields import Field
from finecode_knowledge.model.provider import EntityProvider
from finecode_knowledge.model.registry import SchemaRegistry, set_default_registry
from finecode_knowledge.model.relationship import Relationship

__all__ = [
    "LIBCAT_SCHEMA",
    "Author",
    "AuthorFields",
    "AuthorIndexProvider",
    "Book",
    "BookFields",
    "CatalogScanProvider",
    "Copy",
    "CopyCensusProvider",
    "CopyFields",
    "Rel",
    "Shelf",
    "ShelfFields",
    "ShelfSurveyProvider",
]


# Fields name their entity class in *annotation* position -- a forward reference that
# stays unevaluated at runtime under `from __future__ import annotations`. The entity
# classes below list these fields in KEY/CORE, so naming them in value position would
# invert that cycle. See `model/fields.py` and ADR-0005.
class AuthorFields:
    handle: Field[Author, str] = Field("handle", entity="Author")
    display_name: Field[Author, str] = Field("display_name", entity="Author")
    homepage: Field[Author, str] = Field("homepage", entity="Author")


class BookFields:
    isbn: Field[Book, str] = Field("isbn", entity="Book")
    title: Field[Book, str] = Field("title", entity="Book")
    label: Field[Book, str] = Field("label", entity="Book")
    """Shares its local name with ``ShelfFields.label`` on purpose: a field is
    identified by the (entity type, field) pair, and a schema where every name is
    unique cannot catch a lookup that used the name alone."""


class ShelfFields:
    code: Field[Shelf, str] = Field("code", entity="Shelf")
    room: Field[Shelf, str] = Field("room", entity="Shelf")
    label: Field[Shelf, str] = Field("label", entity="Shelf")


class CopyFields:
    isbn: Field[Copy, str] = Field("isbn", entity="Copy")
    branch: Field[Copy, str] = Field("branch", entity="Copy")
    condition: Field[Copy, str] = Field("condition", entity="Copy")


class Rel:
    wrote: Relationship[Author, Book] = Relationship(
        "wrote", "Author", "Book", band=Band.DECLARED, lower=0, upper=1
    )
    borrowed: Relationship[Author, Book] = Relationship(
        "borrowed", "Author", "Book", band=Band.DECLARED, lower=0, upper=MANY
    )
    reserved: Relationship[Author, Book] = Relationship(
        "reserved", "Author", "Book", band=Band.DECLARED, lower=0, upper=MANY
    )
    frequents: Relationship[Author, Shelf] = Relationship(
        "frequents", "Author", "Shelf", band=Band.DECLARED, lower=0, upper=MANY
    )
    shelved_on: Relationship[Book, Shelf] = Relationship(
        "shelved_on", "Book", "Shelf", band=Band.DECLARED, lower=0, upper=1
    )
    adjacent_to: Relationship[Shelf, Shelf] = Relationship(
        "adjacent_to", "Shelf", "Shelf", band=Band.DECLARED, lower=0, upper=MANY
    )
    cites: Relationship[Book, Book] = Relationship(
        "cites", "Book", "Book", band=Band.SEMI_DECLARED, lower=0, upper=1
    )
    copy_of: Relationship[Copy, Book] = Relationship(
        "copy_of", "Copy", "Book", band=Band.DECLARED, lower=1, upper=1
    )


class Author(EntityType):
    NAME = "Author"
    KEY = [AuthorFields.handle]
    CORE = [AuthorFields.handle, AuthorFields.display_name]


class Book(EntityType):
    NAME = "Book"
    KEY = [BookFields.isbn]
    CORE = [BookFields.isbn, BookFields.title]


class Shelf(EntityType):
    NAME = "Shelf"
    KEY = [ShelfFields.code]
    CORE = [ShelfFields.code, ShelfFields.room]


class Copy(EntityType):
    NAME = "Copy"
    KEY = [CopyFields.isbn, CopyFields.branch]
    CORE = [CopyFields.isbn, CopyFields.branch, CopyFields.condition]


NAMESPACE_CLASSES: list[type] = [AuthorFields, BookFields, ShelfFields, CopyFields, Rel]
ENTITY_TYPES: list[type[EntityType]] = [Author, Book, Shelf, Copy]

LIBCAT_SCHEMA: SchemaRegistry = SchemaRegistry()
# Declaring the schema is what nominates it (ADR-0017's registration shape), so a
# rule written without `schema=` resolves to it. The engine holds no reference to
# this module in either direction (R20): it is handed a registry, it does not go
# looking for one.
set_default_registry(LIBCAT_SCHEMA)
for _entity_type in ENTITY_TYPES:
    LIBCAT_SCHEMA.register_entity_type(_entity_type)
for _namespace in NAMESPACE_CLASSES:
    LIBCAT_SCHEMA.register_namespace(_namespace)


# Providers come after registration for the same reason as in a real schema package:
# `source_inputs` resolves each supplied member's declaring module through the
# registry (ADR-0026 D2), so the members have to be registered first.
class AuthorIndexProvider(EntityProvider):
    """Reads the authority file. Owns an author's identity and display name."""

    ID = "author_index"
    SUPPLIES_FIELDS = [AuthorFields.handle, AuthorFields.display_name]
    SUPPLIES_EDGES = [Rel.cites]


class CatalogScanProvider(EntityProvider):
    """Reads catalogue records. Owns book bibliography and an author's homepage."""

    ID = "catalog_scan"
    SUPPLIES_FIELDS = [
        AuthorFields.homepage,
        BookFields.isbn,
        BookFields.title,
        BookFields.label,
    ]
    SUPPLIES_EDGES = [Rel.wrote, Rel.borrowed, Rel.reserved, Rel.frequents]


class ShelfSurveyProvider(EntityProvider):
    """Walks the building. Owns shelving."""

    ID = "shelf_survey"
    SUPPLIES_FIELDS = [ShelfFields.code, ShelfFields.room, ShelfFields.label]
    SUPPLIES_EDGES = [Rel.shelved_on, Rel.adjacent_to]


class CopyCensusProvider(EntityProvider):
    """Counts physical copies. The only supplier of the composite-key type."""

    ID = "copy_census"
    SUPPLIES_FIELDS = [CopyFields.isbn, CopyFields.branch, CopyFields.condition]
    SUPPLIES_EDGES = [Rel.copy_of]


PROVIDERS: list[type[EntityProvider]] = [
    AuthorIndexProvider,
    CatalogScanProvider,
    ShelfSurveyProvider,
    CopyCensusProvider,
]
for _provider in PROVIDERS:
    LIBCAT_SCHEMA.register_provider(_provider)
