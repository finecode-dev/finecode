"""Input fingerprints: capture, persistence, and what an uncaptured bucket means (R11).

`goals.md` §4.8: the dependency graph and input fingerprints are part of the
**persisted** state, because a process restart otherwise loses them and the next
query must either rebuild everything or trust facts it cannot verify. These tests
cover the capture and the round trip; the walk that consumes them is
``model/verify.py`` (see ``test_verify.py``).
"""

from __future__ import annotations

import json
import pathlib

import pytest
from libcat import LIBCAT_SCHEMA, Book, BookFields

from finecode_knowledge.fact_file import read_facts, write_facts
from finecode_knowledge.model.bands import Band
from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.facts import FieldFact, Provenance, RunStamp
from finecode_knowledge.model.fingerprint import capture, resolve
from finecode_knowledge.model.store import FactStore
from finecode_knowledge.model.unit import Unit

PROVIDER = "libcat.catalog_scan"


def _fact(isbn: str) -> FieldFact:
    return FieldFact(
        entity=Book.ref(isbn=isbn),
        field=BookFields.isbn.id,
        value=isbn,
        prov=Provenance(
            band=Band.DECLARED,
            provider=PROVIDER,
            run=RunStamp(id="r", observed_at="t"),
        ),
    )


def _store_with_unit(tmp_path: pathlib.Path) -> tuple[FactStore, Unit]:
    (tmp_path / "shelfA").mkdir()
    source = tmp_path / "shelfA" / "catalog.toml"
    source.write_text("[book]\nisbn = 'a-1'\n")

    store = FactStore(LIBCAT_SCHEMA)
    unit = Unit(
        provider_id=PROVIDER,
        unit_id="shelfA/catalog.toml",
        inputs=("shelfA/catalog.toml",),
    ).captured(tmp_path)
    store.ingest(PROVIDER, [_fact("a-1")], unit=unit)
    return store, unit


def test_capture_records_the_gate_and_the_decider(tmp_path):
    """size + mtime_ns are the cheap gate; sha256 is what actually decides (§4.8, R8)."""
    target = tmp_path / "a.txt"
    target.write_bytes(b"hello")

    fp = capture("a.txt", tmp_path)

    assert fp.path == "a.txt"
    assert fp.size == 5
    assert fp.mtime_ns > 0
    # sha256(b"hello")
    assert (
        fp.sha256 == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


def test_capture_of_a_missing_input_is_loud(tmp_path):
    """A unit may only declare inputs it actually read. Silently dropping one is the
    untracked-but-not-declared state R9 forbids."""
    with pytest.raises(SchemaError, match="Cannot fingerprint declared input"):
        capture("nope.toml", tmp_path)


def test_an_absolute_input_is_not_reinterpreted_against_the_root(tmp_path):
    """ADR-0023 D3's asymmetry: a path that cannot be portable says so by being
    absolute, rather than being silently resolved against a different root."""
    outside = tmp_path / "elsewhere.py"
    assert resolve(str(outside), tmp_path / "ws") == outside
    assert resolve("inside.py", tmp_path / "ws") == tmp_path / "ws" / "inside.py"


def test_fingerprints_survive_the_fact_file_round_trip(tmp_path):
    """The point of R11: a cold start has nothing but the file to compare against."""
    store, unit = _store_with_unit(tmp_path)
    path = tmp_path / "facts.json"
    write_facts(store, path)

    reloaded = read_facts(LIBCAT_SCHEMA, path)

    assert reloaded.unit(PROVIDER, "shelfA/catalog.toml") == unit


def test_a_units_declarations_survive_alongside_its_fingerprints(tmp_path):
    """``untracked`` and ``depends_on`` are as load-bearing as the hashes: a bucket that
    can never be confirmed must say so after a restart too."""
    store = FactStore(LIBCAT_SCHEMA)
    unit = Unit(
        provider_id=PROVIDER,
        unit_id="fused",
        untracked=("resolved config",),
        depends_on=(("libcat.author_index", "resolved-config"),),
    )
    store.ingest(PROVIDER, [_fact("a-1")], unit=unit)
    path = tmp_path / "facts.json"
    write_facts(store, path)

    reloaded = read_facts(LIBCAT_SCHEMA, path)

    restored = reloaded.unit(PROVIDER, "fused")
    assert restored is not None
    assert restored.untracked == ("resolved config",)
    assert restored.depends_on == (("libcat.author_index", "resolved-config"),)


def test_a_pre_r11_fact_file_loads_with_units_that_declare_nothing(tmp_path):
    """A file the previous release wrote carries only provider/unit/count. Refusing to
    load it would turn a stale-but-usable store into no store at all; loading it with an
    empty unit means the bucket has nothing to check and so cannot be confirmed."""
    store, _ = _store_with_unit(tmp_path)
    path = tmp_path / "facts.json"
    write_facts(store, path)

    data = json.loads(path.read_text())
    for bucket in data["buckets"]:
        for widened in ("inputs", "untracked", "depends_on", "fingerprints"):
            bucket.pop(widened)
    path.write_text(json.dumps(data))

    reloaded = read_facts(LIBCAT_SCHEMA, path)

    unit = reloaded.unit(PROVIDER, "shelfA/catalog.toml")
    assert unit is not None
    assert unit.inputs == ()
    assert unit.fingerprints == ()
    assert list(reloaded.entities_of_type(Book.qualified_name())) == [
        Book.ref(isbn="a-1")
    ]


def test_editing_an_input_changes_its_hash_but_an_untouched_one_keeps_it(tmp_path):
    """The comparison the confirmation walk makes, asserted here on the capture side
    alone."""
    (tmp_path / "a.txt").write_text("one")
    (tmp_path / "b.txt").write_text("two")
    before = {
        f.path: f for f in (capture("a.txt", tmp_path), capture("b.txt", tmp_path))
    }

    (tmp_path / "a.txt").write_text("one changed")
    after = {
        f.path: f for f in (capture("a.txt", tmp_path), capture("b.txt", tmp_path))
    }

    assert after["a.txt"].sha256 != before["a.txt"].sha256
    assert after["b.txt"].sha256 == before["b.txt"].sha256
