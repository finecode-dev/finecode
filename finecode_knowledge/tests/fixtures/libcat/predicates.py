"""``libcat``'s shared rule vocabulary, registered against ``LIBCAT_SCHEMA``.

Separate from ``schema.py`` for the same reason ``fine_knowledge`` splits them: a
predicate calls ``q.all_`` and ``Rel.*``, so it imports both ``query/`` and the
schema module, and the schema module therefore cannot import it back. Importing
the ``libcat`` *package* is what fully populates the registry.
"""

from __future__ import annotations

from finecode_knowledge import query as q
from finecode_knowledge.query.predicate import DerivedPredicate
from libcat.schema import LIBCAT_SCHEMA, Author, Book, BookFields, Rel, Shelf

__all__ = ["borrowed_titled", "cited_or_written", "reachable_shelf", "shelves_a_book"]


@q.derived
def shelves_a_book(author: q.Var[Author], shelf: q.Var[Shelf]) -> q.Body:
    """*author* wrote a book that sits on *shelf*. A two-hop join."""
    book = q.var(Book)
    return q.all_(Rel.wrote(author, book), Rel.shelved_on(book, shelf))


@q.derived
def borrowed_titled(author: q.Var[Author], isbn: q.Var[str]) -> q.Body:
    """*author* borrowed a book identified by *isbn*.

    Addressed through ``Book.key``, not ``BookFields.isbn``: the borrowed book
    need not have been catalogued, and under a negation the field spelling would
    fail *open* (ADR-0019 §2).
    """
    book = q.var(Book)
    return q.all_(Rel.borrowed(author, book), Book.key(book, isbn=isbn))


@q.derived
def cited_or_written(author: q.Var[Author], book: q.Var[Book]) -> q.Body:
    """Two clauses, so the union path has something to union."""
    return q.all_(Rel.wrote(author, book))


@cited_or_written.clause
def _(author: q.Var[Author], book: q.Var[Book]) -> q.Body:
    return q.all_(Rel.borrowed(author, book))


@q.derived
def reachable_shelf(src: q.Var[Shelf], dst: q.Var[Shelf]) -> q.Body:
    """Self-recursive: *src* is adjacent to *dst*, directly or through a chain."""
    return q.all_(Rel.adjacent_to(src, dst))


@reachable_shelf.clause
def _(src: q.Var[Shelf], dst: q.Var[Shelf]) -> q.Body:
    mid = q.var(Shelf)
    return q.all_(Rel.adjacent_to(src, mid), reachable_shelf(mid, dst))


@q.derived
def titled(book: q.Var[Book], text: q.Var[str]) -> q.Body:
    """*book* answers to *text* -- by identity or by asserted title."""
    return q.all_(Book.key(book, isbn=text))


@titled.clause
def _(book: q.Var[Book], text: q.Var[str]) -> q.Body:
    return q.all_(BookFields.title(book, text))


PREDICATES: list[DerivedPredicate] = [
    shelves_a_book,
    borrowed_titled,
    cited_or_written,
    reachable_shelf,
    titled,
]
for _predicate in PREDICATES:
    LIBCAT_SCHEMA.register_predicate(_predicate)
