"""A second package extending ``libcat``'s schema from outside it (R18/R19).

The claim under test is that the engine holds **no privileged path**: a package
that did not declare the schema participates on the same terms as the one that
did. So everything ``libcat`` here is reached **through the registry, by
qualified name** -- never by importing the Python object. A registry lookup is
the only access R19 guarantees, and importing the object would quietly test a
capability a real adopter may not have.

``annex_ext`` is deliberately not a fixture of ``libcat``: it declares its own
field on an entity ``libcat`` owns, its own provider to supply it, its own
predicate over both vocabularies, and its own rule. Every one of those is a
namespace the core would have had to be edited to accept if R18 did not hold.
"""

from __future__ import annotations

from libcat.schema import LIBCAT_SCHEMA

from finecode_knowledge import query as q
from finecode_knowledge.model.fields import Field
from finecode_knowledge.model.provider import EntityProvider

# Resolved by qualified name. `libcat` is not privileged: `annex_ext` reaches its
# names exactly as `libcat` reaches its own.
Author = LIBCAT_SCHEMA.entity_type("libcat.Author")
Book = LIBCAT_SCHEMA.entity_type("libcat.Book")
Shelf = LIBCAT_SCHEMA.entity_type("libcat.Shelf")
wrote = LIBCAT_SCHEMA.relationship("libcat.wrote")
shelves_a_book = LIBCAT_SCHEMA.predicate("libcat.shelves_a_book")


class AnnexAuthorFields:
    """A field ``annex_ext`` declares on an entity ``libcat`` owns.

    The entity is spelled qualified because ``annex_ext`` does not own it -- the
    cross-package case ADR-0017 D4 exists for, and the reason the qualifier is
    derived rather than typed: ``annex_ext`` cannot claim ``libcat.`` for its own
    field by writing the prefix, because it never writes it.
    """

    lending_ban: Field = Field("lending_ban", entity="libcat.Author")


LIBCAT_SCHEMA.register_namespace(AnnexAuthorFields)


class AnnexAuditProvider(EntityProvider):
    """``annex_ext`` must supply its own field.

    SUPPLIES-bounding is enforced at ingest (C4) and knows nothing about who owns
    the *entity*: a third party adding a field to another package's entity also
    has to declare a provider for it, and ``libcat``'s providers cannot write it
    on their behalf.
    """

    ID = "annex_audit"
    SUPPLIES_FIELDS = [AnnexAuthorFields.lending_ban]
    SUPPLIES_EDGES = []


LIBCAT_SCHEMA.register_provider(AnnexAuditProvider)


@q.derived
def banned_author_on_shelf(author: q.Var[Author], shelf: q.Var[Shelf]) -> q.Body:  # type: ignore[valid-type]
    """*author* is under a lending ban and still has a book on *shelf*.

    A body mixing ``libcat``'s derived predicate with ``annex_ext``'s own field
    literal, at identical spelling (FR2) -- the reuse claim, executing.
    """
    return q.all_(
        shelves_a_book(author, shelf),
        AnnexAuthorFields.lending_ban(author, "yes"),
    )


LIBCAT_SCHEMA.register_predicate(banned_author_on_shelf)


@q.rule
def banned_author_still_shelved(
    subject: q.Var[Author],  # type: ignore[valid-type]
    missing: q.Var[str],
    asserted_at: q.Prov,
) -> q.Body:
    """author {subject} is banned from lending but still has a book on shelf {missing}"""
    shelf, book = q.var(Shelf), q.var(Book)
    return q.all_(
        banned_author_on_shelf(subject, shelf),
        wrote(subject, book, at=asserted_at),
        Shelf.key(shelf, code=missing),
    )
