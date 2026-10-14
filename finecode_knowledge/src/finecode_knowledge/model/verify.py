"""The cold-start confirmation walk: which buckets are still current (R11, Q4).

``goals.md`` §4.8 decides the shape. A CLI invocation has no watcher, so it
re-checks the tracked inputs itself, **gated by ``mtime`` + ``size``**: if both
are unchanged the file is not read at all, and content is hashed only when the
cheap gate trips. The hash, never the mtime, is what decides -- mtime moves on
checkout and clone with identical content, so a verdict resting on it would
false-invalidate constantly (R8).

This module answers *which buckets are confirmed*. Turning that into what a
caller sees is Phase 4's job: a verdict here becomes a reservation
([ADR-0022](adr/0022)) only for the buckets a given query could have read.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import pathlib
import typing

from finecode_knowledge.model.fingerprint import resolve
from finecode_knowledge.model.unit import BucketKey, Unit

if typing.TYPE_CHECKING:
    from finecode_knowledge.model.store import FactStore

__all__ = ["BucketVerdict", "Verdict", "VerifyReport", "verify_inputs"]

_CHUNK = 1 << 20


class Verdict(enum.Enum):
    """Why a bucket is, or is not, current.

    Four outcomes and not three: ``MISSING`` is separated from ``STALE``
    because a **deleted** source is what a future retraction acts on (§4.7,
    R10) while a changed one is not. That distinction lives here and
    deliberately does not reach the reservation, where no caller would act
    differently on it (ADR-0022 D3).
    """

    CONFIRMED = "confirmed"
    """Every declared input matches the fingerprint taken at extraction."""
    STALE = "stale"
    """A tracked input's content differs from what was extracted (ADR-0022 D1)."""
    MISSING = "missing"
    """A declared input no longer exists. Not retracted here -- a query must not
    mutate the store as a side effect (§4.7)."""
    UNTRACKED = "untracked"
    """Nothing to check against: the unit declares an input that cannot be
    fingerprinted (R9), or carries no fingerprints at all -- an uncaptured unit
    or a fact file written before R11. Those are the same situation, and
    ADR-0014 D7 already described it as untracked."""


@dataclasses.dataclass(frozen=True)
class BucketVerdict:
    kind: Verdict
    unit: Unit
    detail: str
    """One sentence naming *what*, safe to show a user."""

    @property
    def confirmed(self) -> bool:
        return self.kind is Verdict.CONFIRMED


@dataclasses.dataclass(frozen=True)
class VerifyReport:
    """Per-bucket verdicts, plus what the walk cost.

    ``stat_count`` and ``hash_count`` are here so "a cold start is stat-bound,
    not hash-bound" can be asserted against the walk directly rather than
    reconstructed by instrumenting the filesystem. The same reasoning
    ADR-0013 D5 gives for ``FootprintCollector`` being a first-class object.
    """

    verdicts: dict[BucketKey, BucketVerdict]
    stat_count: int = 0
    hash_count: int = 0

    def __getitem__(self, key: BucketKey) -> BucketVerdict:
        return self.verdicts[key]

    def unconfirmed(self) -> dict[BucketKey, BucketVerdict]:
        return {k: v for k, v in self.verdicts.items() if not v.confirmed}


def verify_inputs(store: FactStore, workspace_root: pathlib.Path) -> VerifyReport:
    """Re-check every bucket's declared inputs against the store's fingerprints.

    Two passes, because ADR-0024 D2's bucket dependency is resolved **one hop**
    and a hop needs its target already decided. A transitive walk is the memo
    DAG's job; one hop is enough while the only dependency in the graph is
    ``ast_definitions``' bound edges on ``wm_registry``.
    """
    counter = _Counter()
    verdicts = {
        unit.key: _verify_one(unit, workspace_root, counter) for unit in store.units()
    }
    _inherit_from_dependencies(verdicts)
    return VerifyReport(
        verdicts=verdicts, stat_count=counter.stats, hash_count=counter.hashes
    )


@dataclasses.dataclass
class _Counter:
    stats: int = 0
    hashes: int = 0


def _verify_one(
    unit: Unit, workspace_root: pathlib.Path, counter: _Counter
) -> BucketVerdict:
    if not unit.fingerprints:
        return BucketVerdict(
            kind=Verdict.UNTRACKED,
            unit=unit,
            detail=(
                f"{unit.unit_id} carries no input fingerprints, so nothing links its "
                "facts to the sources they came from."
            ),
        )

    missing: list[str] = []
    changed: list[str] = []
    for fingerprint in unit.fingerprints:
        path = resolve(fingerprint.path, workspace_root)
        counter.stats += 1
        try:
            stat = path.stat()
        except OSError:
            missing.append(fingerprint.path)
            continue
        # The cheap gate (Q4). Unchanged size+mtime means the file is not read
        # at all, which is what keeps a CLI invocation stat-bound.
        if (
            stat.st_size == fingerprint.size
            and stat.st_mtime_ns == fingerprint.mtime_ns
        ):
            continue
        counter.hashes += 1
        if _digest(path) != fingerprint.sha256:
            changed.append(fingerprint.path)

    # Precedence, most actionable first. A unit can be several of these at once
    # -- wm_registry declares an untracked input *and* fingerprints its own code
    # -- and the caller can only act on one thing at a time. MISSING wants
    # investigation, STALE wants re-extraction, UNTRACKED wants nothing because
    # re-extracting will not clear it.
    if missing:
        return BucketVerdict(
            kind=Verdict.MISSING,
            unit=unit,
            detail=f"Declared input(s) no longer exist: {', '.join(sorted(missing))}.",
        )
    if changed:
        return BucketVerdict(
            kind=Verdict.STALE,
            unit=unit,
            detail=f"Input(s) changed since extraction: {', '.join(sorted(changed))}.",
        )
    if unit.untracked:
        return BucketVerdict(
            kind=Verdict.UNTRACKED,
            unit=unit,
            detail=(
                "Depends on input(s) that cannot be fingerprinted: "
                f"{', '.join(unit.untracked)}."
            ),
        )
    return BucketVerdict(
        kind=Verdict.CONFIRMED,
        unit=unit,
        detail=f"All {len(unit.fingerprints)} declared input(s) unchanged.",
    )


def _inherit_from_dependencies(verdicts: dict[BucketKey, BucketVerdict]) -> None:
    """ADR-0024 D2: an unconfirmed dependency makes the depending unit unconfirmed.

    Applied only to units that would otherwise be confirmed -- a unit already
    stale on its own inputs keeps the reason a caller can act on. A dependency
    that is not in the store at all cannot be checked, so it is untracked
    rather than absent: claiming confirmation over a bucket that was never
    ingested is exactly the false-clean verdict this walk exists to prevent.
    """
    for key, verdict in list(verdicts.items()):
        if not verdict.confirmed:
            continue
        for dependency in verdict.unit.depends_on:
            found = verdicts.get(dependency)
            if found is None:
                verdicts[key] = dataclasses.replace(
                    verdict,
                    kind=Verdict.UNTRACKED,
                    detail=(
                        f"Derived from bucket {dependency[0]}/{dependency[1]}, which is "
                        "not in the store."
                    ),
                )
                break
            if not found.confirmed:
                verdicts[key] = dataclasses.replace(
                    verdict,
                    kind=found.kind,
                    detail=(
                        f"Derived from bucket {dependency[0]}/{dependency[1]}, which is "
                        f"{found.kind.value}: {found.detail}"
                    ),
                )
                break


def _digest(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
