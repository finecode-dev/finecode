"""The output-side cutoff value (§4.4).

Three properties, and each one is a trap rather than a nicety: exclude
provenance or cutoff never fires again; combine order-dependently and it fires
on nothing; use XOR and duplicates cancel. The tests are named after the failure
each prevents.
"""

from __future__ import annotations

from libcat import Author, Book

from finecode_knowledge.memo.digest import (
    EMPTY_DIGEST,
    fact_identity,
    fact_identity_hash,
    unit_digest,
)
from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.facts import (
    EdgeFact,
    FieldFact,
    Provenance,
    RunStamp,
    SourceLoc,
)

PROVIDER = "libcat.catalog_scan"


def _prov(*, run: str = "r-1", line: int = 1) -> Provenance:
    return Provenance(
        band=Band.DECLARED,
        provider=PROVIDER,
        run=RunStamp(id=run, observed_at="t"),
        location=SourceLoc(project="p", file="catalog.toml", line=line),
    )


def _isbn(value: str = "a-1", **prov_kwargs) -> FieldFact:
    return FieldFact(
        entity=Book.ref(isbn=value),
        field="isbn",
        value=value,
        prov=_prov(**prov_kwargs),
    )


def _wrote(**prov_kwargs) -> EdgeFact:
    return EdgeFact(
        kind="wrote",
        src=Author.ref(handle="ana"),
        dst=Book.ref(isbn="a-1"),
        prov=_prov(**prov_kwargs),
    )


# ---- provenance must not participate -----------------------------------


def test_a_new_run_stamp_does_not_change_a_facts_hash() -> None:
    """**The trap that would disable the feature entirely.** ``RunStamp`` is minted
    per extraction, so if it participated every re-extraction would produce
    "different" facts, cutoff would never fire for any node again, and the whole
    mechanism would degrade to full downstream invalidation -- silently."""
    assert fact_identity_hash(_isbn(run="r-1")) == fact_identity_hash(_isbn(run="r-2"))


def test_a_moved_line_does_not_change_a_facts_hash() -> None:
    """Consistent with ``prov`` being ``compare=False`` (R5/C8). This is *correct*
    for the digest and *insufficient* on its own, which is why ADR-0027 gives
    location-sensitive nodes a different cut rather than changing this."""
    assert fact_identity_hash(_wrote(line=4)) == fact_identity_hash(_wrote(line=99))


def test_fact_identity_lists_what_is_in_rather_than_removing_what_is_out() -> None:
    """A field added to ``FieldFact`` tomorrow is absent until someone decides it
    belongs. A forgotten inclusion over-cuts and a test catches it; a forgotten
    exclusion under-cuts and nothing does."""
    assert set(fact_identity(_isbn())) == {"f", "e", "n", "v"}
    assert set(fact_identity(_wrote())) == {"f", "k", "s", "d"}


# ---- what does change it -----------------------------------------------


def test_a_different_value_changes_the_hash() -> None:
    assert fact_identity_hash(_isbn("a-1")) != fact_identity_hash(_isbn("b-9"))


def test_a_field_fact_and_an_edge_fact_never_collide() -> None:
    """They are discriminated in the encoding, not left to differ by luck of
    field names."""
    assert fact_identity_hash(_isbn()) != fact_identity_hash(_wrote())


# ---- the combination ----------------------------------------------------


def test_the_digest_is_order_independent() -> None:
    """C7 promises the same fact *set*, not the same emission order, so a hash of
    the concatenation would report a change whenever a provider iterated a dict
    differently."""
    assert unit_digest([_isbn(), _wrote()]) == unit_digest([_wrote(), _isbn()])


def test_a_repeated_fact_does_not_cancel() -> None:
    """Why plain XOR is rejected despite being order-independent: under it,
    emitting a fact twice hashes the same as emitting it zero times."""
    assert unit_digest([_isbn(), _isbn()]) != unit_digest([])
    assert unit_digest([_isbn(), _isbn()]) != unit_digest([_isbn()])


def test_adding_a_fact_changes_the_digest() -> None:
    assert unit_digest([_isbn()]) != unit_digest([_isbn(), _wrote()])


def test_re_extracting_the_same_facts_gives_the_same_digest() -> None:
    """The property the whole cutoff rests on, stated directly: same facts, new
    run, same digest."""
    first = [_isbn(run="r-1"), _wrote(run="r-1", line=4)]
    second = [_wrote(run="r-2", line=99), _isbn(run="r-2")]

    assert unit_digest(first) == unit_digest(second)


def test_an_empty_bucket_has_a_digest_rather_than_none() -> None:
    """A unit that re-extracted to nothing and a unit that was never extracted are
    different situations, and only the first may cut off."""
    assert unit_digest([]) == EMPTY_DIGEST
    assert EMPTY_DIGEST
