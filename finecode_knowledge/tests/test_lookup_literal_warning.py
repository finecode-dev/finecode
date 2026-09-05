"""A field literal that exists only to bind provenance for the head (ADR-0028).

The defect this closes: in a conjunction every literal is a filter, so retrieving
a value and requiring it are the same act. A literal added to fetch something the
head *reports* narrows the rule's extension -- toward **under-reporting**, which
is the direction ADR-0013 D4.3 and ADR-0002 C3 refuse.

Concretely, two shipped rules bound ``expected_in`` by adding
``ProjectFields.def_path(subject, _, at=expected_in)``, and a project with no
``def_path`` fact then produced no finding at all -- every real condition held.

This is the **mirror** of ``UnconstrainedKeyBindingWarning``
(``tests/test_key_literal.py``): that one catches a body that never requires an
entity to exist, this one a body that requires a fact to exist by accident.

Written against ``libcat`` (``tests/fixtures/libcat``), like every other engine
test -- the engine must not know FineCode's vocabulary (R18/R19).
"""

from __future__ import annotations

import pytest
from libcat import LIBCAT_SCHEMA, Author, Book, BookFields, Rel

from finecode_knowledge import query as q
from finecode_knowledge.query.validate import LookupLiteralWarning, validate_body


def _lookup_warnings(recwarn: pytest.WarningsRecorder) -> list:
    return [w for w in recwarn if issubclass(w.category, LookupLiteralWarning)]


def test_a_field_literal_bound_only_to_a_head_prov_warns() -> None:
    """The ADR-0028 shape: `title` is required to exist purely so `at` can bind.

    `book` is already bound by the edge, and the value term is a throwaway
    variable nobody reads -- so removing the literal would leave every variable
    still bound, and its whole contribution to the answer is an existence test.
    """
    author, book, at = q.var(Author), q.var(Book), q.Prov()
    body = q.all_(
        Rel.wrote(author, book),
        BookFields.title(book, q.var(str), at=at),
    )

    with pytest.warns(LookupLiteralWarning, match="provenance can bind a head parameter"):
        validate_body(body, LIBCAT_SCHEMA, context="test", projected=(author, book, at))


def test_a_prov_riding_on_a_literal_the_rule_needs_does_not_warn(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """`asserted_at`'s shape, and the rule the warning enforces.

    Binding provenance off an edge the body needs anyway adds no filter -- the
    edge was already required. That is the legitimate half of ADR-0028.
    """
    author, book, at = q.var(Author), q.var(Book), q.Prov()
    body = q.all_(Rel.wrote(author, book, at=at))

    validate_body(body, LIBCAT_SCHEMA, context="test", projected=(author, book, at))

    assert not _lookup_warnings(recwarn)


def test_a_field_literal_whose_value_the_body_reads_does_not_warn(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """A value another literal consumes is a real join, not a throwaway."""
    author, book, title, at = q.var(Author), q.var(Book), q.var(str), q.Prov()
    other = q.var(Book)
    body = q.all_(
        Rel.wrote(author, book),
        BookFields.title(book, title, at=at),
        BookFields.label(other, title),
    )

    validate_body(body, LIBCAT_SCHEMA, context="test", projected=(author, book, at))

    assert not _lookup_warnings(recwarn)


def test_a_field_literal_whose_value_is_projected_does_not_warn(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """`locations.project_locations`' shape: the caller wants the value itself."""
    book, title, at = q.var(Book), q.var(str), q.Prov()
    body = q.all_(BookFields.title(book, title, at=at))

    validate_body(body, LIBCAT_SCHEMA, context="test", projected=(book, title, at))

    assert not _lookup_warnings(recwarn)


def test_a_field_literal_with_no_provenance_binding_does_not_warn(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """Without `at=` the existence test is the literal's whole point.

    "Books that were actually scanned" is a thing a rule may legitimately ask,
    and ADR-0019 D4 keeps the field spelling available precisely to ask it.
    """
    author, book = q.var(Author), q.var(Book)
    body = q.all_(Rel.wrote(author, book), BookFields.title(book, q.var(str)))

    validate_body(body, LIBCAT_SCHEMA, context="test", projected=(author, book))

    assert not _lookup_warnings(recwarn)


def test_a_field_literal_that_alone_binds_its_entity_does_not_warn(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """Load-bearing: drop this literal and `book` is unbound, so it is not a lookup."""
    book, at = q.var(Book), q.Prov()
    body = q.all_(BookFields.title(book, q.var(str), at=at))

    validate_body(body, LIBCAT_SCHEMA, context="test", projected=(book, at))

    assert not _lookup_warnings(recwarn)


def test_a_prov_that_is_not_in_the_head_does_not_warn(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """The warning is about feeding the *head*. A body-internal `at` reports nothing."""
    author, book, at = q.var(Author), q.var(Book), q.Prov()
    body = q.all_(
        Rel.wrote(author, book),
        BookFields.title(book, q.var(str), at=at),
    )

    validate_body(body, LIBCAT_SCHEMA, context="test", projected=(author, book))

    assert not _lookup_warnings(recwarn)
