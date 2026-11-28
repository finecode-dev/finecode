"""The output-side cutoff value: a per-fact content hash, combined per unit (§4.4).

``goals.md`` §4.4 needs two hash layers and this is the second. The *input* side
-- the mtime-gated file fingerprint (R11) -- cuts off when a source did not change
at all. It cannot help with the case that motivates early cutoff: **a source that
changed but produced identical facts.** Add a docstring to a Python file and the
file's hash moves, so extraction must re-run; the facts it emits are
byte-identical, and only comparing *those* stops the change propagating.

Without that comparison, one edit anywhere invalidates everything transitively
reachable, and §4.4's verdict applies: incrementality without early cutoff is
mostly theatre.

## Two traps, both load-bearing

**Provenance must be excluded.** Every fact carries a ``RunStamp`` minted per
extraction (``model/facts.py``). If it participated, *every* re-extraction would
produce "different" facts, cutoff would never fire again for any node, and the
mechanism would silently degrade to full downstream invalidation. This is why
``FieldFact.prov`` and ``EdgeFact.prov`` are ``compare=False``, and ADR-0027 D2
closes rather than defers the question of putting them back.

**The combination must be order-independent.** C7 promises the same fact *set*,
not the same emission order, so a hash of the concatenation would report a change
whenever a provider iterated a dict differently. Plain XOR is also wrong -- and
wrongly tempting: it is order-independent, but identical facts cancel, so a unit
emitting a fact twice hashes the same as one emitting it zero times. §4.4 names
the two acceptable forms; this uses sorted-hash-concat, which is a multiset hash
because sorting keeps duplicates.

## What this is *not* sufficient for

The digest compares fact **identity**, and ADR-0018 lets a consumer observe more
than identity -- a violation lifts ``prov.location`` straight out of a head
binding. So a fact re-emitted from a different line hashes equal here, and a node
whose value can contain a location must **not** cut off on this value. That is
ADR-0027 D4, and it is enforced in ``memo/walk.py``, not here: this module
computes the digest correctly for the nodes entitled to use it.
"""

from __future__ import annotations

import hashlib
import json
import typing

from finecode_knowledge.model.facts import EdgeFact, Emission, FieldFact
from finecode_knowledge.model.wire import ref_to_json

__all__ = ["EMPTY_DIGEST", "fact_identity", "fact_identity_hash", "unit_digest"]

_DIGEST_LENGTH = 32


def fact_identity(fact: Emission) -> dict:
    """*fact* reduced to what makes it that fact -- provenance excluded (R5/C8).

    Deliberately not ``dataclasses.asdict`` minus a key: the exclusion is the
    whole point of the function, so it is spelled by listing what is *in* rather
    than by removing what is out. A field added to ``FieldFact`` tomorrow is then
    absent until someone decides it belongs, which is the safe direction -- a
    forgotten inclusion over-cuts and is caught by a test, a forgotten exclusion
    under-cuts and is silent.
    """
    if isinstance(fact, FieldFact):
        return {
            "f": "field",
            "e": ref_to_json(fact.entity),
            "n": fact.field,
            "v": fact.value,
        }
    if isinstance(fact, EdgeFact):
        return {
            "f": "edge",
            "k": fact.kind,
            "s": ref_to_json(fact.src),
            "d": ref_to_json(fact.dst),
        }
    raise TypeError(f"Not a fact: {type(fact).__name__}")


def fact_identity_hash(fact: Emission) -> str:
    """The primitive §4.4 builds both cutoff granularities on.

    Required for identity and merge anyway (R5, C8), which is what dissolves the
    "fact set vs unit hash" choice: a set-diff *needs* per-fact hashes, and once
    they exist the unit digest is just their order-independent combination. The
    two are one mechanism at two granularities, not alternatives -- so the
    fact-level refinement §4.4 defers costs no new primitive when it lands.
    """
    encoded = json.dumps(
        fact_identity(fact), sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(encoded.encode()).hexdigest()[:_DIGEST_LENGTH]


def unit_digest(facts: typing.Iterable[Emission]) -> str:
    """One bucket's facts as a single comparable value.

    Sorted-hash-concat: order-independent because it sorts, a *multiset* hash
    because sorting keeps duplicates. See the module docstring for why plain XOR
    is not an option.
    """
    combined = hashlib.sha256()
    for digest in sorted(fact_identity_hash(fact) for fact in facts):
        combined.update(digest.encode())
        combined.update(b"\n")
    return combined.hexdigest()[:_DIGEST_LENGTH]


EMPTY_DIGEST = unit_digest(())
"""The digest of a bucket with no facts.

A real value rather than ``None``: a unit that re-extracted to nothing and a unit
that was never extracted are different situations, and only the first should cut
off. Giving "nothing" a digest keeps that distinction expressible.
"""
