"""R8's missing memo-key row: the *code version* of a rule or derived predicate.

``goals.md`` §4.5 lists what a memo key must contain. Source fingerprints (R11),
provider identity (ADR-0023), resolved config, and schema version (ADR-0026) all
exist. **Rule / derived-relation code version did not**, and its absence is the
same defect as a missing provider version one layer up: rewrite a rule body and a
memo would serve the previous rule's answer, with every source file untouched.

## What the hash is over

The **body IR**, encoded canonically by ``query/ir_wire.py``. Not the Python
source: two rules whose bodies differ only in a comment or a local variable name
compile to identical IR and *are* the same rule as far as any answer is
concerned. Hashing source text would invalidate on formatting.

## What is deliberately excluded, and the one thing that is not

A derived predicate's **head parameter names** are excluded. They are
documentation -- the engine unifies positionally, and ``clause_to_json`` names
head terms ``_0``, ``_1``, ... in order -- so renaming one changes nothing about
what the predicate computes.

A **rule's** head parameter names are *included*, and that is not an
inconsistency. A rule's head names are not documentation: ``rule.py`` maps them
onto ``Violation``'s fields by name, so swapping ``subject`` and ``missing``
produces different violations from identical rows. The plan's property -- "a
semantically irrelevant change does not move the hash" -- holds in both cases;
the two differ in *which* changes are semantically irrelevant. A rule's
``message`` template is in for the same reason: it is user-visible output built
from the same bindings.

## Why ``MATERIALIZE`` is not in here

ADR-0012 D-C reasoned about "a definition whose body is hashed as the R8 rule
version" before that hash existed. This is it, and the reasoning lands: a cache
policy is not part of what a node computes, so retuning one must not invalidate
correct entries. ``MATERIALIZE`` is a per-node policy value set where the node is
constructed, and appears in neither the key nor this hash.
"""

from __future__ import annotations

import hashlib
import json
import typing

from finecode_knowledge.query.ir_wire import clause_to_json, conjunction_to_json

if typing.TYPE_CHECKING:
    from finecode_knowledge.model.literal import Clause, Conjunction

__all__ = [
    "VERSION_HASH_LENGTH",
    "body_version_hash",
    "clauses_version_hash",
    "hash_of",
]

VERSION_HASH_LENGTH = 16
"""How much of the sha256 a version hash keeps.

Sixteen hex characters -- 64 bits. The hash is a memo-key component compared for
equality inside one process, never a security boundary and never persisted
across trust domains, and a full 64-character digest in every node key makes the
table's contents unreadable when something goes wrong. Collisions at this width
need ~2^32 distinct rule bodies in one workspace.
"""


def hash_of(payload: object) -> str:
    """A stable hash over any JSON-encodable canonical form.

    ``sort_keys`` matters: the encoders build dicts in a fixed order today, and a
    hash that silently depended on that would move the day one of them grew a
    field in a different place.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()[:VERSION_HASH_LENGTH]


def body_version_hash(
    body: Conjunction, *, head: tuple[str, ...] = (), extra: object = None
) -> str:
    """The version of a single-body definition -- a ``Rule``.

    *head* is the ordered head parameter **names**, included for a rule because
    they select ``Violation`` fields. *extra* carries anything else that changes
    the output for identical rows; a rule passes its message template.
    """
    return hash_of(
        {"body": conjunction_to_json(body), "head": list(head), "extra": extra}
    )


def clauses_version_hash(clauses: tuple[Clause, ...]) -> str:
    """The version of a multi-clause definition -- a ``DerivedPredicate``.

    Clause **order is kept**. A predicate's value is the union of its clauses so
    order does not change the answer, but it does change the row order the
    interpreter yields before a caller sorts, and keeping order costs nothing
    while making the hash a faithful description of the definition. Two
    definitions that differ only in clause order are two definitions.
    """
    return hash_of([clause_to_json(clause) for clause in clauses])
