"""What a node is: its key, its two revisions, and what it may cut off on.

``goals.md`` §4.2 is the whole shape of this module. Every node stores **two**
revisions, and their being two different numbers is the trick the rest of the
memo depends on:

- ``verified_at`` -- when this node's value was last confirmed current.
- ``changed_at`` -- when this node's value last actually *differed*.

Step 3 of the walk (§4.3) reads them together: if every dependency's
``changed_at`` is at or below this node's ``verified_at``, nothing it read has
actually changed, so it is verified without being recomputed. Collapsing the two
into one number is exactly how a memo degrades into a timestamp cache that
recomputes on every edit.

## Why the rule-shaped kind is called ``QUERY``

The obvious four kinds are Extraction, Derived predicate, Rule and Projection.
Three survive contact with §4.10's process split; **Rule does not, and the
reason is worth stating rather than papering over.**

A rule's *code* lives in the ER (Q3b) and what crosses to the executing side is
the **serialized query it compiles to** (FR9). The WM therefore never sees a
rule id, and a node kind keyed by one would be a key the WM cannot construct.
What it can construct -- and what ``memo/keys.py`` builds -- carries everything
that identifies the rule anyway: the body IR (which *is* the rule's version),
the projection (its params), and the version hash of every derived predicate the
body references. Only the *name* is missing, and a name is not a memo-key input.

So the kind is ``QUERY``, and "rule" is the authoring-side name for the same
thing. A projection node -- whose value is built by arbitrary Python and so is
location-sensitive unconditionally (ADR-0027 D3) -- keeps its own kind because
it is genuinely not a query.

## MATERIALIZE

``goals.md`` §4.13 assigns this to "the memo-DAG work, which creates the node type
it hangs on"; this is that node type. It is a **per-node policy value**, set where
the node is constructed, and it appears in neither the key nor the version hash
(ADR-0012 D-C): retuning a cache policy must not invalidate correct entries.
"""

from __future__ import annotations

import dataclasses
import enum
import typing

if typing.TYPE_CHECKING:
    from finecode_knowledge.query.footprint import AccessKey

__all__ = [
    "FIRST_REVISION",
    "INITIAL_REVISION",
    "Materialize",
    "MemoNode",
    "NodeKey",
    "NodeKind",
    "Revision",
]

Revision = int
"""The memo DAG's revision counter -- a monotonically increasing integer (§4.2).

Deliberately **not** ``model.fact_source.Revision``, which is a content digest
over the facts served (ADR-0013 D6.2). The two answer different questions: the
digest says *which facts*, the counter says *when we last looked*. Steps 3 and 4
of the walk compare revisions with ``<=``, which a digest cannot support.
"""

INITIAL_REVISION: Revision = 0
"""**Never** -- never verified, never changed. Not a revision the table ever holds.

A node is born here, and the table starts at ``FIRST_REVISION``, so
``verified_at == revision`` is false for a node that has not run. That gap is
load-bearing rather than cosmetic: with the counter starting at zero too, step 1
would serve a brand-new node as though it were verified, returning its empty
value and never computing anything. The bug is silent -- the answer is a
plausible empty result -- which is why the two constants are separated here
rather than left to arithmetic at the call sites.
"""

FIRST_REVISION: Revision = 1
"""Where the counter starts. See ``INITIAL_REVISION`` for why it is not zero."""


class NodeKind(enum.Enum):
    """The kinds of thing the DAG memoizes. See the module docstring on ``QUERY``."""

    EXTRACTION = "extraction"
    """One ``(provider_id, unit_id)`` bucket. Its cut is the input fingerprints
    (R11) on the way in and the unit digest (§4.4) on the way out."""
    DERIVED = "derived"
    """A derived predicate at a bound-argument pattern. The pattern is part of
    the key because a predicate called with different bindings is a different
    question with a different footprint; keying on the predicate alone would
    memoize one call's answer for another's."""
    QUERY = "query"
    """A whole query -- what a rule looks like once it has crossed (see above)."""
    PROJECTION = "projection"
    """A value built by arbitrary Python. Location-sensitive unconditionally,
    because sensitivity is not decidable from IR that does not exist."""


NodeKey = tuple[object, ...]
"""A node's identity, as a value.

A tuple rather than a dataclass because it is used as a dict key several million
times more often than it is read, and because "identity is a value" is the
property this needs -- two nodes are the same node exactly when their keys
compare equal, with nothing else consulted.
"""


class Materialize(enum.Enum):
    """Per-node cache policy (§4.13).

    Not in the key and not in the version hash. A node's policy says what to do
    with its value, not what its value *is*.
    """

    MEMOIZE = "memoize"
    """Default: keep the value and serve it when the walk says it is current."""
    RECOMPUTE = "recompute"
    """Never serve a memoized value. For a node whose value is cheap to produce
    and expensive to hold, or one whose inputs are declared untracked (R9) and so
    can never be verified in step 3 anyway."""


@dataclasses.dataclass
class MemoNode:
    """One node: what it is, when it was last checked, and what it read."""

    key: NodeKey
    kind: NodeKind
    verified_at: Revision = INITIAL_REVISION
    changed_at: Revision = INITIAL_REVISION
    value: object = None
    has_value: bool = False
    """Whether ``value`` has ever been computed.

    Separate from ``value is None`` because ``None`` is a legitimate memoized
    value and an empty row list is a legitimate answer. A node that has never run
    and a node that ran and found nothing must not be confused -- the second may
    be served, the first may not."""

    footprint: tuple[AccessKey, ...] = ()
    """The slots this node's last execution consulted (ADR-0013 D5).

    Recorded per *access*, never per returned row, which is what makes an empty
    scan depend on the bucket that might have filled it. The memo layer is the
    consumer the collector was always for."""

    depends_on: tuple[NodeKey, ...] = ()
    """The nodes step 2 recurses into. Derived from the footprint by attribution,
    which is static and over-approximating by construction: a spurious
    recomputation is acceptable, a missed one is not."""

    input_changed_at: Revision = INITIAL_REVISION
    """Extraction nodes only: when this bucket's *inputs* last moved (ADR-0027 D4).

    A second change revision, because there are two cut strengths. ``changed_at``
    moves when the bucket's facts differ; this moves whenever its sources
    changed at all, whether or not the facts did. A **location-sensitive**
    dependent reads this one and so recomputes across an edit that moved a line
    without changing a fact; an insensitive dependent reads ``changed_at`` and
    keeps §4.4's cutoff.

    Collapsing the two would force a choice between a stale line number in every
    memoized violation and no early cutoff anywhere."""

    digest: str | None = None
    """The output-side cutoff value, when this node has one.

    ``None`` for a node whose value has no digest -- and, importantly, for one
    that has never computed. A node with no digest cannot cut off on a digest,
    which is the correct behaviour rather than a missing case."""

    location_sensitive: bool = False
    """ADR-0027 D3. A sensitive node cuts off on the bucket verdict alone; an
    insensitive one may additionally cut off on the digest."""

    materialize: Materialize = Materialize.MEMOIZE

    def verified(self, revision: Revision) -> MemoNode:
        """Record that this node's value is current as of *revision*. Step 3/4."""
        self.verified_at = revision
        return self

    def changed(self, revision: Revision) -> MemoNode:
        """Record that this node's value actually differed. Step 4's second half.

        Called **only** on a real difference. Calling it unconditionally is the
        single change that would disable early cutoff everywhere while leaving
        every test that checks answers passing.
        """
        self.changed_at = revision
        self.verified_at = revision
        return self

    @property
    def servable(self) -> bool:
        """Whether a memoized value exists and policy permits serving it."""
        return self.has_value and self.materialize is Materialize.MEMOIZE

    def __repr__(self) -> str:
        return (
            f"MemoNode({self.kind.value}, verified_at={self.verified_at}, "
            f"changed_at={self.changed_at}, deps={len(self.depends_on)})"
        )
