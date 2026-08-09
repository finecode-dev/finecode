"""The openCypher 9 compiler's dialect constraints (ADR-0015 D2).

**Where the golden corpus is, and why it is not here.** ``fine_knowledge`` keeps
a golden file per rule plus a test asserting that *every* rule has one -- a
coverage contract over a specific rule set, which is the form NFR4's retargeting
claim takes for that package and which cannot exist in a package that declares no
rules. Moving it here would have deleted the contract rather than relocated it.

What belongs here instead is the compiler's own behaviour: the lowerings the
probed dialect forces, asserted against ``libcat``. Those are engine properties,
and before this file they were only ever checked through FineCode's vocabulary.

Nothing here executes Cypher: no server, no driver, no engine in the test path
(NFR3, NFR7).
"""

from __future__ import annotations

import pytest
from libcat import LIBCAT_SCHEMA, Author, Book, BookFields, Rel, Shelf

from finecode_knowledge import query as q
from finecode_knowledge.query.cypher import (
    CypherCompilationError,
    compile_query,
    compile_rule,
)


@q.derived
def _borrowed_isbn(author: q.Var[Author], isbn: q.Var[str]) -> q.Body:
    """Addressed by identity, so the negation below stays a single path."""
    book = q.var(Book)
    return q.all_(Rel.borrowed(author, book), Book.key(book, isbn=isbn))


@q.derived
def _points_at(src: q.Var[Author], shelf: q.Var[Shelf]) -> q.Body:
    return q.all_(Rel.frequents(src, shelf))


@_points_at.clause
def _(src: q.Var[Shelf], shelf: q.Var[Shelf]) -> q.Body:
    return q.all_(Rel.adjacent_to(src, shelf))


for _predicate in (_borrowed_isbn, _points_at):
    LIBCAT_SCHEMA.register_predicate(_predicate)


@q.rule
def shelved_book_not_borrowed(
    subject: q.Var[Author],
    missing: q.Var[str],
    asserted_at: q.Prov,
    expected_in: q.Prov,
) -> q.Body:
    """author {subject} shelved a book but never borrowed {missing}"""
    book, shelf = q.var(Book), q.var(Shelf)
    return q.all_(
        Rel.wrote(subject, book, at=asserted_at),
        Rel.shelved_on(book, shelf),
        Shelf.key(shelf, code=missing),
        BookFields.title(book, q.var(str), at=expected_in),
        q.not_(_borrowed_isbn(subject, missing)),
    )


# ---- the dialect's constraints show up in the output -------------------


def test_negation_is_a_pattern_predicate_not_a_subquery() -> None:
    """§3.7 probed ``WHERE NOT EXISTS { MATCH ... }`` against FalkorDB and it **fails
    to parse**. The compiler targets that dialect, not Neo4j 5, so output containing
    the subquery form would be output that cannot run."""
    emitted = compile_rule(shelved_book_not_borrowed)

    assert "NOT (subject)-[:`libcat.borrowed`]->" in emitted
    assert "NOT EXISTS" not in emitted


def test_a_negated_predicate_is_one_pattern_not_separately_negated_literals() -> None:
    """``_borrowed_isbn`` is ``borrowed(a, b) AND Book.key(b, isbn=n)``, so its negation
    is ``NOT (a AND b)``. Lowering it to ``NOT a AND NOT b`` would compute a different,
    weaker question -- and would fire whenever the author had borrowed *anything*."""
    emitted = compile_rule(shelved_book_not_borrowed)

    assert emitted.count("NOT ") == 1
    assert "{isbn: missing}" in emitted, (
        "the key literal must fold into the endpoint node, which is what keeps the "
        "negated body a single path"
    )


def test_clause_disjunction_becomes_a_union_of_whole_queries() -> None:
    """A predicate's value *is* the union of its clauses, so this is real openCypher
    rather than a compiler artifact."""
    src, shelf = q.var(Author), q.var(Shelf)
    built = q.query(src).where(_points_at(src, shelf))

    emitted = compile_query(built)

    assert emitted.count("\nUNION\n") == 1
    assert "libcat.frequents" in emitted
    assert "libcat.adjacent_to" in emitted


def test_a_key_literal_lowers_to_the_identity_encoding_never_a_fact_match() -> None:
    """ADR-0019 D7 states this so the compiler is not written against the fact-scan
    lowering and found to diverge at the conformance gate."""
    book = q.var(Book)
    built = q.query(book).where(Book.key(book, isbn="a-1"))

    emitted = compile_query(built)

    assert "{isbn: 'a-1'}" in emitted
    assert ":Fact" not in emitted


def test_the_field_spelling_still_lowers_to_a_fact_match() -> None:
    """The other half of ADR-0019 D4, visible in the emitted string: the field literal
    asks what was *asserted*, and compiles to a fact match."""
    book, title = q.var(Book), q.var(str)
    built = q.query(book).where(BookFields.title(book, title))

    emitted = compile_query(built)

    assert ":Fact {field: 'libcat.title'}" in emitted


def test_provenance_binds_to_the_relationship_variable() -> None:
    """Edge properties in ``RETURN`` work on the probed dialect (§3.7), which is what
    makes ``at=`` on an edge free."""
    emitted = compile_rule(shelved_book_not_borrowed)

    assert "-[asserted_at:`libcat.wrote`]->" in emitted
    assert "RETURN subject, missing, asserted_at, expected_in" in emitted


def test_a_field_facts_provenance_binds_to_a_fact_node() -> None:
    """A property has no identity to name, so a field fact is encoded as a node --
    which is what lets one rule compile to both targets unchanged."""
    emitted = compile_rule(shelved_book_not_borrowed)

    assert "-[:asserted]->(expected_in:Fact {field: 'libcat.title'})" in emitted


def test_qualified_names_are_backtick_quoted() -> None:
    """Every schema name carries a dot (ADR-0017 D4), which is not a bare openCypher
    identifier. Unquoted output would not parse."""
    emitted = compile_rule(shelved_book_not_borrowed)

    assert "`libcat.Author`" in emitted
    assert ":libcat.Author" not in emitted.replace("`", "\x00")


def test_one_rule_object_drives_both_compilers_unchanged() -> None:
    """The retargeting claim restated (D3): the rule does not change when the backend
    changes. Same ``Rule``, two lowerings -- and only the interpreter executes (D1)."""
    rule = shelved_book_not_borrowed

    assert rule.query.body is rule.query.body
    assert compile_rule(rule).startswith(f"// {rule.id}")


# ---- limits are named, not silently mis-emitted ------------------------


def test_a_negated_body_that_is_not_a_single_path_is_refused() -> None:
    """Better a named refusal than Cypher that cannot run. The message says why the
    dialect forces it."""

    @q.derived
    def _two_edges(author: q.Var[Author], book: q.Var[Book]) -> q.Body:
        shelf = q.var(Shelf)
        return q.all_(Rel.wrote(author, book), Rel.shelved_on(book, shelf))

    LIBCAT_SCHEMA.register_predicate(_two_edges)
    author, book = q.var(Author), q.var(Book)
    built = q.query(author).where(
        Rel.wrote(author, book), q.not_(_two_edges(author, book))
    )

    with pytest.raises(CypherCompilationError, match="single path"):
        compile_query(built)
