"""The memoization DAG: one mechanism, two triggers, four node kinds.

``goals.md`` §4.1's split is the whole design and it is the thing to keep hold of
while reading any of these modules:

- **Invalidation is eager, cheap, event-driven.** It marks. Its cost is bounded
  by the size of the change (``memo/table.py``).
- **Computation is lazy, demand-driven.** Nothing is recomputed until a query
  needs its value (``memo/walk.py``).

The distinction that matters is not extraction-versus-derivation; it is *marking*
versus *computing*. Eager re-extraction on save is a legitimate warming policy,
but it must stay a policy: nothing in the semantics may depend on it having run.

| Module | Answers |
| --- | --- |
| ``digest`` | what "the facts did not change" means, given that provenance must not participate (§4.4) |
| ``node`` | what a node is, and why it stores two revisions (§4.2) |
| ``keys`` | what has to be in a key so it can be invalidated (§4.5, R8) |
| ``table`` | where nodes live, and why invalidation computes nothing (R2) |
| ``walk`` | the four steps, which cutoff each node is entitled to (§4.3, ADR-0027), the two read modes (§4.12) and cancel-and-restart (§4.11) |
"""

from __future__ import annotations

from finecode_knowledge.memo.digest import fact_identity_hash, unit_digest
from finecode_knowledge.memo.keys import extraction_key, query_key
from finecode_knowledge.memo.node import (
    INITIAL_REVISION,
    Materialize,
    MemoNode,
    NodeKey,
    NodeKind,
    Revision,
)
from finecode_knowledge.memo.table import MemoTable
from finecode_knowledge.memo.walk import Cancelled, MemoWalk, Provenance, WalkStats

__all__ = [
    "INITIAL_REVISION",
    "Cancelled",
    "Materialize",
    "MemoNode",
    "MemoTable",
    "MemoWalk",
    "NodeKey",
    "NodeKind",
    "Provenance",
    "Revision",
    "WalkStats",
    "extraction_key",
    "fact_identity_hash",
    "query_key",
    "unit_digest",
]
