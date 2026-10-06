"""The IR and its construction-time checks (§5.2-5.4, §5.8).

Nothing here executes. What is testable without a backend is the property
acceptance criterion 3 names -- **a typo fails at build time** -- which §5.8
delivers without a type checker, because ADR-0004 D7 means a third party may
never run one.

Written against ``libcat``, the engine's own synthetic schema, rather than
against FineCode's. The distinction is R18/R19's: these are tests of the
*engine*, and an engine exercised only through one specific vocabulary cannot
tell "generic" apart from "knows core's names". See ``tests/fixtures/libcat``.
"""

from __future__ import annotations

import pytest
from libcat import (
    LIBCAT_SCHEMA,
    Author,
    AuthorFields,
    Book,
    BookFields,
    Rel,
    Shelf,
    ShelfFields,
)

from finecode_knowledge import query as q
from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.literal import Conjunction, Literal, LiteralKind
from finecode_knowledge.query.predicate import PredicateShape
from finecode_knowledge.query.validate import validate_body

# ---- §5.2 terms and literals ------------------------------------------


def test_a_field_and_an_edge_are_spelled_identically() -> None:
    """FR4's whole content: a rule author calls a schema object, and does not have to
    know whether it is stored as a field or as an edge."""
    author, book, isbn = q.var(Author), q.var(Book), q.var(str)

    edge = Rel.wrote(author, book)
    field = BookFields.isbn(book, isbn)

    assert edge.kind is LiteralKind.EDGE
    assert field.kind is LiteralKind.FIELD
    assert edge.terms == (author, book)
    assert field.terms == (book, isbn)


def test_a_constant_is_a_legal_term() -> None:
    book = q.var(Book)

    literal = BookFields.isbn(book, "978-0")

    assert literal.terms == (book, "978-0")


def test_a_field_literal_carries_the_entity_type_not_just_the_field_name() -> None:
    """A field is identified by the pair: ``label`` is declared on both ``Book`` and
    ``Shelf``, so the name alone does not say which slot the literal reads."""
    book_label = BookFields.label(q.var(Book), q.var(str))
    shelf_label = ShelfFields.label(q.var(Shelf), q.var(str))

    assert book_label.predicate == shelf_label.predicate, "same qualified field name"
    assert book_label.entity_type == Book.qualified_name()
    assert shelf_label.entity_type == Shelf.qualified_name()
    assert book_label.slot != shelf_label.slot, (
        "the entity half is what separates them; dropping it conflates two slots"
    )


def test_at_binds_provenance_as_an_ordinary_term() -> None:
    """FR5. Provenance is a bindable term, checkable like any other, not a
    stringly-typed side channel."""
    at = q.Prov()

    literal = Rel.adjacent_to(q.var(Shelf), q.var(Shelf), at=at)

    assert literal.at is at


def test_two_fresh_variables_are_distinct_even_at_the_same_type() -> None:
    """Vars compare by identity. If they compared structurally, a body naming two
    different Shelves would silently join them into one."""
    a, b = q.var(Shelf), q.var(Shelf)

    assert a is not b
    assert len({id(a), id(b)}) == 2


def test_a_body_is_a_value_not_a_stream() -> None:
    """ADR-0007. ``q.all_`` returns a Conjunction that can be held, inspected and
    re-read -- which is what makes §5.8's checks and §5.9's serialization direct."""
    author, book = q.var(Author), q.var(Book)

    body = q.all_(Rel.wrote(author, book), BookFields.isbn(book, "x"))

    assert isinstance(body, Conjunction)
    assert len(body) == 2
    assert [literal.predicate for literal in body] == list(
        literal.predicate for literal in body
    )  # re-iterable


def test_all_rejects_a_non_literal_naming_what_it_got() -> None:
    with pytest.raises(TypeError, match="q.all_ takes literals"):
        q.all_("not a literal")  # type: ignore[arg-type]


# ---- §5.4 negation ----------------------------------------------------


def test_not_negates_exactly_one_literal() -> None:
    literal = Rel.borrowed(q.var(Author), q.var(Book))

    negated = q.not_(literal)

    assert negated.negated
    assert not literal.negated  # the original is untouched; Literal is frozen


def test_double_negation_is_not_in_the_ir() -> None:
    with pytest.raises(TypeError, match="double negation"):
        q.not_(q.not_(Rel.borrowed(q.var(Author), q.var(Book))))


def test_not_rejects_a_conjunction_and_says_how_to_factor_it() -> None:
    """There is no inline negated conjunction (FR3): an existential inside a negation
    is factored into a named predicate, which is what keeps negation a single literal
    the engine can evaluate as a membership test."""
    with pytest.raises(TypeError, match="factor it into a `@q.derived` predicate"):
        q.not_(q.all_(Rel.borrowed(q.var(Author), q.var(Book))))  # type: ignore[arg-type]


# ---- §5.3 derived predicates ------------------------------------------


@q.derived
def shelves_a_book(author: q.Var[Author], shelf: q.Var[Shelf]) -> q.Body:
    """*author* wrote a book that sits on *shelf*."""
    book = q.var(Book)
    return q.all_(Rel.wrote(author, book), Rel.shelved_on(book, shelf))


@q.derived
def points_at_shelf(src: q.Var[Shelf], shelf: q.Var[Shelf], at: q.Prov) -> q.Body:
    return q.all_(Rel.adjacent_to(src, shelf, at=at))


@points_at_shelf.clause
def _(src: q.Var[Author], shelf: q.Var[Shelf], at: q.Prov) -> q.Body:
    return q.all_(Rel.frequents(src, shelf, at=at))


@q.derived
def cited_by(book: q.Var[Book], citing: q.Var[Book]) -> q.Body:
    """The inverse traversal of ``cites`` -- an edge-shaped head."""
    return q.all_(Rel.cites(citing, book))


@q.derived
def named(book: q.Var[Book], text: q.Var[str]) -> q.Body:
    return q.all_(BookFields.isbn(book, text))


@named.clause
def _(book: q.Var[Book], text: q.Var[str]) -> q.Body:
    return q.all_(BookFields.title(book, text))


def test_calling_a_predicate_does_not_run_its_body() -> None:
    """It returns a Literal whose predicate is the DerivedPredicate; the engine expands
    it later. That is what makes a derived call identical at the call site to a base
    relation (FR2) and what makes recursion a fixpoint rather than infinite inlining."""
    literal = shelves_a_book(q.var(Author), q.var(Shelf))

    assert isinstance(literal, Literal)
    assert literal.kind is LiteralKind.DERIVED
    assert literal.predicate == shelves_a_book.id


def test_a_predicate_call_is_indistinguishable_from_a_base_relation_at_the_call_site() -> (
    None
):
    """FR2. The ``store.resolve()`` / ``store.targets_of()`` split disappears."""
    author, shelf = q.var(Author), q.var(Shelf)

    derived_literal = shelves_a_book(author, shelf)
    base_literal = Rel.frequents(author, shelf)

    assert type(derived_literal) is type(base_literal)
    assert derived_literal.terms == base_literal.terms


def test_head_shape_is_inferred_from_the_signature_with_nothing_re_declared() -> None:
    """ADR-0012 D-A. ``@q.derived`` takes no kind/src/dst: a second declaration is what
    ADR-0007 removed, and it could drift from the signature it duplicates."""
    assert cited_by.shape is PredicateShape.EDGE
    assert cited_by.endpoints == (Book.qualified_name(), Book.qualified_name())

    assert named.shape is PredicateShape.ATTRIBUTE
    assert named.endpoints[0] == Book.qualified_name()

    assert points_at_shelf.shape is PredicateShape.UNREPRESENTABLE, (
        "a Prov in the head has no ER rendering; it must be classified, not guessed at"
    )


def test_a_second_clause_is_disjunction() -> None:
    """§5.3. A predicate whose two clauses read different relationships is a union, and
    a union is what invalidated the conjunction-only assumption."""
    predicate = points_at_shelf.predicate

    assert len(predicate.clauses) == 2
    kinds = {clause.body.literals[0].predicate for clause in predicate.clauses}
    assert kinds == {Rel.adjacent_to.qualified_name, Rel.frequents.qualified_name}


def test_each_clause_owns_its_own_head_variables() -> None:
    """Clauses are separate defs, so the engine must unify a call's terms against each
    clause's own head and rename the rest apart."""
    first, second = points_at_shelf.predicate.clauses

    assert first.head != second.head
    assert all(isinstance(term, q.Var) for term in first.head)


def test_a_clause_of_the_wrong_arity_is_rejected() -> None:
    with pytest.raises(SchemaError, match="arity"):

        @points_at_shelf.clause
        def _bad(src: q.Var[Shelf], shelf: q.Var[Shelf]) -> q.Body:
            return q.all_(Rel.adjacent_to(src, shelf))


def test_a_head_parameter_without_an_annotation_is_rejected() -> None:
    """The head signature is the only declaration of the predicate's shape, so an
    unannotated parameter leaves the shape undeclared rather than defaulted."""
    with pytest.raises(SchemaError, match="no annotation"):

        @q.derived
        def _bad(author, shelf: q.Var[Shelf]) -> q.Body:  # type: ignore[no-untyped-def]
            return q.all_(Rel.frequents(author, shelf))


def test_calling_a_predicate_with_a_wrong_keyword_names_the_head() -> None:
    with pytest.raises(SchemaError, match="has no head parameter 'shellf'"):
        shelves_a_book(q.var(Author), shellf=q.var(Shelf))


def test_calling_a_predicate_with_a_missing_term_names_it() -> None:
    with pytest.raises(SchemaError, match="missing term"):
        shelves_a_book(q.var(Author))


# ---- §5.8 construction-time validation: criterion 3 -------------------


def test_a_field_of_the_wrong_entity_is_rejected_at_construction() -> None:
    """Acceptance criterion 3, without a type checker. ``ShelfFields.code`` applied to a
    Book variable type-checks as an error under mypy, and must *also* fail here,
    because ADR-0004 D7 means a third party may never run mypy."""
    book = q.var(Book)
    body = q.all_(ShelfFields.code(book, q.var(str)))

    with pytest.raises(SchemaError, match="expects entity of type"):
        validate_body(body, LIBCAT_SCHEMA, context="test", projected=())


def test_a_wrong_direction_traversal_is_rejected_at_construction() -> None:
    """Wrong-direction traversal would otherwise return empty, which is
    indistinguishable from a rule that passes -- the silent-wrong failure this design
    rejects everywhere."""
    author, book = q.var(Author), q.var(Book)
    body = q.all_(Rel.wrote(book, author))  # backwards

    with pytest.raises(SchemaError, match="expects src of type"):
        validate_body(body, LIBCAT_SCHEMA, context="test", projected=())


def test_a_negated_variable_with_no_positive_binding_is_rejected() -> None:
    """§5.4's safety condition, named per NFR6."""
    author, book = q.var(Author), q.var(Book)
    body = q.all_(q.not_(Rel.borrowed(author, book)))

    with pytest.raises(SchemaError, match="which no earlier positive literal binds"):
        validate_body(body, LIBCAT_SCHEMA, context="test", projected=())


def test_a_negated_variable_bound_earlier_is_accepted() -> None:
    author, book = q.var(Author), q.var(Book)
    body = q.all_(
        Rel.wrote(author, book),
        q.not_(Rel.borrowed(author, book)),
    )

    validate_body(body, LIBCAT_SCHEMA, context="test", projected=(author,))


def test_an_unbound_projected_variable_is_rejected() -> None:
    """Datalog range restriction. It is also what lets the interpreter never enumerate
    a type's population: if every variable is bound positively, there is nothing to
    enumerate."""
    author, orphan = q.var(Author), q.var(Shelf)
    body = q.all_(Rel.wrote(author, q.var(Book)))

    with pytest.raises(SchemaError, match="is not bound by any positive literal"):
        validate_body(body, LIBCAT_SCHEMA, context="test", projected=(orphan,))


def test_a_variable_appearing_only_under_negation_is_rejected() -> None:
    """A negated literal is a membership test, so it binds nothing -- projecting from it
    would ask for the bindings of a thing that must not exist.

    The two §5.8 checks overlap here by construction: negation safety already
    requires every negated variable to be positively bound *earlier*, so a variable
    reachable only through a negation always trips that check first. Range
    restriction's skip of negated literals is therefore belt-and-braces, and this
    asserts the reachable behaviour rather than a message it cannot produce."""
    author, book = q.var(Author), q.var(Book)
    body = q.all_(
        AuthorFields.handle(author, q.var(str)),
        q.not_(Rel.borrowed(author, book)),
    )

    with pytest.raises(SchemaError, match="no earlier positive literal binds"):
        validate_body(body, LIBCAT_SCHEMA, context="test", projected=(book,))


def test_an_unbound_prov_in_the_head_says_how_to_bind_it() -> None:
    at = q.Prov()
    body = q.all_(Rel.frequents(q.var(Author), q.var(Shelf)))

    with pytest.raises(SchemaError, match="an `at=` on a base literal binds a Prov"):
        validate_body(body, LIBCAT_SCHEMA, context="test", projected=(at,))


def test_a_predicate_body_is_validated_at_declaration_not_at_first_execution() -> None:
    """§5.8's whole point: a rule module fails at *import*."""
    with pytest.raises(SchemaError, match="predicate .*expects src of type"):

        @q.derived
        def _backwards(book: q.Var[Book], author: q.Var[Author]) -> q.Body:
            return q.all_(Rel.wrote(book, author))


def test_a_predicate_whose_head_is_not_bound_by_its_body_is_rejected() -> None:
    with pytest.raises(SchemaError, match="is not bound by any positive literal"):

        @q.derived
        def _dangling(author: q.Var[Author], shelf: q.Var[Shelf]) -> q.Body:
            return q.all_(Rel.wrote(author, q.var(Book)))


def test_a_predicate_body_that_is_not_a_conjunction_is_rejected() -> None:
    with pytest.raises(SchemaError, match="must be a Conjunction"):

        @q.derived
        def _not_a_body(author: q.Var[Author], book: q.Var[Book]) -> q.Body:
            return Rel.wrote(author, book)  # type: ignore[return-value]


def test_a_union_head_is_not_refuted_by_the_entity_check() -> None:
    """``points_at_shelf``'s two clauses force ``Var[Shelf | Author]``. The check must
    reject what it can prove wrong and accept what it cannot -- rejecting an
    unresolvable declared type would reject valid rules."""
    src = q.Var(Shelf | Author)  # type: ignore[arg-type]
    body = q.all_(Rel.frequents(src, q.var(Shelf)))

    validate_body(body, LIBCAT_SCHEMA, context="test", projected=(src,))


# ---- ADR-0012 D-B: one namespace for kinds and predicate names --------


def test_a_predicate_cannot_shadow_a_relationship_kind() -> None:
    """Band discipline's first line of defence (ADR-0012 D-B). The two used to be
    independent dicts, so a name could be registered as both -- at which point a
    provider could declare it in SUPPLIES_EDGES and band discipline was bypassed by
    shadowing."""

    @q.derived
    def _shadow(a: q.Var[Book], b: q.Var[Book]) -> q.Body:
        return q.all_(Rel.cites(a, b))

    _shadow.id = Rel.cites.qualified_name

    with pytest.raises(SchemaError, match="Conflicting relationship"):
        LIBCAT_SCHEMA.register_predicate(_shadow)


def test_a_registered_predicate_is_reachable_by_its_qualified_name() -> None:
    @q.derived
    def _reachable(a: q.Var[Book], b: q.Var[Book]) -> q.Body:
        return q.all_(Rel.cites(b, a))

    LIBCAT_SCHEMA.register_predicate(_reachable)

    assert LIBCAT_SCHEMA.predicate(_reachable.id) is _reachable
    assert _reachable.id in LIBCAT_SCHEMA.describe()["derived_predicates"]
