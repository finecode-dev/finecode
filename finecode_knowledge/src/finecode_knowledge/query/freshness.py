"""The freshness verdict: a revision plus a possibly-empty list of reservations (ADR-0014).

**A leaf module.** The query layer imports it and the memo DAG will import it;
it imports neither. ``Revision`` and ``Conflict`` are re-exported from
``model/fact_source.py`` rather than redefined, because a conflict is detected
in the store and the seam has to name it.

**Not a ``Verified | Unconfirmed`` union.** R16's two cases -- "fully verified"
and "these inputs were unconfirmed" -- are the empty and non-empty cases of one
list, not two shapes. Both carry ``revision``, so a union would force
``isinstance`` narrowing at every call site to reach the field every case has,
and it would tax the common case, which must be free to construct.
"""

from __future__ import annotations

import dataclasses
import enum

from finecode_knowledge.model.fact_source import Conflict, FieldValue, Revision

__all__ = [
    "Conflict",
    "FieldValue",
    "Freshness",
    "Reservation",
    "ReservationKind",
    "Revision",
]


class ReservationKind(enum.Enum):
    """Closed, and each kind traces to exactly one requirement.

    A fourth kind needs a requirement behind it. The alternative -- a free-text
    list -- turns the verdict into a log that no caller can branch on, so the
    CLI, the MCP surface and every test would each reimplement parsing it.
    """

    CACHED = "cached"
    """Served from the memo, not verified at ``revision`` (§4.12, ``Mode.CACHED``).

    The one kind no *execution* can produce, and that is structural rather than a
    gap: it says "this answer was not recomputed", so the only layer that can
    assert it is the layer that decided not to recompute. The interpreter never
    attaches it; ``memo/walk.py`` does, on the branch where it serves a value it
    declined to verify."""
    UNTRACKED = "untracked"
    """A declared-untracked input this answer depends on (R9)."""
    CONTESTED = "contested"
    """Providers disagree on a field this query read (C9)."""
    STALE = "stale"
    """A **tracked** input changed since the facts were extracted (R11, ADR-0022).

    The fourth kind, and the requirement D2 demanded before adding one. It is
    not ``UNTRACKED``: the input *is* tracked, which is the whole content of
    R11, and collapsing the two would lose the distinction between "we could
    not check" and "we checked and it moved". It is not ``CACHED``: nothing was
    memoized, and ``CACHED`` is unreachable under ``Mode.VERIFIED`` -- where a
    stale fact file matters most."""


@dataclasses.dataclass(frozen=True)
class Reservation:
    """R16 read literally: ``subject`` is *which* input, ``kind`` + ``detail`` is *why*."""

    kind: ReservationKind
    subject: str
    """Which input: a rendered footprint key, node id, or declared input name.

    A rendered string rather than a structured key because the verdict's
    audience is a rule author, a CLI footer and an MCP payload -- all
    string-consuming. The *structured* dependency record is the footprint, and
    it goes to the memo layer instead (ADR-0014 D3)."""
    detail: str
    """Why, in one sentence, safe to show a user."""


@dataclasses.dataclass(frozen=True)
class Freshness:
    """A revision and the reservations against it. Empty means fully verified.

    ``Result[T]`` always carries one. There is no ``None`` and no "unknown" -- a
    result whose freshness could not be established says so with a reservation,
    which is R16's "never a bare value where staleness was possible" made
    structural rather than remembered.
    """

    revision: Revision
    reservations: tuple[Reservation, ...] = ()

    @property
    def verified(self) -> bool:
        return not self.reservations

    def with_reservations(self, *added: Reservation) -> Freshness:
        return dataclasses.replace(self, reservations=(*self.reservations, *added))

    @staticmethod
    def merge(revision: Revision, *verdicts: Freshness) -> Freshness:
        """Combine verdicts, deduplicating reservations by identity.

        ADR-0014's presentational mitigation -- render reservations once per
        run, not once per violation -- and it lives **here** rather than inside
        ``validation.run_all`` because that is no longer the only path that
        needs it. Reservations became per-unit with R11, and ADR-0025 D5 puts a
        ``Freshness`` on every knowledge-answering result: a multi-hop
        ``walk_knowledge`` over twenty units would otherwise hand an assistant
        twenty reservations for one answer, none of them through ``run_all``.

        A reservation names an *input*, so three rules reading one stale unit
        share one reservation. First-seen order is kept, because the order
        reservations were encountered is the only ordering that means anything.
        """
        seen: dict[tuple[ReservationKind, str, str], Reservation] = {}
        for verdict in verdicts:
            for reservation in verdict.reservations:
                seen.setdefault(
                    (reservation.kind, reservation.subject, reservation.detail),
                    reservation,
                )
        return Freshness(revision=revision, reservations=tuple(seen.values()))


def untracked_fact_file(revision: Revision) -> Reservation:
    """The standing reservation for a store with no input tracking at all (ADR-0014 D7).

    ADR-0014's honesty clause: a fact-file digest identifies exactly the facts
    served and says *nothing* about whether the sources have moved since.

    **Narrowed by R11.** D7 predicted this "disappears when the memo DAG makes
    fact buckets tracked nodes"; persisted fingerprints make buckets tracked
    without the DAG, so it lands early and partially (ADR-0022 D4). It now fires
    only when a store carries no verdicts at all -- no verification was run --
    rather than once per execution. A store that *was* verified reserves per
    unit through ``unit_reservation``.
    """
    return Reservation(
        kind=ReservationKind.UNTRACKED,
        subject=f"fact store @ {revision}",
        detail=(
            "Facts were loaded from a fact file; whether the sources they were "
            "extracted from have changed since is not tracked on this path."
        ),
    )


def unit_reservation(
    kind: ReservationKind, unit_id: str, provider_id: str, detail: str
) -> Reservation:
    """One bucket's verdict, as the thing a caller reads (ADR-0022 D4).

    ``subject`` names the *unit*, not the whole fact file: the point of R11 is
    that a caller can tell which inputs are in question, and "the fact store" is
    not an answer they can act on.
    """
    return Reservation(kind=kind, subject=f"{provider_id}/{unit_id}", detail=detail)


def memo_not_verified(node_key: object, verified_at: int, asked_at: int) -> Reservation:
    """A memoized value served without being verified at the caller's revision (ADR-0014 D6).

    ``Mode.CACHED``'s whole content, and the reason §4.12 can call it safe: the
    value is returned immediately and the answer *says* it was not checked, so
    G3's "no silent staleness" holds while G7's latency is paid at all.

    It fires **only** when the two revisions differ. A cached-mode read of a node
    the walk already verified at the current revision is not stale in any sense,
    and reserving against it would train every caller to ignore the kind --
    which is the failure mode that makes a freshness verdict decorative.
    """
    return Reservation(
        kind=ReservationKind.CACHED,
        subject=f"memo node {node_key}",
        detail=(
            f"Served from the memo as it stood at revision {verified_at}; the "
            f"world has since moved to {asked_at} and this answer was not "
            "re-verified against it."
        ),
    )


def contested_slot(conflict: Conflict) -> Reservation:
    """A field slot two providers disagree about (ADR-0014 D4).

    Both values still bind -- the interpreter does not pick. A contested slot
    yields the union of possible worlds, flagged. Picking one is the silent
    last-write-wins C9 excludes.
    """
    rendered = ", ".join(
        f"{v.value!r} (from {v.prov.provider})" for v in conflict.values
    )
    return Reservation(
        kind=ReservationKind.CONTESTED,
        subject=f"{conflict.entity.type}{list(conflict.entity.key)}.{conflict.field}",
        detail=f"Providers disagree on this field: {rendered}.",
    )
