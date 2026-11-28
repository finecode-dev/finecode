"""Node keys and the code-version rows in them (Phase 2, D-2, D-3, R8).

§4.5's rule -- *an input that is not in the key is an input that cannot
invalidate* -- is only testable one input at a time, so that is how it is tested:
change one thing, assert the key moves; change something semantically irrelevant,
assert it does not.

The row easiest to lose is the last group here. A query carries derived
predicates **by name**, so editing a predicate's body moves nothing in the
serialized query; without the predicate version hashes in the key, a rewritten
predicate would serve the previous one's answer.
"""

from __future__ import annotations

import inspect

from libcat import LIBCAT_SCHEMA, Author, Book, BookFields, Rel, Shelf

from finecode_knowledge import query as q
from finecode_knowledge.memo.keys import (
    extraction_key,
    query_key,
    query_location_sensitive,
    referenced_predicates,
)
from finecode_knowledge.memo.node import NodeKind
from finecode_knowledge.query.version import body_version_hash, clauses_version_hash


def _key(built: q.Query, *, limit: int | None = None):
    return query_key(built, LIBCAT_SCHEMA, limit=limit)


def _joined() -> q.Query:
    author, book, isbn = q.var(Author), q.var(Book), q.var(str)
    return q.query(author, isbn).where(
        Rel.wrote(author, book), BookFields.isbn(book, isbn)
    )


# ---- identity is a value (D-2) -----------------------------------------


def test_the_same_question_asked_twice_is_one_node() -> None:
    """Built from different ``Var`` objects, which compare by identity. If that
    identity reached the key, the memo would miss on every re-import of a rule
    module -- a memo that looks like it works and never hits."""
    assert _key(_joined()) == _key(_joined())


def test_a_different_question_is_a_different_node() -> None:
    author, book = q.var(Author), q.var(Book)
    other = q.query(author).where(Rel.borrowed(author, book))

    assert _key(_joined()) != _key(other)


def test_a_changed_constant_moves_the_key() -> None:
    """The literal, not the shape. Two queries with the same structure and
    different filters are different questions with different answers."""
    book = q.var(Book)
    first = q.query(book).where(BookFields.isbn(book, "a-1"))
    second = q.query(book).where(BookFields.isbn(book, "b-9"))

    assert _key(first) != _key(second)


def test_the_row_limit_is_part_of_the_node() -> None:
    """It changes the answer, so it changes the node. Sharing one node between a
    full scan and a ``limit=1`` existence check would serve one row where every
    row was asked for."""
    built = _joined()

    assert _key(built) != _key(built, limit=1)


def test_the_read_mode_is_not_a_key_component() -> None:
    """Phase 5 reversed this, and the reversal is the mode's whole value.

    ``Mode`` says how hard to look, not what to look for. Keyed on it,
    ``Mode.CACHED`` would get a private cache -- hitting only values some earlier
    cached read computed, and never seeing the memo a verified LSP pass just
    filled, which is exactly the latency §4.12 introduced the mode to pay down.
    Asserted on the signature because the property is that the key *cannot*
    depend on the mode, not that it happens not to.
    """
    assert "mode" not in inspect.signature(query_key).parameters


def test_an_extraction_node_is_keyed_by_its_bucket() -> None:
    """D-2's first row: the ownership unit *is* the node identity."""
    assert extraction_key(("libcat.catalog_scan", "c.toml")) == (
        NodeKind.EXTRACTION.value,
        "libcat.catalog_scan",
        "c.toml",
    )


# ---- R8's code-version row (D-3) ---------------------------------------


def test_a_referenced_predicates_version_is_in_the_key() -> None:
    """The row nothing supplied before this phase. A query carries a predicate by
    name, so without this a rewritten body would be invisible to the key."""
    predicate = LIBCAT_SCHEMA.predicate("libcat.shelves_a_book")
    author, shelf = q.var(Author), q.var(Shelf)
    built = q.query(author, shelf).where(predicate(author, shelf))

    versions = referenced_predicates(built.body, LIBCAT_SCHEMA)

    assert versions == {"libcat.shelves_a_book": predicate.version_hash}


def test_referenced_predicates_are_collected_transitively() -> None:
    """A predicate that calls another inherits its code as an input: editing the
    inner body changes the outer's answer without changing the outer's own IR.
    One hop would leave exactly that case silently memoized."""

    @q.derived
    def _outer(author: q.Var[Author], shelf: q.Var[Shelf]) -> q.Body:
        inner = LIBCAT_SCHEMA.predicate("libcat.shelves_a_book")
        return q.all_(inner(author, shelf))

    LIBCAT_SCHEMA.register_predicate(_outer)
    author, shelf = q.var(Author), q.var(Shelf)
    built = q.query(author, shelf).where(_outer(author, shelf))

    versions = referenced_predicates(built.body, LIBCAT_SCHEMA)

    assert set(versions) == {_outer.id, "libcat.shelves_a_book"}


def test_a_self_recursive_predicate_terminates() -> None:
    """``reachable_shelf`` calls itself. A version walk that did not stop would not
    return, and recursion is a legitimate definition (FR10)."""
    predicate = LIBCAT_SCHEMA.predicate("libcat.reachable_shelf")
    src, dst = q.var(Shelf), q.var(Shelf)
    built = q.query(src, dst).where(predicate(src, dst))

    versions = referenced_predicates(built.body, LIBCAT_SCHEMA)

    assert versions == {"libcat.reachable_shelf": predicate.version_hash}


def test_a_predicate_the_registry_does_not_hold_is_skipped_not_raised_on() -> None:
    """The registry is the authority on resolution and the interpreter fails on an
    unknown predicate with a message naming the call site. Failing here would
    replace that with a memo-layer error about a key."""
    from finecode_knowledge.model.literal import Conjunction, Literal, LiteralKind

    body = Conjunction(
        literals=(
            Literal(kind=LiteralKind.DERIVED, predicate="libcat.no_such", terms=()),
        )
    )

    assert referenced_predicates(body, LIBCAT_SCHEMA) == {}


# ---- version hashes (2.2) ----------------------------------------------


def test_renaming_a_derived_predicates_head_parameter_does_not_move_its_version() -> (
    None
):
    """The engine unifies positionally, so the name is documentation. A hash that
    moved on a rename would invalidate correct memo entries over a comment."""

    @q.derived
    def _first(author: q.Var[Author], shelf: q.Var[Shelf]) -> q.Body:
        book = q.var(Book)
        return q.all_(Rel.wrote(author, book), Rel.shelved_on(book, shelf))

    @q.derived
    def _second(writer: q.Var[Author], place: q.Var[Shelf]) -> q.Body:
        volume = q.var(Book)
        return q.all_(Rel.wrote(writer, volume), Rel.shelved_on(volume, place))

    assert _first.version_hash == _second.version_hash


def test_changing_a_literal_moves_a_derived_predicates_version() -> None:
    @q.derived
    def _wrote(author: q.Var[Author], book: q.Var[Book]) -> q.Body:
        return q.all_(Rel.wrote(author, book))

    @q.derived
    def _borrowed(author: q.Var[Author], book: q.Var[Book]) -> q.Body:
        return q.all_(Rel.borrowed(author, book))

    assert _wrote.version_hash != _borrowed.version_hash


def test_changing_a_constant_moves_the_version() -> None:
    @q.derived
    def _one(book: q.Var[Book], shelf: q.Var[Shelf]) -> q.Body:
        return q.all_(BookFields.isbn(book, "a-1"), Rel.shelved_on(book, shelf))

    @q.derived
    def _other(book: q.Var[Book], shelf: q.Var[Shelf]) -> q.Body:
        return q.all_(BookFields.isbn(book, "b-9"), Rel.shelved_on(book, shelf))

    assert _one.version_hash != _other.version_hash


def test_adding_a_clause_moves_the_version() -> None:
    @q.derived
    def _one_clause(author: q.Var[Author], book: q.Var[Book]) -> q.Body:
        return q.all_(Rel.wrote(author, book))

    before = _one_clause.version_hash

    @_one_clause.clause
    def _(author: q.Var[Author], book: q.Var[Book]) -> q.Body:
        return q.all_(Rel.borrowed(author, book))

    assert _one_clause.version_hash != before


def test_a_rules_head_names_are_in_its_version_because_they_pick_violation_fields() -> (
    None
):
    """Where a rule differs from a derived predicate, and why that is not an
    inconsistency: ``ViolationBuilder`` maps head names onto ``Violation`` fields by
    name, so swapping two produces different violations from identical rows."""
    body = q.all_(Rel.wrote(q.var(Author), q.var(Book)))

    straight = body_version_hash(body, head=("subject", "missing"), extra="m")
    swapped = body_version_hash(body, head=("missing", "subject"), extra="m")

    assert straight != swapped


def test_a_rules_message_is_in_its_version() -> None:
    """User-visible output built from the same bindings. A memoized violation
    carrying the previous message is a wrong answer in the only field a developer
    reads."""
    body = q.all_(Rel.wrote(q.var(Author), q.var(Book)))

    assert body_version_hash(body, extra="before") != body_version_hash(
        body, extra="after"
    )


def test_clause_order_is_part_of_a_predicates_version() -> None:
    """A predicate's value is the union of its clauses, so order does not change the
    answer -- but two definitions that differ in it are two definitions, and keeping
    order costs nothing."""

    @q.derived
    def _ab(author: q.Var[Author], book: q.Var[Book]) -> q.Body:
        return q.all_(Rel.wrote(author, book))

    @_ab.clause
    def _(author: q.Var[Author], book: q.Var[Book]) -> q.Body:
        return q.all_(Rel.borrowed(author, book))

    @q.derived
    def _ba(author: q.Var[Author], book: q.Var[Book]) -> q.Body:
        return q.all_(Rel.borrowed(author, book))

    @_ba.clause
    def _(author: q.Var[Author], book: q.Var[Book]) -> q.Body:
        return q.all_(Rel.wrote(author, book))

    assert _ab.version_hash != _ba.version_hash
    assert clauses_version_hash(_ab.predicate.clauses) == _ab.version_hash


# ---- location sensitivity (2.4, ADR-0027 D3) ---------------------------


def test_a_rule_with_a_prov_head_parameter_is_location_sensitive() -> None:
    @q.rule(id="libcat.sensitive_probe")
    def _sensitive(subject: q.Var[Author], asserted_at: q.Prov) -> q.Body:
        """author {subject} wrote something"""
        return q.all_(Rel.wrote(subject, q.var(Book), at=asserted_at))

    assert _sensitive.location_sensitive


def test_a_rule_without_one_is_not() -> None:
    @q.rule(id="libcat.insensitive_probe")
    def _insensitive(subject: q.Var[Author], missing: q.Var[str]) -> q.Body:
        """author {subject} wrote {missing}"""
        book = q.var(Book)
        return q.all_(Rel.wrote(subject, book), BookFields.isbn(book, missing))

    assert not _insensitive.location_sensitive


def test_a_derived_predicate_is_classified_the_same_way() -> None:
    assert not LIBCAT_SCHEMA.predicate("libcat.shelves_a_book").location_sensitive


def test_sensitivity_does_not_propagate_from_a_body_to_a_head() -> None:
    """D3's locality argument, which is what keeps this a one-line test rather than
    a dataflow analysis: a node that reads a location without projecting it cannot
    carry one."""

    @q.derived
    def _reads_prov(author: q.Var[Author], book: q.Var[Book]) -> q.Body:
        # A Prov is bound in the body and thrown away -- it reaches no head term.
        return q.all_(Rel.wrote(author, book, at=q.Prov()))

    assert not _reads_prov.location_sensitive


def test_a_query_is_sensitive_when_a_prov_reaches_its_projection() -> None:
    """A query's projection *is* its head: exactly what the consumer receives."""
    author, book, at = q.var(Author), q.var(Book), q.Prov()

    sensitive = q.query(author, at).where(Rel.wrote(author, book, at=at))
    insensitive = q.query(author).where(Rel.wrote(author, book, at=at))

    assert query_location_sensitive(sensitive, LIBCAT_SCHEMA)
    assert not query_location_sensitive(insensitive, LIBCAT_SCHEMA)
