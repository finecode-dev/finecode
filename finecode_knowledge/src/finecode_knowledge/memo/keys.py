"""Building node keys, and the R8 rows that have to be in them.

§4.5's rule: **an input that is not in the key is an input that cannot
invalidate.** For a query node that means the key must move when any of these
move, and each one is a row R8 lists:

| Row | Where it enters the key |
| --- | --- |
| the question itself | the canonical serialized body and projection |
| rule / derived code version | the version hash of every predicate the body references, resolved transitively through the schema |
| schema version | indirectly: a renamed field changes the literal's qualified name, which is in the body |
| row limit | it changes the answer, so it changes the node |

**The read mode is deliberately not in this list**, and it was until Phase 5.
``Mode`` says *how hard to look*, not *what to look for*: both modes answer the
same question over the same facts, and the only thing that differs is whether the
walk is willing to serve a value it has not re-verified. Keying on it would give
``Mode.CACHED`` its own private cache -- so a cached-mode read could only ever hit
a value some earlier cached-mode read computed, and the memo a verified LSP pass
just filled would be invisible to the agent loop asking for it cheaply. That is
the exact latency §4.12 introduced the mode to pay down, so the mode belongs on
the *serving* decision (``memo/walk.py``) and nowhere near identity.

Recomputation therefore always runs at ``Mode.VERIFIED``: a value that lands in a
node is one both modes may be handed, so it must not have been produced under the
weaker contract.

Source fingerprints, provider identity and resolved config are the *extraction*
node's key, not this one -- they enter a query node's world as dependencies, not
as key components, which is what lets one query node survive an edit that changed
none of its inputs.

**The predicate version hashes are the row that is easy to lose.** A query
carries derived predicates *by name* (§5.9), so editing a predicate's body moves
nothing in the serialized query. Without the transitive version hashes here, a
rewritten predicate would serve the previous one's answer with the query text
untouched -- exactly the defect R8 lists for provider code, one layer up.
"""

from __future__ import annotations

import typing

from finecode_knowledge.memo.node import NodeKey, NodeKind
from finecode_knowledge.model.errors import SchemaError
from finecode_knowledge.model.literal import Conjunction, LiteralKind
from finecode_knowledge.query.ir_wire import (
    TermTable,
    conjunction_to_json,
    term_to_json,
)
from finecode_knowledge.query.terms import Prov
from finecode_knowledge.query.version import hash_of

if typing.TYPE_CHECKING:
    from finecode_knowledge.model.registry import SchemaRegistry
    from finecode_knowledge.model.unit import BucketKey
    from finecode_knowledge.query.query import Query

__all__ = [
    "extraction_key",
    "query_key",
    "query_location_sensitive",
    "referenced_predicates",
]


def extraction_key(bucket: BucketKey) -> NodeKey:
    """One fact bucket's node key -- its ``(provider_id, unit_id)``, tagged."""
    provider_id, unit_id = bucket
    return (NodeKind.EXTRACTION.value, provider_id, unit_id)


def referenced_predicates(
    body: Conjunction, schema: SchemaRegistry, *, _seen: set[str] | None = None
) -> dict[str, str]:
    """Every derived predicate *body* reaches, mapped to its version hash.

    **Transitive**, because a predicate that calls another inherits that one's
    code as an input: editing the inner body changes the outer's answer without
    changing the outer's own IR. Following one hop would leave exactly that case
    silently memoized.

    Cycles terminate on ``_seen``: a self-recursive predicate (``reaches``) is a
    legitimate definition, and its version hash is its own -- visiting it twice
    would not add information and would not return.

    A predicate the registry does not hold is **skipped rather than raised on**.
    The registry is the authority on resolution and the interpreter will fail on
    it in a moment with a message that names the call site; failing here would
    replace that with a memo-layer error about a key.
    """
    seen = _seen if _seen is not None else set()
    versions: dict[str, str] = {}
    for literal in body.literals:
        if literal.kind is not LiteralKind.DERIVED or literal.predicate in seen:
            continue
        seen.add(literal.predicate)
        try:
            predicate = schema.predicate(literal.predicate)
        except SchemaError:
            continue
        versions[literal.predicate] = predicate.version_hash
        for clause in predicate.predicate.clauses:
            versions.update(referenced_predicates(clause.body, schema, _seen=seen))
    return versions


def query_location_sensitive(query: Query, schema: SchemaRegistry) -> bool:
    """ADR-0027 D3's test, applied to a query's projection.

    A query's projection *is* its head: it is exactly what a consumer receives, so
    "can this node's value contain a location" is "does a ``Prov`` term reach the
    projection". D3's locality argument transfers unchanged -- a body that reads a
    sensitive predicate without projecting its ``Prov`` cannot carry a location.

    The schema is taken and unused on purpose: an earlier draft classified by
    walking into referenced predicates, which is the propagating analysis D3
    rejects for being one conservative step away from demoting everything. The
    parameter stays so the call sites do not have to change if a future node kind
    genuinely needs it.
    """
    del schema
    return any(isinstance(term, Prov) for term in query.projection)


def query_key(query: Query, schema: SchemaRegistry, *, limit: int | None) -> NodeKey:
    """The key for one whole query, with R8's code-version rows folded in.

    Canonical by construction: ``ir_wire`` names variables by first appearance,
    so two structurally identical queries built from different ``Var`` objects
    produce the same key. Without that the memo would miss on every re-import of
    a rule module -- the failure mode that makes a memo look like it works while
    never hitting.

    The read mode is **not** a component; see the module docstring for why.
    """
    table = TermTable()
    projection = [term_to_json(term, table) for term in query.projection]
    body = conjunction_to_json(query.body, table)
    versions = referenced_predicates(query.body, schema)
    fingerprint = hash_of(
        {
            "projection": projection,
            "body": body,
            "predicates": sorted(versions.items()),
        }
    )
    return (NodeKind.QUERY.value, fingerprint, limit)
