"""The cold-start confirmation walk (R11, ADR-0022, ADR-0024 D2).

The stat-gate matrix is the substance: unchanged, touched-but-not-modified,
modified, deleted. The second case is the one a plausible implementation gets
wrong -- comparing mtime alone false-invalidates on every checkout and clone
(R8 is explicit that the hash, not the mtime, is the input).

Written against ``libcat`` (``tests/fixtures/libcat``): the walk reads units and
the filesystem and never consults the schema, so the schema here is scaffolding
-- which is exactly why it should not be one specific tool's (R18/R19).
"""

from __future__ import annotations

import os
import pathlib

from libcat import LIBCAT_SCHEMA, Author, AuthorFields, Book, BookFields

from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.facts import FieldFact, Provenance, RunStamp
from finecode_knowledge.model.store import FactStore
from finecode_knowledge.model.unit import Unit
from finecode_knowledge.model.verify import Verdict, verify_inputs

PROVIDER = "libcat.catalog_scan"
OTHER = "libcat.author_index"


def _prov(provider: str) -> Provenance:
    return Provenance(
        band=Band.DECLARED, provider=provider, run=RunStamp(id="r", observed_at="t")
    )


def _fact(isbn: str) -> FieldFact:
    return FieldFact(
        entity=Book.ref(isbn=isbn),
        field=BookFields.isbn.id,
        value=isbn,
        prov=_prov(PROVIDER),
    )


def _other_fact(handle: str) -> FieldFact:
    """``OTHER`` is ``author_index``, which supplies Author fields rather than Book ones."""
    return FieldFact(
        entity=Author.ref(handle=handle),
        field=AuthorFields.handle.id,
        value=handle,
        prov=_prov(OTHER),
    )


def _store(tmp_path: pathlib.Path, **unit_kwargs) -> tuple[FactStore, pathlib.Path]:
    source = tmp_path / "a.toml"
    source.write_text("one")
    store = FactStore(LIBCAT_SCHEMA)
    unit = Unit(
        provider_id=PROVIDER, unit_id="a.toml", inputs=("a.toml",), **unit_kwargs
    ).captured(tmp_path)
    store.ingest(PROVIDER, [_fact("a-1")], unit=unit)
    return store, source


def test_an_unchanged_tree_confirms_and_reads_nothing(tmp_path):
    """Criterion 7: cold start is stat-bound. One stat per input, zero file reads."""
    store, _ = _store(tmp_path)

    report = verify_inputs(store, tmp_path)

    assert report[(PROVIDER, "a.toml")].kind is Verdict.CONFIRMED
    assert report.stat_count == 1
    assert report.hash_count == 0


def test_touching_a_file_without_changing_it_still_confirms(tmp_path):
    """Criterion 2, and the trap: mtime moves on checkout, clone and rebase with
    identical content. A verdict resting on mtime alone false-invalidates constantly,
    so the gate trips but the hash decides -- and the hash says unchanged."""
    store, source = _store(tmp_path)
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns + 10**9, stat.st_mtime_ns + 10**9))

    report = verify_inputs(store, tmp_path)

    assert report[(PROVIDER, "a.toml")].kind is Verdict.CONFIRMED
    assert report.hash_count == 1, "the gate must trip and the content must be read"


def test_editing_a_file_is_stale(tmp_path):
    store, source = _store(tmp_path)
    source.write_text("one changed")

    verdict = verify_inputs(store, tmp_path)[(PROVIDER, "a.toml")]

    assert verdict.kind is Verdict.STALE
    assert "a.toml" in verdict.detail


def test_deleting_a_file_is_missing_not_stale(tmp_path):
    """§4.7 keeps retraction a separate mechanism: a deleted source is what a future
    retraction acts on, and a query must not mutate the store as a side effect."""
    store, source = _store(tmp_path)
    source.unlink()

    verdict = verify_inputs(store, tmp_path)[(PROVIDER, "a.toml")]

    assert verdict.kind is Verdict.MISSING


def test_a_declared_untracked_input_never_confirms(tmp_path):
    """R9: a provider reading through an in-process API has an input no fingerprint
    covers. Re-extracting will not clear this, which is why it ranks below STALE in
    precedence."""
    store, _ = _store(tmp_path, untracked=("resolved config",))

    verdict = verify_inputs(store, tmp_path)[(PROVIDER, "a.toml")]

    assert verdict.kind is Verdict.UNTRACKED
    assert "resolved config" in verdict.detail


def test_a_stale_input_outranks_a_declared_untracked_one(tmp_path):
    """A unit can be both. STALE is reported because it is the one a caller can act
    on -- re-extraction fixes it, and leaves the untracked part where it was."""
    store, source = _store(tmp_path, untracked=("resolved config",))
    source.write_text("changed")

    assert verify_inputs(store, tmp_path)[(PROVIDER, "a.toml")].kind is Verdict.STALE


def test_a_unit_without_fingerprints_cannot_be_confirmed(tmp_path):
    """An uncaptured unit and a pre-R11 fact file are the same situation: nothing
    links the facts to their sources, which is what ADR-0014 D7 called untracked."""
    store = FactStore(LIBCAT_SCHEMA)
    store.ingest(
        PROVIDER, [_fact("a-1")], unit=Unit(PROVIDER, "a.toml", inputs=("a.toml",))
    )

    assert (
        verify_inputs(store, tmp_path)[(PROVIDER, "a.toml")].kind is Verdict.UNTRACKED
    )


def test_editing_provider_code_marks_its_unit_stale(tmp_path):
    """Criterion 8 / ADR-0023: the fact file is cached extraction, so a rewritten
    extractor serves facts from the old one with every scanned file untouched."""
    provider_code = tmp_path / "provider.py"
    provider_code.write_text("# v1")
    (tmp_path / "a.toml").write_text("one")
    store = FactStore(LIBCAT_SCHEMA)
    store.ingest(
        PROVIDER,
        [_fact("a-1")],
        unit=Unit(PROVIDER, "a.toml", inputs=("a.toml", "provider.py")).captured(
            tmp_path
        ),
    )

    provider_code.write_text("# v2 -- rewritten")

    verdict = verify_inputs(store, tmp_path)[(PROVIDER, "a.toml")]
    assert verdict.kind is Verdict.STALE
    assert "provider.py" in verdict.detail


def test_editing_a_schema_module_marks_the_units_supplying_from_it_stale(tmp_path):
    """Criterion 9 / ADR-0026: rename a field and stored facts keep the old qualified
    name while every scanned file stays byte-identical."""
    schema_module = tmp_path / "schema.py"
    schema_module.write_text("name = 'v1'")
    (tmp_path / "a.toml").write_text("one")
    (tmp_path / "b.toml").write_text("two")
    store = FactStore(LIBCAT_SCHEMA)
    store.ingest(
        PROVIDER,
        [_fact("a-1")],
        unit=Unit(PROVIDER, "a.toml", inputs=("a.toml", "schema.py")).captured(
            tmp_path
        ),
    )
    store.ingest(
        OTHER,
        [_other_fact("ana")],
        unit=Unit(OTHER, "b.toml", inputs=("b.toml",)).captured(tmp_path),
    )

    schema_module.write_text("name = 'v2'")

    report = verify_inputs(store, tmp_path)
    assert report[(PROVIDER, "a.toml")].kind is Verdict.STALE
    assert report[(OTHER, "b.toml")].kind is Verdict.CONFIRMED, "and only those"


def test_a_unit_inherits_its_dependency_s_verdict(tmp_path):
    """ADR-0024 D2: a provider whose edges are bound against another provider's bucket
    depends on that bucket, which no file fingerprint expresses. One hop, checked
    rather than walked."""
    (tmp_path / "a.toml").write_text("one")
    (tmp_path / "b.toml").write_text("two")
    store = FactStore(LIBCAT_SCHEMA)
    store.ingest(
        OTHER,
        [_other_fact("ana")],
        unit=Unit(
            OTHER, "fused", inputs=("b.toml",), untracked=("resolved config",)
        ).captured(tmp_path),
    )
    store.ingest(
        PROVIDER,
        [_fact("a-1")],
        unit=Unit(
            PROVIDER, "calls", inputs=("a.toml",), depends_on=((OTHER, "fused"),)
        ).captured(tmp_path),
    )

    report = verify_inputs(store, tmp_path)

    assert report[(OTHER, "fused")].kind is Verdict.UNTRACKED
    depending = report[(PROVIDER, "calls")]
    assert depending.kind is Verdict.UNTRACKED
    assert "fused" in depending.detail


def test_a_unit_stale_on_its_own_inputs_keeps_that_reason(tmp_path):
    """Inheritance applies only to a unit that would otherwise be confirmed: its own
    stale input is the reason a caller can act on."""
    (tmp_path / "a.toml").write_text("one")
    (tmp_path / "b.toml").write_text("two")
    store = FactStore(LIBCAT_SCHEMA)
    store.ingest(
        OTHER,
        [_other_fact("ana")],
        unit=Unit(OTHER, "fused", inputs=("b.toml",), untracked=("cfg",)).captured(
            tmp_path
        ),
    )
    store.ingest(
        PROVIDER,
        [_fact("a-1")],
        unit=Unit(
            PROVIDER, "calls", inputs=("a.toml",), depends_on=((OTHER, "fused"),)
        ).captured(tmp_path),
    )
    (tmp_path / "a.toml").write_text("changed")

    assert verify_inputs(store, tmp_path)[(PROVIDER, "calls")].kind is Verdict.STALE


def test_a_dependency_absent_from_the_store_is_untracked(tmp_path):
    """Claiming confirmation over a bucket that was never ingested is exactly the
    false-clean verdict this walk exists to prevent."""
    (tmp_path / "a.toml").write_text("one")
    store = FactStore(LIBCAT_SCHEMA)
    store.ingest(
        PROVIDER,
        [_fact("a-1")],
        unit=Unit(
            PROVIDER,
            "calls",
            inputs=("a.toml",),
            depends_on=((OTHER, "never-ingested"),),
        ).captured(tmp_path),
    )

    verdict = verify_inputs(store, tmp_path)[(PROVIDER, "calls")]

    assert verdict.kind is Verdict.UNTRACKED
    assert "not in the store" in verdict.detail
